"""Fresh-schema intake, SQL leases, distributed planning, reconciliation and rewards."""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from config import Config
from rewards import score
from sqlalchemy import func, select, text
from test_exact_competition import result

import db
from conftest import register
from db import models as m
from db.evaluations import EvaluationsDb
from db.jobs import WorkerDb


def setup(store, config):
    repo = EvaluationsDb(store.sessions)
    repo.set_leader(uuid.uuid4().hex)
    repo.reconcile(config)
    return repo, WorkerDb(store.sessions)


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


def test_proof_retry_fencing_and_cache(store, config):
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
    register(store, "bob", uid=1)
    other = store.submissions.add("bob", b"source")
    assert repo.submissions.claim_proof("two", config.proof_identity, 30) is None
    with db.session_scope(store.sessions) as s:
        assert s.get(m.Submission, other).state == "proved"


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
    raw = copy.deepcopy(config.__dict__)
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
    raw = copy.deepcopy(config.__dict__)
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
    raw = copy.deepcopy(config.__dict__)
    raw.update(seed_mode="fresh", seeds=(), seed_count=2)
    fresh = Config.parse(raw)
    repo, _ = setup(store, fresh)
    accepted(store, fresh)
    repo.plan(fresh)
    with db.session_scope(store.sessions) as s:
        old = {e.board: e.plan["seed_values"] for e in s.scalars(select(m.Evaluation))}
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
    raw = copy.deepcopy(config.__dict__)
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
