"""Fresh-schema intake, SQL leases, distributed planning, reconciliation and rewards."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from comparisons import board_request
from config import Config, digest, measurement_hash
from rewards import score
from sqlalchemy import func, select, text
from test_exact_competition import result
from workload import RealWorkloadSpec

import db
from conftest import register
from db import models as m
from db.evaluations import (
    EvaluationsDb,
    _sample_plan_prefix,
    _synthetic_plan_prefix,
    select_real_sample_plan,
    submission_assignment,
)
from db.jobs import WorkerDb


def setup(store, config):
    repo = EvaluationsDb(store.sessions)
    repo.set_leader(uuid.uuid4().hex)
    repo.reconcile(config)
    return repo, WorkerDb(store.sessions)


def editable_config(config):
    raw = copy.deepcopy(config.__dict__)
    raw["benchmark"] = {
        "suites": list(raw.pop("suites")),
        "synthetic_cells": list(raw.pop("synthetic_cells")),
        "real_workloads": [workload.json() for workload in raw.pop("real_workloads")],
        "suite_weights": raw.pop("suite_weights"),
        "reuse_board_timings": raw.pop("reuse_board_timings", True),
        "reuse_completed_jobs": raw.pop("reuse_completed_jobs", True),
    }
    return raw


def test_coordinator_sample_plan_is_balanced_and_reproducible():
    workloads = [
        {"id": "digits-mlp", "sample_count": 4, "samples_per_run": 1},
        {"id": "har-resnet", "sample_count": 20, "samples_per_run": 1},
    ]
    timing = {"rounds": 3, "inner": 2}
    first = select_real_sample_plan(workloads, timing, rng=random.Random(19))
    second = select_real_sample_plan(workloads, timing, rng=random.Random(19))
    assert first == second
    for workload in workloads:
        groups = [samples for round_samples in first[workload["id"]] for samples in round_samples]
        assert len(groups) == timing["rounds"] * timing["inner"]
        assert all(len(samples) == workload["samples_per_run"] for samples in groups)
        flattened = [sample for group in groups for sample in group]
        assert len(set(flattened)) == min(workload["sample_count"], len(flattened))
        assert (
            max(flattened.count(sample) for sample in set(flattened))
            - min(flattened.count(sample) for sample in set(flattened))
            <= 1
        )


def test_submission_assignment_resolves_shared_seed_and_sample_plan():
    workloads = [{"id": "digits-mlp", "sample_count": 4, "samples_per_run": 1}]
    timing = {"rounds": 3, "inner": 2}
    first = submission_assignment(workloads, timing, fresh_seed_count=2, rng=random.Random(23))
    second = submission_assignment(workloads, timing, fresh_seed_count=2, rng=random.Random(23))

    assert first == second
    assert len(set(first["seed_values"])) == 2
    assert len(first["real_sample_plan"]["digits-mlp"]) == timing["rounds"]
    for scored_round, warmup_round in zip(
        first["real_sample_plan"]["digits-mlp"], first["real_warmup_plan"]["digits-mlp"]
    ):
        for scored, warmup in zip(scored_round, warmup_round):
            assert scored[0] != warmup[0]
    preserved = submission_assignment(
        workloads,
        timing,
        fresh_seed_count=2,
        previous_seed_values=first["seed_values"],
        rng=random.Random(99),
    )
    assert preserved["seed_values"] == first["seed_values"]


def test_submission_assignment_resolves_shared_synthetic_inputs():
    timing = {"rounds": 3, "inner": 2}
    cells = ["mlp:b1:normal", "mlp:b1:wide"]
    first = submission_assignment(
        [], timing, workload_seeds=[42], synthetic_cells=cells, rng=random.Random(23)
    )
    second = submission_assignment(
        [], timing, workload_seeds=[42], synthetic_cells=cells, rng=random.Random(23)
    )

    assert first == second
    plan = first["synthetic_input_plan"]
    assert set(plan) == {f"s{i}|{cell}" for i in range(6) for cell in cells}
    flattened = [
        seed for rounds in plan.values() for group in rounds for values in group for seed in values
    ]
    assert len(flattened) == len(set(flattened))
    warmups = first["synthetic_warmup_plan"]
    warmup_seeds = [
        seed
        for rounds in warmups.values()
        for group in rounds
        for values in group
        for seed in values
    ]
    assert len(warmup_seeds) == len(set(warmup_seeds))
    assert not set(flattened) & set(warmup_seeds)
    prefix = _synthetic_plan_prefix(plan, [42], ["mlp:b1:wide"], {"rounds": 2, "inner": 1})
    assert set(prefix) == {f"s{i}|mlp:b1:wide" for i in range(6)}
    assert all(
        len(rounds) == 2 and all(len(group) == 1 for group in rounds) for rounds in prefix.values()
    )


def test_shared_sample_plan_prefix_preserves_order_and_board_round_shape():
    shared = {"digits-mlp": [[[5], [1]], [[8], [4]], [[2], [9]]]}
    assert _sample_plan_prefix(shared, {"rounds": 2, "inner": 1}) == {"digits-mlp": [[[5]], [[1]]]}
    assert _sample_plan_prefix(shared, {"rounds": 2, "inner": 2}) == {
        "digits-mlp": [[[5], [1]], [[8], [4]]]
    }


def accepted(store, config, hotkey="alice", content=b"candidate"):
    with db.session_scope(store.sessions) as s:
        uid = s.scalar(select(func.count()).select_from(m.Registration))
    register(store, hotkey, uid=uid)
    sid = store.submissions.add(hotkey, content)
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Submission, sid)
        row.state, row.verification_identity = "proved", config.proof_identity
        row.proof_report = {"passed": True}
    return sid


def drain(worker, ratio=0.9, env="local", identity="a" * 64):
    jobs = []
    while job := worker.claim(env, identity, "worker"):
        assert worker.complete(job.id, job.owner, job.token, result(job.request, ratio))
        jobs.append(job)
    return jobs


def test_intake_latest_registration_and_status_independent_entitlement(store):
    first = register(store, "alice", uid=0)
    register(store, "alice", uid=1, block=2000)
    sid = store.submissions.add("alice", b"source")
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Submission, sid)
        assert row.registration_id != first
        row.state = "rejected"
    with pytest.raises(db.NoSlot, match="used"):
        store.submissions.add("alice", b"different")
    register(store, "alice", uid=2, block=3000)
    with pytest.raises(ValueError, match="duplicate"):
        store.submissions.add("alice", b"source")
    new = store.submissions.add("alice", b"different")
    assert new != sid


def test_concurrent_uploads_cannot_spend_same_registration(store):
    register(store, "alice")

    def upload(content):
        try:
            return store.submissions.add("alice", content)
        except db.NoSlot:
            return None

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(upload, (b"one", b"two")))
    assert sum(i is not None for i in ids) == 1


def test_cutoff_strict_and_utf8_and_hash_guard(store):
    register(store, "alice")
    with db.session_scope(store.sessions) as s:
        cutoff = s.scalar(select(m.Registration.block_date))
    with pytest.raises(db.NoSlot):
        store.submissions.add("alice", b"source", cutoff=cutoff)
    with pytest.raises(UnicodeDecodeError):
        store.submissions.add("alice", b"\xff")
    with pytest.raises(Exception, match="source hash mismatch"):
        with store.engine.begin() as c:
            c.execute(
                text("INSERT INTO sources(sha256,content) VALUES(:h,:b)"),
                {"h": "a" * 64, "b": b"wrong"},
            )


def test_proof_retry_fencing_and_copies(store, config):
    repo, _ = setup(store, config)
    register(store, "alice")
    sid = store.submissions.add("alice", b"source")
    proof = repo.submissions.claim_proof("one", config.proof_identity, 30)
    assert proof.id == sid and proof.state == "running"
    assert not repo.submissions.finish_proof(
        sid, "wrong", config.proof_identity, {"exit_code": 0}, 1
    )
    assert repo.submissions.finish_proof(
        sid, proof.token, config.proof_identity, {"exit_code": 2, "reason": "infra"}, 0.001
    )
    with pytest.raises(db.NoSlot):
        store.submissions.add("alice", b"second")
    with db.session_scope(store.sessions) as s:
        s.get(m.Submission, sid).next_attempt_at = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    proof = repo.submissions.claim_proof("two", config.proof_identity, 30)
    assert repo.submissions.finish_proof(
        sid, proof.token, config.proof_identity, {"exit_code": 0, "passed": True}, 1
    )
    # Another hotkey's copy is rejected before its proof, not served from the proof cache.
    register(store, "bob", uid=1)
    other = store.submissions.add("bob", b"source")
    assert repo.submissions.claim_proof("two", config.proof_identity, 30) is None
    with db.session_scope(store.sessions) as s:
        assert (s.get(m.Submission, other).state, s.get(m.Submission, other).stage) == (
            "rejected",
            "intake",
        )


def test_claim_races_stale_completions_and_atomic_payout(store, config):
    repo, _ = setup(store, config)
    sid = accepted(store, config)
    assert len(repo.plan(config)) == 2

    def claim(owner):
        return WorkerDb(store.sessions).claim("local", "a" * 64, owner)

    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(claim, ("one", "two")))
    assert len({j.id for j in jobs}) == 2
    for job in jobs:
        worker = WorkerDb(store.sessions)
        worker.requests[job.id] = job.request
        assert not worker.complete(job.id, job.owner, "stale", result(job.request))
        assert worker.heartbeat(job.id, job.owner, job.token)
        assert worker.complete(job.id, job.owner, job.token, result(job.request))
        assert not worker.complete(job.id, job.owner, job.token, result(job.request))
    assert repo.aggregate(config) == 2
    assert repo.aggregate(config) == 0
    with db.session_scope(store.sessions) as s:
        assert s.scalar(select(func.count()).select_from(m.Promotion)) == 2
        assert {e.submission_id for e in s.scalars(select(m.Evaluation))} == {sid}
        state = s.get(m.CoordinatorState, 1)
        assert state.publication_hash
    assert score(store.publications.publication()).weights == pytest.approx({"alice": 0.8})


def test_expired_and_failing_jobs_do_not_starve_other_board(store, config):
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    dead = worker.claim("local", "a" * 64, "dead")
    with db.session_scope(store.sessions) as s:
        s.get(m.BenchmarkJob, dead.id).lease_until = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    assert not worker.complete(dead.id, dead.owner, dead.token, result(dead.request))
    healthy = worker.claim("local", "a" * 64, "healthy")
    assert healthy.id != dead.id
    assert worker.complete(healthy.id, healthy.owner, healthy.token, result(healthy.request))
    assert repo.aggregate(config) == 1
    with db.session_scope(store.sessions) as s:
        s.get(m.BenchmarkJob, dead.id).due_at = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    retry = worker.claim("local", "a" * 64, "retry")
    assert retry.token != dead.token
    assert not worker.heartbeat(dead.id, dead.owner, dead.token)
    assert worker.fail(
        retry.id, retry.owner, retry.token, {"kind": "reference", "reason": "build failed"}
    )
    repo.publish(config)
    assert any(b["paused"] for b in store.publications.publication()["boards"].values())
    assert len(repo.plan(config)) == 0  # retains same queued challenger


def test_hardware_changes_reuse_completed_evidence_with_links(store, config):
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    drain(worker)
    raw = editable_config(config)
    raw["environments"]["local"]["weight"] = 0.5
    raw["environments"]["arm"] = {"identity": "d" * 64, "weight": 0.5}
    changed = Config.parse(raw)
    repo.reconcile(changed)
    repo.plan(changed)
    with db.session_scope(store.sessions) as s:
        assert s.scalar(select(func.count()).select_from(m.BenchmarkJob)) == 4
        assert s.scalar(select(func.count()).select_from(m.EvaluationJob)) == 6
        assert not any("reuse_job_id" in j.request for j in s.scalars(select(m.BenchmarkJob)))
    assert repo.aggregate(changed) == 0
    repo.reconcile(config)
    repo.plan(config)
    assert repo.aggregate(config) == 2


def test_activation_reactivation_boundary_and_active_config_keeps_fifo(store, config):
    repo, worker = setup(store, config)
    first = accepted(store, config)
    repo.plan(config)
    raw = editable_config(config)
    raw["boards"]["new"] = copy.deepcopy(raw["boards"]["mlp"])
    raw["rewards"]["board_weights"] = {"mlp": 0.25, "resnet": 0.25, "new": 0.5}
    expanded = Config.parse(raw)
    repo.reconcile(expanded)
    repo.plan(expanded)
    with db.session_scope(store.sessions) as s:
        assert s.scalar(select(m.Evaluation).where(m.Evaluation.board == "new")) is None
    drain(worker)
    assert repo.aggregate(expanded) == 2
    second = accepted(store, config, "bob", b"second")
    assert len(repo.plan(expanded)) == 3
    repo.reconcile(config)
    drain(worker)
    repo.aggregate(config)
    accepted(store, config, "carol", b"during inactive")
    repo.reconcile(expanded)
    repo.plan(expanded)
    with db.session_scope(store.sessions) as s:
        progress = s.get(m.BoardProgress, "new")
        assert progress.activation_boundary > second > first
        assert not s.scalar(
            select(m.Evaluation).where(m.Evaluation.board == "new", m.Evaluation.state == "pending")
        )


def test_stale_reference_and_leadership_fence(store, config):
    repo, worker = setup(store, config)
    accepted(store, config)
    ids = repo.plan(config)
    drain(worker)
    assert repo.finalize(ids[0], config, {"outcome": "qualifies", "score": 0.9})
    # A pending comparison against a predecessor cannot promote.
    with db.session_scope(store.sessions) as s:
        e = s.get(m.Evaluation, ids[1])
        s.add(m.Promotion(board=e.board, evaluation_id=e.id))
    assert not repo.finalize(ids[1], config, {"outcome": "qualifies"})
    other = EvaluationsDb(store.sessions)
    other.set_leader("replacement")
    with pytest.raises(RuntimeError, match="leadership"):
        repo.plan(config)


def test_disqualification_preserves_history_and_updates_publication(store, config):
    repo, worker = setup(store, config)
    for i in range(3):
        accepted(store, config, f"miner-{i}", str(i).encode())
        repo.plan(config)
        drain(worker)
        assert repo.aggregate(config) == 2
    with db.session_scope(store.sessions) as s:
        pid = s.scalar(
            select(m.Promotion.id)
            .where(m.Promotion.board == "mlp")
            .order_by(m.Promotion.id.desc())
            .limit(1)
        )
    repo.disqualify(pid, "operator audit", config)
    assert [k["hotkey"] for k in store.publications.publication()["boards"]["mlp"]["kings"]] == [
        "miner-1",
        "miner-0",
    ]
    with db.session_scope(store.sessions) as s:
        assert s.scalar(select(func.count()).select_from(m.Promotion)) == 6


def test_fresh_seeds_persist_across_changed_plan(store, config):
    raw = editable_config(config)
    raw.update(seed_mode="fresh", seeds=(), seed_count=2)
    fresh = Config.parse(raw)
    repo, _ = setup(store, fresh)
    accepted(store, fresh)
    repo.plan(fresh)
    with db.session_scope(store.sessions) as s:
        old = {e.board: e.plan["seed_values"] for e in s.scalars(select(m.Evaluation))}
    assert len({tuple(values) for values in old.values()}) == 1
    raw["admission"]["bar"] = 0.95
    changed = Config.parse(raw)
    repo.reconcile(changed)
    repo.plan(changed)
    with db.session_scope(store.sessions) as s:
        new = {
            e.board: e.plan["seed_values"]
            for e in s.scalars(select(m.Evaluation).where(m.Evaluation.state == "pending"))
        }
    assert new == old and all(len(set(v)) == 2 for v in new.values())


def test_overall_and_family_plans_share_seed_and_real_sample_draws(store, config):
    boards = copy.deepcopy(config.boards)
    boards["overall"] = {
        "models": {"mlp": 0.5, "resnet": 0.5},
        "reference": copy.deepcopy(boards["mlp"]["reference"]),
        "timing": copy.deepcopy(boards["mlp"]["timing"]),
    }
    rewards = copy.deepcopy(config.rewards)
    rewards["board_weights"] = {"mlp": 1 / 3, "resnet": 1 / 3, "overall": 1 / 3}
    workload = RealWorkloadSpec(
        id="digits-mlp",
        family="mlp",
        model="fixture-mlp",
        dataset="digits",
        adapter="image-zoo-v1",
        weights_path="local/weights.bin",
        weights_sha256=hashlib.sha256(b"weights").hexdigest(),
        samples_path="local/samples.bin",
        samples_sha256=hashlib.sha256(b"samples").hexdigest(),
        asset_revision="test-fixture-v1",
        sample_count=17,
        sample_words=16,
        samples_per_run=1,
    )
    shared = replace(
        config,
        boards=boards,
        rewards=rewards,
        seed_mode="fresh",
        seeds=(),
        seed_count=2,
        suites=("synthetic", "real"),
        real_workloads=(workload,),
    )
    repo, _ = setup(store, shared)
    accepted(store, shared)
    assert len(repo.plan(shared)) == 3
    with db.session_scope(store.sessions) as s:
        plans = {
            evaluation.board: evaluation.plan for evaluation in s.scalars(select(m.Evaluation))
        }
    assert plans["mlp"]["seed_values"] == plans["overall"]["seed_values"]
    assert (
        plans["mlp"]["real_sample_plan"]["digits-mlp"]
        == plans["overall"]["real_sample_plan"]["digits-mlp"]
    )
    assert plans["resnet"]["real_sample_plan"] is None


def test_overall_and_family_evaluations_share_one_leased_comparison_bundle(store, config, tmp_path):
    boards = copy.deepcopy(config.boards)
    overall_source = b"overall reference"
    overall_path = tmp_path / "overall.lean"
    overall_path.write_bytes(overall_source)
    boards["overall"] = {
        "models": {"mlp": 0.5, "resnet": 0.5},
        "reference": {
            "path": str(overall_path),
            "sha256": hashlib.sha256(overall_source).hexdigest(),
        },
        "timing": copy.deepcopy(boards["mlp"]["timing"]),
    }
    rewards = copy.deepcopy(config.rewards)
    rewards["board_weights"] = {"mlp": 1 / 3, "resnet": 1 / 3, "overall": 1 / 3}
    shared = replace(config, boards=boards, rewards=rewards)
    repo, worker = setup(store, shared)
    accepted(store, shared)

    evaluation_ids = repo.plan(shared)
    assert len(evaluation_ids) == 3
    with db.session_scope(store.sessions) as s:
        jobs = list(s.scalars(select(m.BenchmarkJob)))
        links = list(s.scalars(select(m.EvaluationJob)))
    assert len(jobs) == 1
    assert len(links) == 3
    job = worker.claim("local", "a" * 64, "shared-worker")
    assert job is not None
    assert set(job.request["comparisons"]) == {"mlp", "resnet", "overall"}
    candidate, references = worker.sources_bundle(job.id)
    assert candidate == b"candidate"
    assert set(references) == {
        config.boards["mlp"]["reference"]["sha256"],
        hashlib.sha256(overall_source).hexdigest(),
    }

    bundle_result = {
        "bundle_hash": digest(job.request),
        "comparisons": {
            board: result(board_request(job.request, board)) for board in job.request["comparisons"]
        },
    }
    assert worker.complete(job.id, job.owner, job.token, bundle_result)
    ready = repo.ready()
    assert {evaluation.board for evaluation, _ in ready} == {"mlp", "resnet", "overall"}
    assert all(len(pairs) == 1 for _, pairs in ready)
    assert all(pairs[0][1]["request_hash"] == measurement_hash(pairs[0][0]) for _, pairs in ready)


def test_independent_board_jobs_isolate_timeout_and_keep_partial_evidence(store, config, tmp_path):
    boards = copy.deepcopy(config.boards)
    overall_source = b"overall reference"
    overall_path = tmp_path / "overall-independent.lean"
    overall_path.write_bytes(overall_source)
    boards["overall"] = {
        "models": {"mlp": 0.5, "resnet": 0.5},
        "reference": {
            "path": str(overall_path),
            "sha256": hashlib.sha256(overall_source).hexdigest(),
        },
        "timing": copy.deepcopy(boards["mlp"]["timing"]),
    }
    rewards = copy.deepcopy(config.rewards)
    rewards["board_weights"] = {"mlp": 1 / 3, "resnet": 1 / 3, "overall": 1 / 3}
    shared = replace(
        config,
        boards=boards,
        rewards=rewards,
        reuse_board_timings=False,
        reuse_completed_jobs=False,
    )
    repo, worker = setup(store, shared)
    accepted(store, shared)

    evaluation_ids = repo.plan(shared)
    with db.session_scope(store.sessions) as s:
        jobs = list(s.scalars(select(m.BenchmarkJob).order_by(m.BenchmarkJob.id)))
        links = list(
            s.scalars(
                select(m.EvaluationJob).order_by(
                    m.EvaluationJob.evaluation_id, m.EvaluationJob.job_id
                )
            )
        )
        evaluations = list(s.scalars(select(m.Evaluation).order_by(m.Evaluation.id)))
    assert len(evaluation_ids) == len(jobs) == len(links) == 3
    assert [evaluation.board for evaluation in evaluations] == ["mlp", "resnet", "overall"]
    assert all("comparisons" not in job.request for job in jobs)
    assert [link.evaluation_id for link in links] == evaluation_ids

    first = worker.claim("local", "a" * 64, "family-worker")
    assert first.id == jobs[0].id
    assert worker.fail(
        first.id,
        first.owner,
        first.token,
        {
            "kind": "candidate",
            "reason": "benchmark-timeout: candidate exceeded the board budget",
            "timeout": {"phase": "benchmark", "budget_seconds": 3600, "cell": "mlp:b1:normal"},
            "partial": {"rounds": [{"cells": {"mlp:b1:normal": {"candidate_ns": 9}}}]},
        },
    )
    assert repo.aggregate(shared) == 1
    with db.session_scope(store.sessions) as s:
        mlp = s.scalar(select(m.Evaluation).where(m.Evaluation.board == "mlp"))
        assert mlp.state == "completed"
        assert mlp.decision["outcome"] == "timed-out"
        assert s.get(m.BenchmarkJob, first.id).error["partial"]["rounds"]
        assert (
            s.scalar(
                select(func.count())
                .select_from(m.Evaluation)
                .where(
                    m.Evaluation.board.in_(("resnet", "overall")), m.Evaluation.state == "pending"
                )
            )
            == 2
        )
    assert worker.claim("local", "a" * 64, "next-family").id == jobs[1].id


def test_execute_only_worker_role(store, config):
    repo, _ = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    role = "tensor_worker_test"
    with store.engine.begin() as c:
        c.exec_driver_sql(f"CREATE ROLE {role}")
        grant = (
            Path(__file__).resolve().parents[1] / "deploy/platform/benchmark_worker.sql"
        ).read_text()
        c.execute(text(grant.replace("benchmark_worker", role)))
    engine = db.create_db_engine(store.engine.url)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def restrict(conn, record):
        conn.execute(f"SET ROLE {role}")

    try:
        worker = WorkerDb(db.session_factory(engine))
        job = worker.claim("local", "a" * 64, "restricted")
        a, b = worker.sources(job.id)
        assert a == b"candidate" and b == b"initial reference"
        assert worker.complete(job.id, job.owner, job.token, result(job.request))
        with engine.begin() as c:
            assert not c.scalar(
                text("SELECT has_table_privilege(current_user,'promotions','INSERT')")
            )
            assert not c.scalar(
                text("SELECT has_table_privilege(current_user,'benchmark_jobs','UPDATE')")
            )
    finally:
        engine.dispose()
        with store.engine.begin() as c:
            c.exec_driver_sql(f"DROP OWNED BY {role}")
            c.exec_driver_sql(f"DROP ROLE {role}")


def test_weight_poll_does_not_write_per_block_and_attempt_records_policy(store, config):
    from chain.types import MetagraphView, PollState, SubnetParams
    from workers.weight_setter import WeightSetterConfig, step

    setup(store, config)

    class Chain:
        block = 1000

        def current_block(self):
            return self.block

        def params(self, n):
            return SubnetParams(uid=1, tempo=360, weights_rate_limit=10)

        def poll(self, n, u):
            return PollState(self.block, 1000, 900)

        def metagraph(self, n):
            return MetagraphView((0, 1), {})

        def set_weights(self, *a):
            raise AssertionError("must remain dry-run")

    chain = Chain()
    setter = WeightSetterConfig(netuid=2, treasury_override=0)
    for _ in range(5):
        assert step(chain, store, setter).action == "wait"
        chain.block += 1
    with db.session_scope(store.sessions) as s:
        assert s.scalar(select(func.count()).select_from(m.WeightSet)) == 0
    chain.block = 1250
    assert step(chain, store, setter).action == "skip"
    assert step(chain, store, setter).weight_set_id is None
    with db.session_scope(store.sessions) as s:
        row = s.scalar(select(m.WeightSet))
        assert row.dry_run and row.publication["version"] == "tensor-top2-v1"
        assert row.publication_hash


def test_short_reference_retry_keeps_other_submissions_moving(store, config):
    repo, worker = setup(store, config)
    first = accepted(store, config)
    repo.plan(config)
    broken = worker.claim("local", "a" * 64, "worker")
    blocked_cells = set(broken.request["cells"])
    assert worker.fail(
        broken.id, broken.owner, broken.token, {"kind": "reference", "reason": "retry me"}
    )
    healthy = worker.claim("local", "a" * 64, "worker")
    assert set(healthy.request["cells"]) != blocked_cells
    assert worker.complete(healthy.id, healthy.owner, healthy.token, result(healthy.request))
    assert repo.aggregate(config) == 1
    for i in range(3):
        accepted(store, config, f"next-{i}", f"next-{i}".encode())
        repo.plan(config)
        # Make the failed head eligible before the newer healthy job. It gets one turn,
        # then its retry goes behind work that is already due.
        with db.session_scope(store.sessions) as s:
            s.get(m.BenchmarkJob, broken.id).due_at = dt.datetime(
                2000, 1, 1, tzinfo=dt.timezone.utc
            )
        retry = worker.claim("local", "a" * 64, "worker")
        assert retry.id == broken.id
        worker.fail(
            retry.id, retry.owner, retry.token, {"kind": "reference", "reason": "still broken"}
        )
        healthy = worker.claim("local", "a" * 64, "worker")
        assert healthy.id != broken.id
        worker.complete(healthy.id, healthy.owner, healthy.token, result(healthy.request))
        assert repo.aggregate(config) == 1
    with db.session_scope(store.sessions) as s:
        pending = s.scalar(select(m.Evaluation).where(m.Evaluation.state == "pending"))
        assert pending.submission_id == first


def test_reward_only_edit_changes_no_jobs_or_proofs(store, config):
    repo, worker = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    drain(worker)
    repo.aggregate(config)
    raw = editable_config(config)
    raw["rewards"]["rank_shares"] = [0.9, 0.1]
    changed = Config.parse(raw)
    repo.reconcile(changed)
    assert repo.plan(changed) == []
    with db.session_scope(store.sessions) as s:
        assert s.scalar(select(func.count()).select_from(m.BenchmarkJob)) == 2
        assert s.scalar(select(func.count()).select_from(m.Promotion)) == 2
    assert score(store.publications.publication()).weights == pytest.approx({"alice": 0.9})


def test_malformed_protocol_result_is_quarantined_not_a_miner_rejection(store, config):
    repo, worker = setup(store, config)
    sid = accepted(store, config)
    repo.plan(config)
    while job := worker.claim("local", "a" * 64, "worker"):
        # Simulate a worker that calls the SQL function without the Python validator.
        with store.engine.begin() as c:
            assert c.scalar(
                text("SELECT public.complete(:id,:owner,:token,CAST(:payload AS jsonb))"),
                {
                    "id": job.id,
                    "owner": job.owner,
                    "token": job.token,
                    "payload": '{"broken":true}',
                },
            )
    assert repo.aggregate(config) == 0
    with db.session_scope(store.sessions) as s:
        assert s.get(m.Submission, sid).state == "proved"
        assert all(e.state == "superseded" for e in s.scalars(select(m.Evaluation)))
        assert all(j.result == {"broken": True} for j in s.scalars(select(m.BenchmarkJob)))
    assert len(repo.plan(config)) == 2
    drain(worker)
    assert repo.aggregate(config) == 2


def test_unchanged_publication_is_not_rewritten(store, config):
    repo, _ = setup(store, config)
    with store.engine.connect() as c:
        before = c.scalar(text("SELECT xmin::text FROM coordinator_state"))
    repo.publish(config)
    with store.engine.connect() as c:
        assert c.scalar(text("SELECT xmin::text FROM coordinator_state")) == before


def test_database_url_escapes_password(monkeypatch):
    from sqlalchemy.engine import make_url

    from db.engine import database_url

    monkeypatch.delenv("DATABASE_URL", raising=False)
    password = "a:@/# b%"
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    assert make_url(database_url()).password == password


def test_per_device_preparation_budgets_survive_db_planning_and_admission(store, config):
    raw = editable_config(config)
    raw.pop("timing")
    for board in raw["boards"].values():
        board.pop("timing", None)
    raw["environments"] = {
        "dev": dict(weight=0.5, identity="a" * 64, architecture="amd64"),
        "arm": dict(weight=0.5, identity="d" * 64, architecture="arm64"),
    }
    configured = Config.parse(raw)
    repo, worker = setup(store, configured)
    accepted(store, configured)
    repo.plan(configured)
    with db.session_scope(store.sessions) as session:
        jobs = list(session.scalars(select(m.BenchmarkJob)))
        assert len(jobs) == 4
        for job in jobs:
            assert job.request["timing"]["benchmark_seconds"] == 360
            expected = (300, 120) if job.environment == "dev" else (960, 180)
            assert (
                job.request["timing"]["build_seconds"],
                job.request["timing"]["cached_build_seconds"],
            ) == expected
    drain(worker, env="dev", identity="a" * 64)
    drain(worker, env="arm", identity="d" * 64)
    assert repo.aggregate(configured) == 2
