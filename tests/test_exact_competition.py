"""Exact board statistics, fencing, reconciliation and rewards on isolated PostgreSQL."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text

import db
from db import models as m
from db.exact import ExactDb
from exact.config import Config, digest, measurement_hash
from exact.rewards import score
from exact.scoring import assess


@pytest.fixture
def config(tmp_path):
    source = b"initial reference"
    path = tmp_path / "Kernel.lean"
    path.write_bytes(source)
    boards = {
        name: {
            "models": {model: 1.0},
            "reference": {"path": str(path), "sha256": hashlib.sha256(source).hexdigest()},
        }
        for name, model in (("mlp", "mlp"), ("resnet", "resnet"))
    }
    return Config.parse(
        {
            "boards": boards,
            "environments": {"local": {"weight": 1, "identity": "a" * 64}},
            "seeds": [42],
            "timing": {
                "rounds": 5,
                "inner": 1,
                "cell_ms": 1,
                "run_seconds": 30,
                "job_seconds": 120,
            },
            "admission": {
                "bar": 0.97,
                "guard": 1.1,
                "draws": 100,
                "quantile": 0.05,
                "min_rounds": 5,
            },
            "rewards": {
                "board_weights": {"mlp": 0.5, "resnet": 0.5},
                "rank_shares": [0.8, 0.2],
                "paused_policy": "preserve",
            },
            "proof_identity": "b" * 64,
            "benchmark_identity": "c" * 64,
            "retry_seconds": 0.1,
        }
    )


def accepted(store, config, hotkey="alice", content=b"candidate"):
    sid, _ = store.submissions.add(hotkey, hashlib.sha256(content).hexdigest(), "exact", content)
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Submission, sid)
        row.state = "accepted"
        row.exact_verification = {"identity": config.proof_identity, "accepted": True}
    return sid


def result(request, ratio=0.9):
    return {
        "request_hash": measurement_hash(request),
        "provenance": {"identity": request["identity"], "machine": "test-machine"},
        "repetitions": {c: 1 for c in request["cells"]},
        "rounds": [
            {
                "reference_first": i % 2 == 0,
                "cells": {
                    c: {
                        "reference_ns": 1000,
                        "candidate_ns": 1000 * ratio,
                        "reference_checksum": 123,
                        "candidate_checksum": 123,
                    }
                    for c in request["cells"]
                },
            }
            for i in range(request["timing"]["rounds"])
        ],
    }


def request_for(config, board="mlp"):
    plan = config.plan(board)
    return {k: v for k, v in plan["strata"][0].items() if k != "weight"} | {
        k: plan[k] for k in ("cells", "timing", "proof_identity", "benchmark_identity")
    }


@pytest.mark.parametrize(
    "ratio,outcome", [(1, "above-bar"), (0.9, "qualifies"), (1.2, "guard-failed")]
)
def test_scoring(config, ratio, outcome):
    request = request_for(config)
    decision = assess(config.plan("mlp"), [(request, result(request, ratio))])
    assert decision["score"] == pytest.approx(ratio)
    assert decision["outcome"] == outcome
    assert decision == assess(config.plan("mlp"), [(request, result(request, ratio))])


def test_stratified_hardware_guard(config):
    plan = config.plan("mlp")
    plan["strata"] = [
        plan["strata"][0] | {"weight": 0.5},
        plan["strata"][0] | {"environment": "arm", "weight": 0.5},
    ]
    a = request_for(config)
    b = a | {"environment": "arm"}
    decision = assess(plan, [(a, result(a, 0.5)), (b, result(b, 1.2))])
    assert decision["score"] < 0.97
    assert decision["outcome"] == "guard-failed"
    with pytest.raises(ValueError, match="incomplete"):
        assess(plan, [(a, result(a))])


@pytest.mark.parametrize("change", ["nan", "checksum", "missing", "order", "provenance"])
def test_bad_evidence(config, change):
    request = request_for(config)
    data = result(request)
    cell = next(iter(request["cells"]))
    if change == "nan":
        data["rounds"][0]["cells"][cell]["candidate_ns"] = float("nan")
    elif change == "checksum":
        data["rounds"][0]["cells"][cell]["candidate_checksum"] = 4
    elif change == "missing":
        data["rounds"].pop()
    elif change == "order":
        data["rounds"][0]["reference_first"] = False
    else:
        data["provenance"]["identity"] = "wrong"
    with pytest.raises(ValueError):
        assess(config.plan("mlp"), [(request, data)])


def test_reward_only_identity_and_paths(config):
    raw = copy.deepcopy(config.__dict__)
    raw["rewards"]["rank_shares"] = [0.9, 0.1]
    changed = Config.parse(raw)
    assert config.identity("mlp") == changed.identity("mlp")
    assert digest(config.public()) != digest(changed.public())
    assert "path" not in config.public()["boards"]["mlp"]["reference"]


def test_legacy_worker_cannot_claim_exact(store, config):
    store.submissions.add("alice", "d" * 64, "exact", b"entry")
    assert store.submissions.claim_next("legacy") is None
    repo = ExactDb(store.sessions)
    row = repo.claim_proof("coordinator", config.proof_identity, 30)
    assert row.slot == "exact"
    assert not repo.finish_proof(row.id, "stale", config.proof_identity, {"exit_code": 0}, 30)
    assert repo.finish_proof(
        row.id, row.verification_attempt, config.proof_identity, {"exit_code": 0}, 30
    )
    assert repo.claim_proof("coordinator", config.proof_identity, 30) is None


def test_leases_promotions_and_rewards(store, config):
    sid = accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    assert len(repo.plan(config)) == 2
    assert repo.plan(config) == []
    with ThreadPoolExecutor(2) as pool:
        jobs = list(
            pool.map(lambda owner: repo.claim("local", "a" * 64, owner, 30), ("one", "two"))
        )
    assert len({j.id for j in jobs}) == 2
    for j in jobs:
        assert not repo.complete(j.id, j.owner, "wrong", result(j.request))
        assert repo.heartbeat(j.id, j.owner, j.token, 30)
        assert repo.complete(j.id, j.owner, j.token, result(j.request))
        assert not repo.complete(j.id, j.owner, j.token, result(j.request))
    assert repo.aggregate(config) == 2
    assert repo.aggregate(config) == 0
    with db.session_scope(store.sessions) as s:
        promotions = list(s.scalars(select(m.ExactPromotion)))
        assert len(promotions) == 2
        assert {p.submission_id for p in promotions} == {sid}
    assert score(repo.publication()).weights == pytest.approx({"alice": 0.8})
    assert repo.plan(config) == []


def test_expired_token_and_fairness(store, config):
    accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.plan(config)
    job = repo.claim("local", "a" * 64, "dead", 30)
    with db.session_scope(store.sessions) as s:
        s.get(m.ExactJob, job.id).lease_until = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=1
        )
    assert not repo.complete(job.id, "dead", job.token, result(job.request))
    healthy = repo.claim("local", "a" * 64, "healthy", 0.1)
    assert healthy.id != job.id
    assert repo.complete(healthy.id, healthy.owner, healthy.token, result(healthy.request))
    assert repo.aggregate(config) == 1
    assert len(repo.ready()) == 0
    assert not repo.heartbeat(job.id, "dead", job.token, 30)


def test_reference_retry_does_not_starve_other_board(store, config):
    sid = accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    repo.plan(config)
    job = repo.claim("local", "a" * 64, "worker", 30)
    assert repo.fail(
        job.id, job.owner, job.token, {"kind": "reference", "reason": "broken build"}, 0.001
    )
    repo.publish(config)
    assert any(b["paused"] for b in repo.publication()["boards"].values())
    healthy = repo.claim("local", "a" * 64, "worker", 30)
    assert healthy.id != job.id
    repo.complete(healthy.id, healthy.owner, healthy.token, result(healthy.request))
    assert repo.aggregate(config) == 1
    # The original candidate remains head on the blocked board.
    other = accepted(store, config, "bob", b"next")
    repo.plan(config)
    with db.session_scope(store.sessions) as s:
        pending = list(
            s.scalars(select(m.ExactEvaluation).where(m.ExactEvaluation.state == "pending"))
        )
        assert {e.submission_id for e in pending} == {sid, other}


def test_reconcile_cancels_stale_and_reuses_compatible_results(store, config):
    accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    repo.plan(config)
    j = repo.claim("local", "a" * 64, "worker", 30)
    repo.complete(j.id, j.owner, j.token, result(j.request))
    raw = copy.deepcopy(config.__dict__)
    raw["admission"]["bar"] = 0.95
    changed = Config.parse(raw)
    repo.reconcile(changed)
    repo.plan(changed)
    assert not repo.complete(j.id, j.owner, j.token, result(j.request))
    assert repo.aggregate(changed) == 1  # completed measurement reused, no new timing
    with db.session_scope(store.sessions) as s:
        assert (
            s.scalar(select(m.ExactEvaluation).where(m.ExactEvaluation.id == j.evaluation_id)).state
            == "superseded"
        )


def test_stale_reference_cannot_promote(store, config):
    sid = accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.plan(config)
    with db.session_scope(store.sessions) as s:
        e = s.scalar(select(m.ExactEvaluation).order_by(m.ExactEvaluation.id))
        eid = e.id
        s.add(
            m.ExactPromotion(
                board=e.board, submission_id=sid, evaluation_id=eid, source_hash="e" * 64
            )
        )
    assert not repo.finalize(eid, config, {"outcome": "qualifies"})


def test_leader_fencing(store, config):
    old, new = ExactDb(store.sessions), ExactDb(store.sessions)
    old.set_leader("old")
    new.set_leader("new")
    with pytest.raises(RuntimeError, match="leadership"):
        old.reconcile(config)
    new.reconcile(config)


@pytest.mark.parametrize("n", [0, 1, 2])
def test_two_rank_payout(config, n):
    publication = {
        "version": "exact-top2-v1",
        "rewards": config.rewards,
        "boards": {
            board: {
                "paused": False,
                "kings": [
                    {"promotion_id": i + 1, "submission_id": i + 1, "hotkey": "alice"}
                    for i in range(n)
                ],
            }
            for board in config.boards
        },
    }
    assert sum(score(publication).weights.values()) == pytest.approx([0, 0.8, 1][n])
    publication["rewards"] = publication["rewards"] | {"paused_policy": "treasury"}
    for board in publication["boards"].values():
        board["paused"] = True
    assert score(publication).weights == {}


def test_worker_execute_only_role(store, config):
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from exact.worker_db import WorkerDb

    accepted(store, config)
    admin = ExactDb(store.sessions)
    admin.reconcile(config)
    admin.plan(config)
    root = Path(__file__).resolve().parents[1]
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            (root / "deploy/migrate/alembic/versions/0018_exact_protocol.sql").read_text()
        )
        conn.exec_driver_sql("CREATE ROLE exact_benchmark_worker NOLOGIN")
        conn.execute(text((root / "deploy/platform/exact_benchmark_worker.sql").read_text()))
    engine = create_engine(
        store.engine.url, connect_args={"options": "-c role=exact_benchmark_worker"}
    )
    try:
        worker = WorkerDb(sessionmaker(bind=engine))
        job = worker.claim("local", "a" * 64, "restricted", 30)
        assert job is not None
        assert all(worker.sources(job.id))
        assert worker.heartbeat(job.id, job.owner, job.token, 30)
        assert not worker.heartbeat(job.id, job.owner, "bad", 30)
        assert worker.complete(job.id, job.owner, job.token, result(job.request))
        assert not worker.complete(job.id, job.owner, job.token, result(job.request))
        second = worker.claim("local", "a" * 64, "restricted", 30)
        with db.session_scope(store.sessions) as session:
            session.get(m.ExactJob, second.id).lease_until = dt.datetime.now(
                dt.timezone.utc
            ) - dt.timedelta(seconds=1)
        assert not worker.complete(second.id, second.owner, second.token, result(second.request))
        assert worker.claim("local", "a" * 64, "restricted", 0.01) is None
        with db.session_scope(store.sessions) as session:
            session.get(m.ExactJob, second.id).due_at = dt.datetime.now(
                dt.timezone.utc
            ) - dt.timedelta(seconds=1)
        reclaimed = worker.claim("local", "a" * 64, "restricted", 30)
        assert reclaimed.id == second.id and reclaimed.token != second.token
        assert not worker.heartbeat(second.id, second.owner, second.token, 30)
        second = reclaimed
        assert worker.fail(
            second.id, second.owner, second.token, {"kind": "candidate", "reason": "timeout"}, 1
        )
        assert admin.aggregate(config) == 2
        for table in ("exact_promotions", "observed_states", "submissions", "exact_jobs"):
            with engine.begin() as conn:
                assert not conn.scalar(
                    text("SELECT has_table_privilege(current_user,:table,'UPDATE')"),
                    {"table": table},
                )
    finally:
        engine.dispose()
        with store.engine.begin() as conn:
            conn.exec_driver_sql("DROP OWNED BY exact_benchmark_worker")
            conn.exec_driver_sql("DROP ROLE exact_benchmark_worker")


def test_reward_setter_uses_publication_and_keeps_legacy_config_out(store, config):
    from workers.weight_setter import WeightSetterConfig

    accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    repo.plan(config)
    while job := repo.claim("local", "a" * 64, "worker", 30):
        repo.complete(job.id, job.owner, job.token, result(job.request))
    repo.aggregate(config)
    import reward
    from chain.types import MetagraphView
    from workers.weight_setter import plan_for

    setter = WeightSetterConfig(
        scoring_source="exact", netuid=2, treasury_override=0, competition_share_override=0.2
    )
    plan, scored, snapshot = plan_for(
        store, MetagraphView((0, 1), {"alice": 1}), setter, reward.ScoringConfig(winner_share=0)
    )
    assert scored.weights == pytest.approx({"alice": 0.8})
    assert dict(zip(plan.uids, plan.weights)) == pytest.approx({0: 0.84, 1: 0.16})
    assert snapshot["policy"]["rank_shares"] == [0.8, 0.2]


def test_hardware_add_remove_and_cancelled_completion(store, config):
    accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    repo.plan(config)
    old = repo.claim("local", "a" * 64, "worker", 30)
    raw = copy.deepcopy(config.__dict__)
    raw["environments"]["local"]["weight"] = 0.5
    raw["environments"]["arm"] = {"weight": 0.5, "identity": "d" * 64}
    added = Config.parse(raw)
    repo.reconcile(added)
    assert not repo.complete(old.id, old.owner, old.token, result(old.request))
    repo.plan(added)
    for env, identity in (("local", "a" * 64), ("arm", "d" * 64)):
        while job := repo.claim(env, identity, "worker", 30):
            if env == "local":
                repo.complete(job.id, job.owner, job.token, result(job.request))
            else:
                repo.fail(
                    job.id,
                    job.owner,
                    job.token,
                    {"kind": "infrastructure", "reason": "offline"},
                    30,
                )
    assert repo.aggregate(added) == 0
    # Removing the unavailable environment replaces the plan, reusing local evidence.
    repo.reconcile(config)
    repo.plan(config)
    assert repo.aggregate(config) == 2


def test_new_board_does_not_rerank_history(store, config):
    sid = accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    repo.plan(config)
    while job := repo.claim("local", "a" * 64, "worker", 30):
        repo.complete(job.id, job.owner, job.token, result(job.request, 1.0))
    repo.aggregate(config)
    raw = copy.deepcopy(config.__dict__)
    raw["boards"]["new_model_board"] = copy.deepcopy(raw["boards"]["mlp"])
    raw["rewards"]["board_weights"] = {"mlp": 0.25, "resnet": 0.25, "new_model_board": 0.5}
    changed = Config.parse(raw)
    repo.reconcile(changed)
    assert repo.plan(changed) == []
    next_id = accepted(store, changed, "bob", b"new after board activation")
    assert next_id != sid
    assert len(repo.plan(changed)) == 3


def test_disqualification_keeps_full_history_and_fills_previous_rank(store, config):
    repo = ExactDb(store.sessions)
    repo.reconcile(config)
    for i in range(3):
        accepted(store, config, f"miner-{i}", f"source-{i}".encode())
        repo.plan(config)
        while job := repo.claim("local", "a" * 64, "worker", 30):
            repo.complete(job.id, job.owner, job.token, result(job.request))
        assert repo.aggregate(config) == 2
    with db.session_scope(store.sessions) as s:
        promotion = s.scalar(
            select(m.ExactPromotion)
            .where(m.ExactPromotion.board == "mlp")
            .order_by(m.ExactPromotion.id.desc())
        )
        promotion.disqualified_at = dt.datetime.now(dt.timezone.utc)
        assert len(list(s.scalars(select(m.ExactPromotion)))) == 6
    repo.publish(config)
    paid = repo.publication()["boards"]["mlp"]["kings"]
    assert [p["hotkey"] for p in paid] == ["miner-1", "miner-0"]


def test_malformed_worker_result_retries_without_rejecting_submission(store, config):
    sid = accepted(store, config)
    repo = ExactDb(store.sessions)
    repo.plan(config)
    with db.session_scope(store.sessions) as s:
        jobs = list(s.scalars(select(m.ExactJob)))
        for job in jobs:
            job.state = "completed"
            job.result = {"broken": True}
    assert repo.aggregate(config) == 0
    with db.session_scope(store.sessions) as s:
        assert s.get(m.Submission, sid).state == "accepted"
        assert all(e.state == "superseded" for e in s.scalars(select(m.ExactEvaluation)))
    assert len(repo.plan(config)) == 2


def test_supervisor_lost_lease_stops_child(tmp_path):
    import sys

    from exact.process import supervised

    with pytest.raises(RuntimeError, match="lease lost"):
        supervised(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            lambda: False,
            5,
            0.01,
            tmp_path / "process.log",
        )


def test_fresh_seeds_are_frozen_for_retry_and_reconciliation(store, config):
    raw = copy.deepcopy(config.__dict__)
    raw["seed_mode"] = "fresh"
    fresh = Config.parse(raw)
    accepted(store, fresh)
    repo = ExactDb(store.sessions)
    repo.reconcile(fresh)
    repo.plan(fresh)
    with db.session_scope(store.sessions) as s:
        old = {e.board: e.plan["seed_values"] for e in s.scalars(select(m.ExactEvaluation))}
    raw["admission"]["bar"] = 0.95
    changed = Config.parse(raw)
    repo.reconcile(changed)
    repo.plan(changed)
    with db.session_scope(store.sessions) as s:
        active = {
            e.board: e.plan["seed_values"]
            for e in s.scalars(
                select(m.ExactEvaluation).where(m.ExactEvaluation.state == "pending")
            )
        }
    assert old == active
    assert all(len(values) == 1 and 0 <= values[0] < 2**32 for values in active.values())


def test_proof_mode_does_not_build_native_benchmark(tmp_path, monkeypatch):
    import gate

    calls = []
    monkeypatch.setattr(gate, "WORK", tmp_path / "work" / "Submission.lean")
    monkeypatch.setattr(gate, "own", lambda cmd, *a, **kw: calls.append(cmd) or (0, ""))
    monkeypatch.setattr(gate, "foreign", lambda layer: [])
    source = tmp_path / "Kernel.lean"
    source.write_text("import ContractExact")
    assert (
        gate.statement(source, gate.Verdict(submission="proof", slot="exact", mode="proof")) is None
    )
    assert calls == [[gate.LAKE, "build", "VerifyExact"]]


def test_supervisor_cancellation_is_prompt_even_with_long_heartbeat(tmp_path):
    import sys
    import time

    from exact.process import supervised

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="stopping"):
        supervised(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            lambda: True,
            60,
            40,
            tmp_path / "process.log",
            cancelled=lambda: True,
        )
    assert time.monotonic() - t0 < 5


def test_parent_death_kills_worker_subprocess(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    from exact.process import environment

    child_pid = tmp_path / "child.pid"
    child_code = (
        f"import os,time; open({str(child_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    command = [sys.executable, "-c", child_code]
    logfile = str(tmp_path / "parent.log")
    parent_code = (
        f"from exact.process import supervised; supervised({command!r}, "
        f"lambda: True, 90, 30, {logfile!r})"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], env=environment())
    child = None
    try:
        until = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert child_pid.exists()
        child = int(child_pid.read_text())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            status = Path(f"/proc/{child}/status")
            if not status.exists() or "State:\tZ" in status.read_text():
                break
            time.sleep(0.02)
        else:
            pytest.fail("benchmark subprocess survived its dead supervisor")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
