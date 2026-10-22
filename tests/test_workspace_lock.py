"""A busy proof workspace must not consume a benchmark lease or execution budget."""

import fcntl
import sys
import threading
from types import SimpleNamespace

from workers import benchmark_worker


def test_benchmark_waits_for_proof_workspace_before_claiming(monkeypatch, tmp_path):
    started, claimed = threading.Event(), threading.Event()
    errors = []
    repo = SimpleNamespace(claim=lambda *args: claimed.set())
    store = SimpleNamespace(sessions=None, close=lambda: None)
    monkeypatch.setattr(benchmark_worker, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark_worker, "install_signals", started.set)
    monkeypatch.setattr(benchmark_worker.db, "connect", lambda: store)
    monkeypatch.setattr(benchmark_worker, "WorkerDb", lambda sessions: repo)
    monkeypatch.setattr(benchmark_worker, "machine_identity", lambda: "identity")
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker", "--environment", "test", "--once", "--host-lock", str(tmp_path / "host.lock")],
    )

    def worker():
        try:
            benchmark_worker.main()
        except BaseException as exc:
            errors.append(exc)

    with open(tmp_path / "gate.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            assert started.wait(2)
            assert not claimed.wait(0.1), "a proof is still using the checkout"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    assert claimed.is_set()
