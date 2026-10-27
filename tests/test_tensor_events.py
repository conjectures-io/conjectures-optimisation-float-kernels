"""Axiom events from the coordinator pipeline: what each step reports, how loudly, and when."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import get_args

import pytest
from sqlalchemy import select, text
from test_tensor_workflow import accepted, drain, setup

import axiom
import db
from db import models as m
from db.evaluations import EvaluationsDb
from workers.benchmark_worker import report as report_job
from workers.coordinator import report_proof, report_publication

EVENT_TYPES = set(get_args(axiom.EventType))


class Recorder:
    """An enabled client that keeps every event instead of shipping it."""

    enabled = True

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def ingest(self, *, severity, source, event_type, details) -> None:
        self.events.append(
            {**details, "severity": severity, "source": source, "event_type": event_type}
        )

    def close(self) -> None:
        pass

    def stats(self) -> dict[str, int]:
        return {}

    def of(self, event_type: str) -> list[dict[str, object]]:
        return [e for e in self.events if e["event_type"] == event_type]


@pytest.fixture
def events() -> Iterator[Recorder]:
    recorder = Recorder()
    axiom.init("competition-coordinator", client=recorder)
    yield recorder
    axiom.reset()
    assert {e["event_type"] for e in recorder.events} <= EVENT_TYPES
    assert all(e["competition"] == "torchlean" for e in recorder.events)


def test_planning_deciding_and_promoting_are_reported_per_board(store, config, events):
    repo, worker = setup(store, config)
    sid = accepted(store, config, "alice")
    ids = repo.plan(config)
    planned = events.of("evaluation_planned")
    assert {e["evaluation_id"] for e in planned} == set(ids)
    assert {e["board"] for e in planned} == {"mlp", "resnet"}
    assert all(e["submission_id"] == sid and e["hotkey"] == "alice" for e in planned)
    assert all(e["generation"] == 1 and e["jobs"] == 1 and e["reused_jobs"] == 0 for e in planned)

    drain(worker)
    assert repo.aggregate(config) == 2
    decided = events.of("evaluation_decided")
    assert {(e["board"], e["outcome"], e["severity"]) for e in decided} == {
        ("mlp", "qualifies", "info"),
        ("resnet", "qualifies", "info"),
    }
    assert all(e["guard_passed"] is True and e["score"] < 1 for e in decided)
    promoted = events.of("champion_promoted")
    with db.session_scope(store.sessions) as s:
        promotions = {p.board: p.id for p in s.scalars(select(m.Promotion))}
    assert {e["board"]: e["promotion_id"] for e in promoted} == promotions
    assert all(e["reference_promotion_id"] is None for e in promoted)


def test_a_candidate_failure_is_a_warning_and_promotes_nobody(store, config, events):
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    while job := worker.claim("local", "a" * 64, "worker"):
        assert worker.fail(job.id, job.owner, job.token, {"kind": "candidate", "reason": "crash"})
    assert repo.aggregate(config) == 2
    decided = events.of("evaluation_decided")
    assert {(e["outcome"], e["severity"], e["reason"]) for e in decided} == {
        ("benchmark-failed", "warning", "crash")
    }
    assert events.of("champion_promoted") == []


def test_stale_and_fenced_decisions_are_not_reported(store, config, events):
    repo, _ = setup(store, config)
    accepted(store, config, "alice")
    ids = repo.plan(config)
    assert repo.finalize(ids[0], config, {"outcome": "qualifies", "score": 0.9})
    # Measured against a reference that is no longer the champion: superseded, not decided.
    with db.session_scope(store.sessions) as s:
        e = s.get(m.Evaluation, ids[1])
        s.add(m.Promotion(board=e.board, evaluation_id=e.id))
    assert not repo.finalize(ids[1], config, {"outcome": "qualifies"})
    # A coordinator that lost leadership writes nothing, so it reports nothing.
    EvaluationsDb(store.sessions).set_leader("replacement")
    with pytest.raises(RuntimeError, match="leadership"):
        repo.plan(config)
    assert [e["evaluation_id"] for e in events.of("evaluation_decided")] == [ids[0]]
    assert len(events.of("evaluation_planned")) == 2


def test_unreadable_results_are_quarantined_as_errors(store, config, events):
    repo, worker = setup(store, config)
    sid = accepted(store, config)
    repo.plan(config)
    while job := worker.claim("local", "a" * 64, "worker"):
        with store.engine.begin() as c:
            c.execute(
                text("SELECT public.complete(:id,:owner,:token,CAST(:payload AS jsonb))"),
                {"id": job.id, "owner": job.owner, "token": job.token, "payload": "{}"},
            )
    assert repo.aggregate(config) == 0
    quarantined = events.of("evaluation_quarantined")
    assert {e["board"] for e in quarantined} == {"mlp", "resnet"}
    assert all(e["severity"] == "error" and e["submission_id"] == sid for e in quarantined)
    assert events.of("evaluation_decided") == []


def test_operator_decisions_name_what_changed_and_why(store, config, events):
    repo, worker = setup(store, config)
    accepted(store, config, "alice")
    repo.plan(config)
    drain(worker)
    repo.aggregate(config)
    promotion = events.of("champion_promoted")[0]
    repo.disqualify(promotion["promotion_id"], "copied kernel", config)
    assert events.of("promotion_disqualified") == [
        {
            "competition": "torchlean",
            "promotion_id": promotion["promotion_id"],
            "board": promotion["board"],
            "reason": "copied kernel",
            "severity": "warning",
            "source": "competition-coordinator",
            "event_type": "promotion_disqualified",
        }
    ]
    with db.session_scope(store.sessions) as s:
        digest = s.get(m.Submission, promotion["submission_id"]).source_sha256
    repo.set_disqualification(digest, True, "plagiarism", "operator", config)
    (event,) = events.of("source_disqualification_set")
    assert (event["source_hash"], event["active"], event["actor"]) == (digest, True, "operator")


def test_publication_changes_report_kings_and_pause_transitions(events):
    def publication(paused, kings):
        return {
            "version": "tensor-top2-v1",
            "boards": {
                "mlp": {"paused": paused, "kings": [{"hotkey": k} for k in kings]},
                "resnet": {"paused": False, "kings": []},
            },
        }

    first = publication(True, [])
    report_publication(first, None)
    report_publication(publication(False, ["bob", "alice"]), first)
    published = events.of("rewards_published")
    assert [p["boards"]["mlp"] for p in published] == [
        {"paused": True, "kings": []},
        {"paused": False, "kings": ["bob", "alice"]},
    ]
    assert [(e["event_type"], e["board"]) for e in events.events if "board" in e] == [
        ("board_paused", "mlp"),
        ("board_resumed", "mlp"),
    ]


@pytest.mark.parametrize(
    ("exit_code", "outcome", "severity"),
    [(0, "proved", "info"), (1, "rejected", "warning"), (2, "retry", "error")],
)
def test_proof_outcomes_separate_the_miners_failures_from_ours(
    events, exit_code, outcome, severity
):
    sub = SimpleNamespace(id=7, hotkey="alice", source_sha256="c" * 64)
    report_proof(sub, {"exit_code": exit_code, "stage": "build", "reason": "x" * 3000}, 12.345)
    (event,) = events.of("proof_checked")
    assert (event["outcome"], event["severity"], event["seconds"]) == (outcome, severity, 12.3)
    assert len(str(event["reason"])) == 2000


@pytest.mark.parametrize(
    ("error", "severity"),
    [
        (None, "info"),
        ({"kind": "candidate", "reason": "segfault"}, "warning"),
        ({"kind": "reference", "reason": "slow reference"}, "error"),
        ({"kind": "infrastructure", "reason": "disk full"}, "error"),
    ],
)
def test_benchmark_job_outcomes(events, error, severity):
    job = SimpleNamespace(
        id=3,
        environment="amd64",
        request={"seed": 42},
        candidate_hash="c" * 64,
        reference_hash="r" * 64,
    )
    report_job(job, error, 1.0)
    (event,) = events.of("benchmark_job_finished")
    assert event["severity"] == severity
    assert event["outcome"] == ("completed" if error is None else "failed")
    assert event["error_kind"] == (error or {}).get("kind")
