"""Slot outages preserve challengers, isolate queues, and optionally redirect rewards."""

import dataclasses
import hashlib
import json
import subprocess
import sys

import pytest
from sqlalchemy import select

import axiom
import db
import gate
import reward
from conftest import register
from db import models
from db.scoring import Board, ScoredSubmission
from service import worker
from service.settings import Settings


def add(store, name, slot="add", body=b"RATIO=1.01"):
    register(store, name, uid=len(name), block=1000 + sum(name.encode()))
    return store.submissions.add(name, hashlib.sha256(body).hexdigest(), slot, body)[0]


def pause(store, sub, retry=300):
    assert store.pauses.fail(
        sub,
        reference_id=None,
        reference_sha256="c" * 64,
        phase="reference-build",
        reason="compiler unavailable",
        retry_seconds=retry,
    )


def test_cooldown_priority_restart_and_stale_attempt(store, clock):
    first = add(store, "first")
    sub = store.submissions.claim_next("worker")
    pause(store, sub)
    second = add(store, "second", body=b"different")
    other = add(store, "other", "mul")
    assert store.submissions.claim_next("worker").id == other
    assert store.submissions.claim_next("worker") is None
    # Another Store sees the same pause, with no in-memory scheduling state.
    restarted = db.Store(store.engine)
    clock["tick"](300)
    retry = restarted.submissions.claim_next("worker")
    assert retry.id == first != second
    revision = store.pauses.all()["add"].revision
    store.pauses.recover(sub, revision)  # old attempt cannot clear a newer claim
    assert "add" in store.pauses.all()
    assert not store.pauses.fail(
        sub,
        reference_id=None,
        reference_sha256=None,
        phase="old",
        reason="stale",
        retry_seconds=300,
    )
    store.pauses.recover(retry, revision)
    assert not store.pauses.all()


def test_due_retry_can_yield_to_other_slots(store, clock):
    first = add(store, "first")
    sub = store.submissions.claim_next("w")
    pause(store, sub)
    other = add(store, "other", "mul")
    clock["tick"](300)
    assert store.submissions.claim_next("w", prefer_healthy=True).id == other
    assert store.submissions.claim_next("w").id == first


def test_terminal_challenger_advances_but_does_not_clear_pause(store, clock):
    add(store, "first")
    sub = store.submissions.claim_next("w")
    pause(store, sub)
    with db.session_scope(store.sessions) as session:
        session.get(models.Submission, sub.id).state = "rejected"
    assert store.submissions.claim_next("w") is None
    second = add(store, "second")
    clock["tick"](300)
    assert store.submissions.claim_next("w").id == second
    assert store.pauses.all()["add"].challenger_id == second


def test_failed_retry_cooldown_starts_at_completion(store, clock):
    add(store, "first")
    sub = store.submissions.claim_next("w")
    pause(store, sub)
    clock["tick"](300)
    retry = store.submissions.claim_next("w")
    clock["tick"](600)
    pause(store, retry)
    assert store.submissions.claim_next("w") is None
    clock["tick"](299)
    assert store.submissions.claim_next("w") is None
    clock["tick"](1)
    assert store.submissions.claim_next("w").id == sub.id


def test_pause_reward_policy_keeps_champion_visible(clock):
    leader = ScoredSubmission(1, "alice", 0.9, clock["t"])
    board = Board("full", [leader], [], leader, paused=True)
    preserved = reward.score([board], reward.ScoringConfig())
    burned = reward.score([board], reward.ScoringConfig(paused_slot_policy="treasury"))
    assert preserved.weights == {"alice": 1.0}
    assert burned.weights == {}
    snapshot = reward.publication(
        burned,
        [board],
        reward.ScoringConfig(paused_slot_policy="treasury"),
        competition_share=0.2,
        computed_at=clock["t"],
    )
    assert snapshot["slots"]["full"]["leader"] == 1
    assert snapshot["slots"]["full"]["paused"] is True
    assert snapshot["slots"]["full"]["items"][0]["weight"] == 0
    restored = reward.score([dataclasses.replace(board, paused=False)], reward.ScoringConfig())
    assert restored.weights == preserved.weights


def test_settings_validate_policy_and_interval(monkeypatch):
    monkeypatch.setenv("CHAMPION_RETRY_SECONDS", "23")
    assert Settings().champion_retry_seconds == 23
    monkeypatch.setenv("CHAMPION_RETRY_SECONDS", "0")
    with pytest.raises(ValueError):
        Settings()
    with pytest.raises(ValueError):
        reward.ScoringConfig(paused_slot_policy="typo")


@pytest.mark.parametrize(
    "phase,exit_code", [("reference-build", 2), ("reference-benchmark", 2), ("challenger", 1)]
)
def test_outer_timeout_attributes_active_phase(tmp_path, monkeypatch, phase, exit_code):
    stub = tmp_path / "gate.py"
    stub.write_text(
        "import os, json, time\nfrom pathlib import Path\n"
        f"Path(os.environ['GATE_PROGRESS_PATH']).write_text(json.dumps({{'phase': {phase!r}, "
        "'champion_sha256': 'c' * 64}))\ntime.sleep(30)\n"
    )
    monkeypatch.setattr(worker, "GATE", stub)
    monkeypatch.setattr(worker, "TOTAL_TIMEOUT", 0.3)
    verdict = tmp_path / "verdict.json"
    result = worker.run_gate(tmp_path, verdict)
    assert result.returncode == exit_code
    report = json.loads(verdict.read_text())
    assert report["incumbent_failed"] == (exit_code == 2)
    assert report["champion_sha256"] == "c" * 64


def test_pin_failure_writes_structured_result(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "HERE", tmp_path)
    monkeypatch.setattr(gate, "log_to", lambda *args: None)
    monkeypatch.setattr(gate, "check_limits", lambda: None)

    def broken(slot):
        raise RuntimeError("compiler unavailable")

    monkeypatch.setattr(gate, "pin", broken)
    verdict = tmp_path / "verdict.json"
    monkeypatch.setattr(sys, "argv", ["gate.py", str(tmp_path), "--verdict", str(verdict)])
    assert gate.main() == 2
    report = json.loads(verdict.read_text())
    assert report["incumbent_failed"] is True
    assert report["reference_failure_phase"] == "reference-build"
    assert "compiler unavailable" in report["reason"]


def test_worker_recovery_and_other_slot_progress(store, settings, stub_gate, monkeypatch):
    first = add(store, "first")
    second = add(store, "second", body=b"RATIO=1.02")
    other = add(store, "other", "mul")
    original = worker.run_gate
    seen = []

    def broken(directory, verdict, slot, env):
        seen.append(int(directory.name))
        if slot == "add":
            verdict.write_text(
                json.dumps(
                    {
                        "incumbent_failed": True,
                        "reference_failure_phase": "reference-build",
                        "reason": "unavailable",
                    }
                )
            )
            return subprocess.CompletedProcess([], 2, "", "")
        return original(directory, verdict, slot, env)

    monkeypatch.setattr(worker, "run_gate", broken)
    assert worker.drain(store, settings) == 1
    assert seen == [first, other]
    assert store.submissions.get(second).state == "queued"
    assert store.submissions.get(first).gate_errors == 0
    monkeypatch.setattr(worker, "run_gate", original)
    store.pauses.retry("add")
    assert worker.drain(store, settings) == 2
    assert store.pauses.all() == {}


class Recorder:
    # An Axiom client that keeps every event it is given.
    enabled = True

    def __init__(self):
        self.events = []

    def ingest(self, *, severity, source, event_type, details):
        self.events.append((severity, event_type, details))

    def close(self):
        pass


def test_a_pause_alerts_on_every_failed_attempt_and_on_recovery(
    store, settings, stub_gate, monkeypatch
):
    first = add(store, "first")
    recorder = Recorder()
    monkeypatch.setattr(worker, "get_events", lambda: axiom.Events(recorder, source="test"))
    original = worker.run_gate

    def broken(directory, verdict, slot, env):
        verdict.write_text(
            json.dumps(
                {
                    "incumbent_failed": True,
                    "reference_failure_phase": "reference-benchmark",
                    "champion_sha256": "c" * 64,
                    "reason": "the incumbent's bench failed",
                }
            )
        )
        return subprocess.CompletedProcess([], 2, "", "")

    monkeypatch.setattr(worker, "run_gate", broken)
    worker.drain(store, settings)
    store.pauses.retry("add")
    worker.drain(store, settings)
    paused = [(s, d) for s, t, d in recorder.events if t == "slot_paused"]
    assert [s for s, _ in paused] == ["error", "error"]
    assert [d["first"] for _, d in paused] == [True, False]
    assert all(d["slot"] == "add" and d["submission_id"] == first for _, d in paused)
    assert paused[0][1]["phase"] == "reference-benchmark"
    assert paused[1][1]["paused_since"] is not None
    monkeypatch.setattr(worker, "run_gate", original)
    store.pauses.retry("add")
    worker.drain(store, settings)
    recovered = [(s, d) for s, t, d in recorder.events if t == "slot_recovered"]
    assert [s for s, _ in recovered] == ["info"]
    assert recovered[0][1]["submission_id"] == first
    assert store.pauses.all() == {}


def test_manual_demotion_refuses_wrong_champion(store):
    sid = add(store, "champion")
    with db.session_scope(store.sessions) as session:
        session.add(models.Promotion(slot="add", submission_id=sid, champion_sha256="a" * 64))
    with pytest.raises(ValueError):
        store.pauses.demote("add", sid + 1, "audit")
    assert store.submissions.champion("add").id == sid
    store.pauses.demote("add", sid, "audit confirmed failure")
    assert store.submissions.champion("add") is None
    assert store.pauses.all()["add"].reason == "audit confirmed failure"
    with db.session_scope(store.sessions) as session:
        assert session.scalar(select(models.Promotion)).failures == 0


def test_no_verdict_reference_exit_pauses_instead_of_penalizing(tmp_path, monkeypatch):
    stub = tmp_path / "gate.py"
    stub.write_text(
        "import os, json\nfrom pathlib import Path\n"
        "Path(os.environ['GATE_PROGRESS_PATH']).write_text(json.dumps("
        "{'phase': 'reference-build', 'champion_sha256': 'c' * 64}))\nos._exit(2)\n"
    )
    monkeypatch.setattr(worker, "GATE", stub)
    verdict = tmp_path / "verdict.json"
    assert worker.run_gate(tmp_path, verdict).returncode == 2
    assert json.loads(verdict.read_text())["incumbent_failed"] is True


def test_early_rejection_keeps_pause_until_next_challenger(store, settings, stub_gate, clock):
    first = add(store, "first", body=b"REJECT")
    sub = store.submissions.claim_next("w")
    pause(store, sub)
    second = add(store, "second")
    clock["tick"](300)
    assert worker.drain(store, settings) == 1
    assert store.submissions.get(first).state == "rejected"
    assert store.submissions.get(second).state == "queued"
    assert "add" in store.pauses.all()
    clock["tick"](300)
    assert worker.drain(store, settings) == 1
    assert not store.pauses.all()


def test_retries_rotate_due_slots(store, clock):
    first = add(store, "first", "add")
    pause(store, store.submissions.claim_next("w"))
    second = add(store, "second", "mul")
    pause(store, store.submissions.claim_next("w"))
    clock["tick"](300)
    retry = store.submissions.claim_next("w")
    assert retry.id == first
    pause(store, retry)
    assert store.submissions.claim_next("w").id == second


def test_pause_treasury_vector_and_snapshot_dedup(store, clock):
    from test_weight_setter import FakeChain, accept, config, vector

    from workers.weight_setter import plan_for, step

    accept(store, "alice", 0.9)
    chain = FakeChain(hotkeys={"alice": 1})
    cfg = config()
    preserved = reward.ScoringConfig()
    redirected = reward.ScoringConfig(paused_slot_policy="treasury")
    assert vector(plan_for(store, chain.metagraph(2), cfg, preserved)[0]) == {
        0: 0.8,
        1: 0.2,
        2: 0.0,
    }
    challenger = add(store, "challenger", "full")
    sub = store.submissions.claim_next("w")
    assert sub.id == challenger
    pause(store, sub)
    assert vector(plan_for(store, chain.metagraph(2), cfg, preserved)[0]) == {
        0: 0.8,
        1: 0.2,
        2: 0.0,
    }
    assert vector(plan_for(store, chain.metagraph(2), cfg, redirected)[0]) == {
        0: 1.0,
        1: 0.0,
        2: 0.0,
    }
    step(chain, store, cfg, redirected)
    with store.engine.connect() as connection:
        ids_before = connection.execute(select(models.WeightSet.id)).scalars().all()
    clock["tick"](300)
    retrying = store.submissions.claim_next("w")
    step(chain, store, cfg, redirected)
    with store.engine.connect() as connection:
        assert connection.execute(select(models.WeightSet.id)).scalars().all() == ids_before
    pause(store, retrying)
    step(chain, store, cfg, redirected)
    with store.engine.connect() as connection:
        assert connection.execute(select(models.WeightSet.id)).scalars().all() == ids_before
    store.pauses.retry("full")
    retry = store.submissions.claim_next("w")
    store.pauses.recover(retry, store.pauses.all()["full"].revision)
    assert vector(plan_for(store, chain.metagraph(2), cfg, redirected)[0]) == {
        0: 0.8,
        1: 0.2,
        2: 0.0,
    }
