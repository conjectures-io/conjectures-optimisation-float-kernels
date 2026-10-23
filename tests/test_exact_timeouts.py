"""Phase timeouts reject/pause the responsible side; a supervisor overrun is infrastructure."""

from __future__ import annotations

import contextlib
import hashlib
import threading
from types import SimpleNamespace

import process
import pytest

from workers import benchmark_worker
from workers import coordinator as exact_coordinator


class FakeWorkerRepo:
    # The benchmark worker's database view: two sources in, one failure or result out.
    def __init__(self):
        self.failures = []

    def sources(self, job_id):
        return b"candidate", b"reference"

    def heartbeat(self, *args):
        return True

    def fail(self, job_id, owner, token, error):
        self.failures.append(error)

    def complete(self, *args):
        raise AssertionError("a failed run cannot complete")


def raising(exc):
    # A supervisor that raises `exc` instead of running anything.
    def supervised(*args, **kwargs):
        raise exc

    return supervised


@pytest.mark.parametrize(
    "exc, kind",
    [
        (process.BudgetExceeded("did not finish within 1440s"), "infrastructure"),
        (RuntimeError("lease lost"), "infrastructure"),
    ],
)
def test_supervisor_failure_is_infrastructure(monkeypatch, exc, kind):
    # The subprocess owns phase attribution. The outer safety net cannot blame a candidate.
    def supervise(*args, **kwargs):
        assert args[2] == 1440
        raise exc

    monkeypatch.setattr(benchmark_worker, "supervised", supervise)
    repo = FakeWorkerRepo()
    job = SimpleNamespace(
        id=1,
        token="t",
        lease_seconds=120,
        request={
            "timing": {
                "rounds": 25,
                "inner": 3,
                "cell_ms": 100,
                "run_seconds": 300,
                "build_seconds": 480,
                "benchmark_seconds": 900,
            }
        },
    )
    benchmark_worker.execute(repo, job, "owner")
    assert [f["kind"] for f in repo.failures] == [kind]
    if isinstance(exc, process.BudgetExceeded):
        assert repo.failures[0]["reason"].startswith("supervisor-timeout:")


class FakeCoordinatorRepo:
    # The coordinator's database view for one proof check.
    def __init__(self):
        self.sessions = None
        self.verdicts = []

    def heartbeat_proof(self, *args):
        return True

    def finish_proof(self, sid, token, identity, verdict, retry):
        self.verdicts.append(verdict)


@pytest.mark.parametrize(
    "exc, code",
    [(process.BudgetExceeded("did not finish within 1800s"), 1), (RuntimeError("lease lost"), 2)],
)
def test_a_proof_check_past_its_budget_rejects(monkeypatch, tmp_path, exc, code):
    # A timeout rejects the entry (exit 1, final); any other error requeues it (exit 2).
    monkeypatch.setattr(process, "ROOT", tmp_path)
    content = b"Kernel.lean bytes"

    @contextlib.contextmanager
    def session_scope(_sessions):
        yield SimpleNamespace(get=lambda _model, _key: SimpleNamespace(content=content))

    monkeypatch.setattr(exact_coordinator.db, "session_scope", session_scope)
    monkeypatch.setattr(exact_coordinator, "supervised", raising(exc))
    repo = FakeCoordinatorRepo()
    sub = SimpleNamespace(id=1, source_sha256=hashlib.sha256(content).hexdigest(), token="a")
    config = SimpleNamespace(
        lease_seconds=120, verification_seconds=1800, proof_identity="p", retry_seconds=300
    )
    exact_coordinator.verify(repo, sub, config, threading.Event())
    assert [v["exit_code"] for v in repo.verdicts] == [code]
