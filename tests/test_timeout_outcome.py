"""A candidate that runs out of time is decided `timed-out`, and its job keeps the evidence."""

from __future__ import annotations

from alembic import command
from sqlalchemy import select
from test_db_schema import migration_config
from test_tensor_events import events  # noqa: F401 - fixture
from test_tensor_workflow import accepted, setup

import db
from db import models as m

TIMEOUT = {
    "phase": "benchmark",
    "limit": "invocation",
    "side": "candidate",
    "running": "candidate",
    "budget_seconds": 1200,
    "elapsed_seconds": 512.5,
    "stopped_after_seconds": 300.0,
    "runs": {
        "reference": {"finished": 2, "mean_seconds": 120.5},
        "candidate": {"finished": 1, "mean_seconds": 290.1},
    },
    "allowance_seconds": 300,
    "cell": "s0|mlp:b1:normal",
    "cells_finished": 3,
    "cells_total": 12,
    "finished": {"s0|mlp:b1:relu": [100, 7]},
}
PARTIAL = {
    "build_seconds": 40.0,
    "rounds": [{"reference_first": True, "cells": {}}],
    "rounds_planned": 11,
    "calibration": {"s0|mlp:b1:relu": 90},
}


def fail_all(worker, failure):
    while job := worker.claim("local", "a" * 64, "worker"):
        assert worker.fail(job.id, job.owner, job.token, failure)


def decisions(store):
    with db.session_scope(store.sessions) as s:
        return {
            e.board: (e.decision, ej.environment, ej.seed)
            for e, ej in s.execute(select(m.Evaluation, m.EvaluationJob).join(m.EvaluationJob))
        }


def test_a_candidate_timeout_is_timed_out_and_keeps_its_evidence(store, config, events):  # noqa: F811
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    reason = "benchmark-timeout: candidate invocation exceeded its 300s time allowance"
    failure = {"kind": "candidate", "reason": reason, "timeout": TIMEOUT, "partial": PARTIAL}
    fail_all(worker, failure | {"unrelated": "dropped"})
    assert repo.aggregate(config) == 2
    decided = events.of("evaluation_decided")
    assert {(e["outcome"], e["severity"], e["reason"]) for e in decided} == {
        ("timed-out", "info", reason)
    }
    for decision, environment, seed in decisions(store).values():
        assert decision == {
            "outcome": "timed-out",
            "reason": reason,
            "score": None,
            "timeout": {k: v for k, v in TIMEOUT.items() if k not in ("side", "finished")}
            | {
                "environment": environment,
                "seed": seed,
                "rounds_completed": 1,
                "rounds_planned": 11,
            },
        }
    with db.session_scope(store.sessions) as s:
        errors = [j.error for j in s.scalars(select(m.BenchmarkJob))]
    assert errors == [failure, failure]


def test_any_other_candidate_failure_is_still_benchmark_failed(store, config, events):  # noqa: F811
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    fail_all(worker, {"kind": "candidate", "reason": "bench exit 1: panic", "partial": PARTIAL})
    assert repo.aggregate(config) == 2
    assert {(e["outcome"], e["severity"]) for e in events.of("evaluation_decided")} == {
        ("benchmark-failed", "warning")
    }
    assert all("timeout" not in d for d, _, _ in decisions(store).values())


def test_oversized_evidence_is_dropped_not_the_failure(store, config):
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    huge = PARTIAL | {"calibration": {"x": "y" * 1_100_000}}
    fail_all(worker, {"kind": "candidate", "reason": "r", "timeout": TIMEOUT, "partial": huge})
    with db.session_scope(store.sessions) as s:
        errors = [j.error for j in s.scalars(select(m.BenchmarkJob))]
    assert (
        errors == [{"kind": "candidate", "reason": "r", "timeout": TIMEOUT, "truncated": True}] * 2
    )


def test_the_migration_relabels_timeouts_decided_before_it(store, config, monkeypatch):
    # Decided under the old fail(): kind and reason only, a timeout known by its reason alone.
    monkeypatch.setenv("DATABASE_URL", store.engine.url.render_as_string(hide_password=False))
    cfg = migration_config()
    command.downgrade(cfg, "tensor_0002")
    try:
        repo, worker = setup(store, config)
        accepted(store, config)
        repo.plan(config)
        jobs = []
        while job := worker.claim("local", "a" * 64, "worker"):
            jobs.append(job)
        run_seconds = jobs[0].request["timing"]["run_seconds"]
        budget = jobs[1].request["timing"]["benchmark_seconds"]
        reasons = [
            f"benchmark-timeout: candidate invocation exceeded its {run_seconds:g}s time allowance",
            "bench exit 1: panic",
        ]
        for job, reason in zip(jobs, reasons, strict=True):
            failure = {"kind": "candidate", "reason": reason, "partial": PARTIAL}
            assert worker.fail(job.id, job.owner, job.token, failure)
        assert repo.aggregate(config) == 2
    finally:
        command.upgrade(cfg, "head")
    with db.session_scope(store.sessions) as s:
        by_job = {
            ej.job_id: (e.decision, ej.environment, ej.seed)
            for e, ej in s.execute(select(m.Evaluation, m.EvaluationJob).join(m.EvaluationJob))
        }
        errors = [j.error for j in s.scalars(select(m.BenchmarkJob).order_by(m.BenchmarkJob.id))]
    timed_out, crashed = by_job[jobs[0].id], by_job[jobs[1].id]
    decision, environment, seed = timed_out
    assert decision["outcome"] == "timed-out" and decision["reason"] == reasons[0]
    assert decision["timeout"] == {
        "phase": "benchmark",
        "limit": "invocation",
        "allowance_seconds": run_seconds,
        "budget_seconds": jobs[0].request["timing"]["benchmark_seconds"],
        "environment": environment,
        "seed": seed,
    }
    assert crashed[0]["outcome"] == "benchmark-failed" and "timeout" not in crashed[0]
    assert budget > 0
    # The old fail() kept nothing else; that is not recoverable.
    assert all(set(e) == {"kind", "reason"} for e in errors)


def test_a_budget_timeout_from_the_old_reason_is_a_budget_limit(store, config, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", store.engine.url.render_as_string(hide_password=False))
    cfg = migration_config()
    command.downgrade(cfg, "tensor_0002")
    try:
        repo, worker = setup(store, config)
        accepted(store, config)
        repo.plan(config)
        fail_all(
            worker,
            {
                "kind": "candidate",
                "reason": "benchmark-timeout: candidate invocation exceeded its 25.7093s time "
                "allowance",
            },
        )
        assert repo.aggregate(config) == 2
    finally:
        command.upgrade(cfg, "head")
    for decision, _, _ in decisions(store).values():
        assert decision["outcome"] == "timed-out"
        assert decision["timeout"]["limit"] == "budget"
        assert decision["timeout"]["allowance_seconds"] == 25.7093


def test_a_reference_reason_on_a_candidate_decision_names_the_candidate(store, config, monkeypatch):
    # The shared budget ran out while the reference ran, and was blamed on the slower candidate;
    # the old reason still named the reference.
    monkeypatch.setenv("DATABASE_URL", store.engine.url.render_as_string(hide_password=False))
    cfg = migration_config()
    command.downgrade(cfg, "tensor_0002")
    try:
        repo, worker = setup(store, config)
        accepted(store, config)
        repo.plan(config)
        reason = "benchmark-timeout: reference invocation exceeded its 3.43003s time allowance"
        fail_all(worker, {"kind": "candidate", "reason": reason})
        assert repo.aggregate(config) == 2
    finally:
        command.upgrade(cfg, "head")
    with db.session_scope(store.sessions) as s:
        budget = s.scalars(select(m.BenchmarkJob)).first().request["timing"]["benchmark_seconds"]
    for decision, _, _ in decisions(store).values():
        assert decision["outcome"] == "timed-out"
        assert decision["reason"] == (
            "benchmark-timeout: candidate runs take longer than the reference's, which was "
            f"stopped when the shared {budget:g}s benchmark budget ran out"
        )
        assert decision["timeout"]["limit"] == "budget"
