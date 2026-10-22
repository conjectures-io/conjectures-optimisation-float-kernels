"""Independent audit discovery, remote execution, fencing and operator eligibility decisions."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import process
import pytest
import test_exact_competition as competition
from config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_tensor_workflow import accepted

import db
from conftest import register
from db import models as m
from db.audits import AuditDb
from db.evaluations import EvaluationsDb
from db.jobs import WorkerDb
from db.publications import PublicationsDb
from workers import exact_auditor

config = competition.config


class Coordinator(EvaluationsDb):
    """Test fixture combining the independent production repository interfaces."""

    def __init__(self, sessions):
        super().__init__(sessions)
        self.set_leader(uuid.uuid4().hex)
        self.worker = WorkerDb(sessions)
        self.claimed = {}

    def claim(self, environment, identity, owner, lease):
        job = self.worker.claim(environment, identity, owner)
        if job:
            self.claimed[job.id] = job.request
        return job

    def complete(self, jid, owner, token, result):
        self.worker.requests[jid] = self.claimed[jid]
        return self.worker.complete(jid, owner, token, result)

    def claim_proof(self, owner, identity, lease):
        return self.submissions.claim_proof(owner, identity, lease)

    def publication(self):
        return PublicationsDb(self.sessions).publication()


def promote(store, config, repo, names):
    repo.reconcile(config)
    for name in names:
        accepted(store, config, name, f"source-{name}".encode())
        repo.plan(config)
        while job := repo.claim("local", "a" * 64, "worker", 30):
            repo.complete(job.id, job.owner, job.token, competition.result(job.request))
        repo.aggregate(config)


def paid(repo, board="mlp"):
    return [k["hotkey"] for k in repo.publication()["boards"][board]["kings"]]


@pytest.fixture
def audit_setup(store, config):
    coordinator = Coordinator(store.sessions)
    promote(store, config, coordinator, ["a", "b", "c"])
    profile = exact_auditor.audit_profile(2)
    return coordinator, AuditDb(store.sessions), profile


def test_competition_runs_with_no_auditor_or_audit_config(store, config):
    repo = Coordinator(store.sessions)
    promote(store, config, repo, ["a", "b", "c"])
    assert paid(repo) == ["c", "b"]
    with db.session_scope(store.sessions) as s:
        assert list(s.scalars(select(m.Audit))) == []
    # Old hold/disqualify configuration must fail explicitly, never silently change payouts.
    with pytest.raises(ValueError, match="unknown configuration"):
        Config.parse(dataclasses.asdict(config) | {"audit": {"policy": "hold"}})


def test_discovery_backfills_every_promotion_deduplicates_and_resumes(audit_setup, store):
    repo, audits, profile = audit_setup
    assert audits.discover(profile, batch=1) == 1
    assert AuditDb(store.sessions).discover(profile) == 5
    assert audits.discover(profile) == 0
    with db.session_scope(store.sessions) as s:
        rows = list(s.scalars(select(m.Audit)))
        assert len(rows) == 3  # Includes the old source a, despite two later kings on each board.
        assert {r.source_hash for r in rows} == {
            hashlib.sha256(f"source-{name}".encode()).hexdigest() for name in "abc"
        }
    assert paid(repo) == ["c", "b"]


def test_discovery_and_cursor_commit_atomically(audit_setup, store):
    _, audits, profile = audit_setup
    with pytest.raises(IntegrityError):
        audits.discover(profile | {"cases": None})
    with db.session_scope(store.sessions) as s:
        assert not list(s.scalars(select(m.AuditCursor)))
        assert not list(s.scalars(select(m.Audit)))
    assert audits.discover(profile) == 6


def test_concurrent_discovery_does_not_duplicate_or_skip(audit_setup, store):
    _, audits, profile = audit_setup
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: audits.discover(profile, batch=1), range(2))) == 2
    audits.discover(profile)
    with db.session_scope(store.sessions) as s:
        assert len(list(s.scalars(select(m.Audit)))) == 3


def test_coverage_change_backfills_again_and_worker_selects_matching_release(audit_setup, config):
    _, audits, profile = audit_setup
    audits.discover(profile)
    assert audits.claim(profile, "wrong-release", "host", 30) is None
    stronger = exact_auditor.audit_profile(1000)
    assert stronger != profile
    assert audits.discover(stronger) == 6
    task = audits.claim(stronger, config.proof_identity, "host", 30)
    assert task.cases == 1000
    assert task.request["profile"] == stronger
    assert task.request["proof_identity"] == config.proof_identity


def test_audit_never_changes_rewards_or_references(audit_setup, config, store):
    repo, audits, profile = audit_setup
    audits.discover(profile)
    before = repo.publication()
    for state in ["failed", "clean", "error"]:
        task = audits.claim(profile, config.proof_identity, "remote", 30)
        assert hashlib.sha256(task.content).hexdigest() == task.source_hash
        assert audits.finish(task, state, {"mismatches": 1}, 300)
    assert repo.publication() == before
    repo.publish(config)
    assert repo.publication() == before
    with db.session_scope(store.sessions) as s:
        assert not list(s.scalars(select(m.SourceDisqualification)))
        assert len(list(s.scalars(select(m.Promotion)))) == 6


def test_leases_expire_stale_tokens_are_rejected_and_attempts_survive(audit_setup, config, store):
    repo, audits, profile = audit_setup
    audits.discover(profile)
    first = audits.claim(profile, config.proof_identity, "dead-host", 30)
    assert audits.heartbeat(first, 30)
    assert not audits.finish(dataclasses.replace(first, token="stale"), "clean", {}, 300)
    repo.reconcile(config)  # Restarting a coordinator must not steal a running audit.
    with db.session_scope(store.sessions) as s:
        assert s.get(m.Audit, first.id).state == "running"
        s.get(m.Audit, first.id).lease_until = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=1
        )
    again = audits.claim(profile, config.proof_identity, "replacement-host", 30)
    assert again.id == first.id and again.token != first.token
    assert not audits.heartbeat(first, 30)
    assert not audits.finish(first, "clean", {}, 300)
    assert audits.finish(again, "failed", {"mismatches": 2}, 300)
    with db.session_scope(store.sessions) as s:
        assert s.get(m.AuditAttempt, first.token).state == "expired"
        assert s.get(m.AuditAttempt, again.token).report == {"mismatches": 2}


def test_error_backoff_and_old_pending_alerts(audit_setup, config, store):
    _, audits, profile = audit_setup
    audits.discover(profile)
    task = audits.claim(profile, config.proof_identity, "host", 30)
    assert audits.finish(task, "error", {"reason": "sandbox unavailable"}, 300)
    # Other tasks can proceed while this source cools down.
    another = audits.claim(profile, config.proof_identity, "host", 30)
    assert another.id != task.id
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Audit, task.id)
        assert row.due_at > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=290)
        row.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        row.due_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    assert audits.alerts(profile, 900)[0]["id"] == task.id
    retry = audits.claim(profile, config.proof_identity, "host", 30)
    assert retry.id == task.id and retry.attempts == 2
    assert audits.finish(retry, "clean", {}, 300)
    with db.session_scope(store.sessions) as s:
        assert s.get(m.AuditAttempt, task.token).report["reason"] == "sandbox unavailable"


def test_operator_disqualification_blocks_later_board_promotion_and_can_be_restored(store, config):
    repo = Coordinator(store.sessions)
    repo.reconcile(config)
    sid = accepted(store, config, "a", b"source-a")
    repo.plan(config)
    jobs = []
    while job := repo.claim("local", "a" * 64, "worker", 30):
        jobs.append(job)
    first, later = jobs
    repo.complete(first.id, first.owner, first.token, competition.result(first.request))
    assert repo.aggregate(config) == 1
    source = hashlib.sha256(b"source-a").hexdigest()
    repo.set_disqualification(source, True, "investigated mismatch", "operator", config)
    assert paid(repo) == []
    repo.complete(later.id, later.owner, later.token, competition.result(later.request))
    repo.aggregate(config)
    assert paid(repo, "resnet") == []
    assert repo.plan(config) == []
    with db.session_scope(store.sessions) as s:
        assert s.get(m.Submission, sid).state == "proved"
    repo.set_disqualification(source, False, "harness bug fixed", "operator", config)
    assert paid(repo) == ["a"]
    assert len(repo.plan(config)) == 1  # The uncompleted board can resume after restoration.
    with db.session_scope(store.sessions) as s:
        assert len(s.get(m.SourceDisqualification, source).history) == 2


def test_disqualification_fills_previous_reward_ranks(audit_setup, config):
    repo, _, _ = audit_setup
    repo.set_disqualification(hashlib.sha256(b"source-c").hexdigest(), True, "review", "op", config)
    assert paid(repo) == ["b", "a"]
    assert paid(repo, "resnet") == ["b", "a"]


def test_profile_changes_with_coverage_and_audit_sources(monkeypatch, tmp_path):
    assert exact_auditor.audit_profile(50) != exact_auditor.audit_profile(1000)
    for name in exact_auditor.AUDIT_SOURCES:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("version one")
    monkeypatch.setattr(exact_auditor, "ROOT", tmp_path)
    profile = exact_auditor.audit_profile(50)
    (tmp_path / exact_auditor.AUDIT_SOURCES[0]).write_text("version two")
    assert profile != exact_auditor.audit_profile(50)


def test_missing_git_metadata_does_not_block_competition_proofs(monkeypatch):
    from workers import coordinator as exact_coordinator

    def absent(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(exact_coordinator.subprocess, "check_output", absent)
    environment = exact_coordinator.source_environment()
    assert environment["commit"] is None
    assert environment["toolchain"].startswith("leanprover/lean4:")
    assert len(environment["manifest_sha256"]) == 64


def verdict(code=0, mismatch=0):
    return {
        "mode": "audit",
        "stage": "audit",
        "passed": code == 0,
        "verifier_fingerprint": "b" * 64,
        "verifier_fingerprint_end": "b" * 64,
        "reason": "audit mismatch" if mismatch else "",
        "classes": {"audit": {"cases": 504, "mismatches": mismatch, "seed": 7}},
    }


@pytest.mark.parametrize(
    "code, data, expected",
    [
        (0, verdict(), "clean"),
        (1, verdict(1, 3), "failed"),
        (2, verdict(1, 3), "error"),
        (0, {}, "error"),
        (0, verdict() | {"verifier_fingerprint_end": "different"}, "error"),
        (1, {"stage": "audit", "reason": "gate error: build failed"}, "error"),
    ],
)
def test_result_classification_requires_complete_matching_evidence(code, data, expected):
    task = SimpleNamespace(request={"proof_identity": "b" * 64}, seed=7)
    assert exact_auditor.classify(code, data, task) == expected


@pytest.mark.parametrize(
    "code, data, expected",
    [(0, verdict(), "clean"), (1, verdict(1, 3), "failed"), (2, {}, "error")],
)
def test_remote_worker_fetches_source_and_keeps_artifacts(
    monkeypatch, tmp_path, code, data, expected
):
    # The subprocess is mocked; keep this unit test off a live worker's build lock.
    monkeypatch.setattr(process, "ROOT", tmp_path)
    profile = exact_auditor.audit_profile(2)
    task = SimpleNamespace(
        id=1,
        token="claim",
        owner="remote-host",
        cases=2,
        seed=7,
        content=b"source",
        source_hash=hashlib.sha256(b"source").hexdigest(),
        request={"proof_identity": "b" * 64, "profile": profile},
    )
    outcomes = []
    repo = SimpleNamespace(
        heartbeat=lambda *_: True,
        finish=lambda t, state, report, retry: outcomes.append((state, report)) or True,
    )
    monkeypatch.setattr(exact_auditor.fingerprint, "current", lambda: "b" * 64)

    def fake_gate(command, *args, **kwargs):
        Path(command[command.index("--verdict") + 1]).write_text(json.dumps(data))
        return code

    monkeypatch.setattr(exact_auditor, "supervised", fake_gate)
    settings = SimpleNamespace(output=tmp_path, lease=30, seconds=900, retry=300)
    exact_auditor.run_audit(repo, task, settings, threading.Event())
    assert outcomes[0][0] == expected
    assert (tmp_path / "1-claim/Kernel.lean").read_bytes() == task.content
    assert json.loads((tmp_path / "1-claim/report.json").read_text())["state"] == expected


def test_promotion_preserves_environment_when_source_is_reverified(store, config):
    repo = Coordinator(store.sessions)
    repo.reconcile(config)
    sid = accepted(store, config, "a", b"source-a")
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Submission, sid)
        row.proof_report = {"source_environment": {"commit": "original-release"}}
    repo.plan(config)
    while job := repo.claim("local", "a" * 64, "worker", 30):
        repo.complete(job.id, job.owner, job.token, competition.result(job.request))
    repo.aggregate(config)
    with db.session_scope(store.sessions) as s:
        s.get(m.Submission, sid).proof_report = {"source_environment": {"commit": "new-release"}}
    audits = AuditDb(store.sessions)
    profile = exact_auditor.audit_profile(2)
    audits.discover(profile)
    task = audits.claim(profile, config.proof_identity, "remote", 30)
    assert task.request["source_environment"] == {"commit": "original-release"}


def test_new_auditor_can_test_same_spec_without_using_original_proof_checker(
    audit_setup, config, store
):
    _, audits, profile = audit_setup
    from provenance import specification_identity

    spec = specification_identity()
    with db.session_scope(store.sessions) as s:
        for p in s.scalars(select(m.Promotion)):
            p.source_environment = {"specification_identity": spec}
    audits.discover(profile)
    assert audits.claim(profile, config.proof_identity, "wrong-spec", 30, "wrong") is None
    task = audits.claim(profile, "new-audit-verifier", "remote", 30, spec)
    assert task is not None
    assert task.request["proof_identity"] == config.proof_identity
    assert task.request["specification_identity"] == spec


def test_audit_result_uses_actual_execution_identity_and_preserves_original_proof_identity():
    task = SimpleNamespace(request={"proof_identity": "original"}, seed=7)
    data = verdict()
    assert exact_auditor.classify(0, data, task, "b" * 64) == "clean"
    assert exact_auditor.classify(0, data, task) == "error"


def test_operator_blocked_queued_source_does_not_stall_other_submissions(store, config):
    repo = Coordinator(store.sessions)
    repo.reconcile(config)
    register(store, "blocked")
    sid = store.submissions.add("blocked", b"blocked")
    repo.set_disqualification(hashlib.sha256(b"blocked").hexdigest(), True, "review", "op", config)
    other = accepted(store, config, "good", b"good")
    assert repo.claim_proof("worker", config.proof_identity, 30) is None
    assert len(repo.plan(config)) == 2
    with db.session_scope(store.sessions) as s:
        assert {e.submission_id for e in s.scalars(select(m.Evaluation))} == {other}
        assert s.get(m.Submission, sid).state == "queued"


def test_auditor_database_role_cannot_change_competition(audit_setup, config, store):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    _, _, profile = audit_setup
    role = "exact_auditor_test"
    root = Path(__file__).resolve().parents[1]
    with store.engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        sql = (
            (root / "deploy/platform/exact_auditor.sql").read_text().replace("exact_auditor", role)
        )
        conn.execute(text(sql))
    engine = create_engine(store.engine.url, connect_args={"options": f"-c role={role}"})
    try:
        repo = AuditDb(sessionmaker(bind=engine))
        assert repo.discover(profile) == 6
        task = repo.claim(profile, config.proof_identity, "remote", 30)
        assert task and task.content
        assert repo.heartbeat(task, 30)
        assert repo.finish(task, "failed", {"reason": "mismatch"}, 300)
        for table in [
            "promotions",
            "submissions",
            "coordinator_state",
            "source_disqualifications",
            "benchmark_jobs",
        ]:
            with engine.begin() as conn:
                assert not conn.scalar(
                    text("SELECT has_table_privilege(current_user,:t,'UPDATE')"), {"t": table}
                )
    finally:
        engine.dispose()
        with store.engine.begin() as conn:
            conn.exec_driver_sql(f"DROP OWNED BY {role}")
            conn.exec_driver_sql(f"DROP ROLE {role}")


@pytest.mark.slow
@pytest.mark.parametrize("fixture, expected", [("incumbent", "clean"), ("auditwrong", "failed")])
def test_real_sandboxed_independent_auditor(store, config, tmp_path, fixture, expected):
    import sandbox as gate
    from provenance import specification_identity

    import fingerprint

    try:
        gate.check_limits()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    identity = fingerprint.current()
    # The observer is a newer verifier release, but its pinned mathematical definitions match.
    config = dataclasses.replace(config, proof_identity="d" * 64, benchmark_identity=identity)
    root = Path(__file__).resolve().parents[1]
    work = root / "lean/work/Submission.lean"
    original = work.read_bytes() if work.exists() else None
    repo = Coordinator(store.sessions)
    repo.reconcile(config)
    # Simulate a promotion at the audit boundary, including a deliberately wrong fixture
    # standing for a defect that escaped the proof/build pipeline. No chain writes.
    source = (root / f"kernels/exact/{fixture}/Kernel.lean").read_bytes()
    sid = accepted(store, config, fixture, source)
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Submission, sid)
        row.proof_report = {
            "source_environment": {"specification_identity": specification_identity()}
        }
    repo.plan(config)
    while job := repo.claim("local", "a" * 64, "worker", 120):
        repo.complete(job.id, job.owner, job.token, competition.result(job.request))
    repo.aggregate(config)
    before = repo.publication()
    audits = AuditDb(store.sessions)
    profile = exact_auditor.audit_profile(2)
    audits.discover(profile)
    with db.session_scope(store.sessions) as s:
        s.scalar(select(m.Audit)).seed = 971
    task = audits.claim(profile, identity, "sandbox-auditor", 120, specification_identity())
    assert task.request["proof_identity"] != identity
    settings = SimpleNamespace(output=tmp_path, lease=120, seconds=300, retry=300)
    try:
        exact_auditor.run_audit(audits, task, settings, threading.Event())
        with db.session_scope(store.sessions) as s:
            row = s.get(m.Audit, task.id)
            assert row.state == expected, row.report
            assert row.report["verdict"]["classes"]["audit"]["cases"] == 504
            assert row.report["audit_verifier_identity"] == identity
        assert repo.publication() == before
    finally:
        if original is None:
            work.unlink(missing_ok=True)
        else:
            work.write_bytes(original)
