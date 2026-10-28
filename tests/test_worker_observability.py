"""Alert events follow committed audit outcomes; telemetry never controls the competition."""

import hashlib
import importlib.util
import json
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import axiom
from workers import benchmark_worker, exact_auditor


class Capture:
    enabled = True

    def __init__(self):
        self.events = []
        self.second_heartbeat = threading.Event()

    def ingest(self, **record):
        self.events.append(record)
        if sum(r["event_type"] == "service_heartbeat" for r in self.events) >= 2:
            self.second_heartbeat.set()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset_events():
    axiom.reset()
    yield
    axiom.reset()


def test_worker_heartbeat_continues_while_a_job_is_busy_and_stops_on_exit(monkeypatch):
    capture = Capture()
    monkeypatch.setattr(axiom, "client_from_env", lambda env=None: capture)
    monkeypatch.setattr(axiom, "HEARTBEAT_SECONDS", 0.01)

    @axiom.observe_worker("torchlean-audit-worker")
    def busy_worker():
        assert capture.second_heartbeat.wait(2), "no heartbeat during a long job"

    busy_worker()
    assert capture.events[0]["event_type"] == "service_started"
    assert capture.events[-1]["event_type"] == "service_stopped"
    assert capture.events[-1]["details"]["outcome"] == "stopped"
    assert all(e["details"]["competition"] == "torchlean" for e in capture.events)
    assert not any(t.name == "axiom-heartbeat" for t in threading.enumerate())


@pytest.mark.parametrize("error", [ValueError("startup failed"), SystemExit(2)])
def test_worker_startup_failure_is_reported_and_client_is_flushed(monkeypatch, error):
    capture = Capture()
    monkeypatch.setattr(axiom, "client_from_env", lambda env=None: capture)

    @axiom.observe_worker("torchlean-audit-worker")
    def broken_worker():
        raise error

    with pytest.raises(type(error)):
        broken_worker()
    failure = next(e for e in capture.events if e["event_type"] == "service_failed")
    assert failure["severity"] == "error"
    assert failure["details"]["error_type"] == type(error).__name__
    assert capture.events[-1]["details"]["outcome"] == "failed"


def test_requested_shutdown_is_not_an_anomaly(monkeypatch):
    capture = Capture()
    monkeypatch.setattr(axiom, "client_from_env", lambda env=None: capture)

    @axiom.observe_worker("torchlean-audit-worker")
    def interrupted_worker():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        interrupted_worker()
    assert "service_failed" not in [e["event_type"] for e in capture.events]


@pytest.mark.parametrize(
    "code,mismatches,saved,event,severity",
    [
        (0, 0, True, "audit_clean", "info"),
        (1, 3, True, "audit_failed", "critical"),
        (2, 0, True, "audit_error", "error"),
        (1, 3, False, "audit_result_discarded", "warning"),
    ],
)
def test_audit_alerts_distinguish_mismatches_errors_and_stale_results(
    monkeypatch, tmp_path, code, mismatches, saved, event, severity
):
    capture = Capture()
    axiom.init("torchlean-audit-worker", client=capture)
    identity = "a" * 64
    profile = exact_auditor.audit_profile(2)
    source = b"private source should never appear in telemetry"
    task = SimpleNamespace(
        id=7,
        token="private-lease-token",
        owner="host",
        cases=2,
        seed=71,
        attempts=3,
        content=source,
        source_hash=hashlib.sha256(source).hexdigest(),
        request={
            "proof_identity": identity,
            "profile": profile,
            "board": "mixer",
            "promotion_id": 11,
        },
    )
    monkeypatch.setattr(exact_auditor.fingerprint, "current", lambda: identity)
    monkeypatch.setattr(
        exact_auditor, "checkout", lambda *a: nullcontext(SimpleNamespace(fileno=lambda: 9))
    )

    def audit(command, *args, **kwargs):
        Path(command[command.index("--verdict") + 1]).write_text(
            json.dumps(
                {
                    "mode": "audit",
                    "stage": "audit",
                    "passed": code == 0,
                    "verifier_fingerprint": identity,
                    "verifier_fingerprint_end": identity,
                    "reason": "test result",
                    "classes": {"audit": {"cases": 504, "mismatches": mismatches, "seed": 71}},
                }
            )
        )
        return code

    monkeypatch.setattr(exact_auditor, "supervised", audit)
    writes = []
    repo = SimpleNamespace(
        heartbeat=lambda *a: True,
        finish=lambda task, state, report, retry: writes.append(state) or saved,
    )
    settings = SimpleNamespace(output=tmp_path, seconds=60, lease=30, retry=60)
    exact_auditor.run_audit(repo, task, settings, threading.Event())
    emitted = capture.events
    assert [e["event_type"] for e in emitted] == ["audit_started", event]
    assert emitted[-1]["severity"] == severity
    assert emitted[-1]["details"]["audit_id"] == 7
    assert emitted[-1]["details"]["board"] == "mixer"
    assert source.decode() not in json.dumps(capture.events)
    assert task.token not in json.dumps(capture.events)
    if not saved:
        assert not any(e["event_type"] == "audit_failed" for e in emitted)


def test_unconfigured_worker_does_not_start_telemetry_threads(monkeypatch):
    monkeypatch.setattr(axiom, "client_from_env", lambda env=None: axiom.NoopClient())

    @axiom.observe_worker("torchlean-audit-worker")
    def worker():
        return 42

    assert worker() == 42
    assert not any(t.name == "axiom-heartbeat" for t in threading.enumerate())


@pytest.mark.parametrize("data", [None, [], {"classes": None}, {"classes": {"audit": []}}])
def test_malformed_audit_evidence_is_an_error(data):
    task = SimpleNamespace(request={"proof_identity": "a" * 64}, seed=7)
    assert exact_auditor.classify(0, data, task) == "error"


@pytest.mark.parametrize(
    "error,saved,event,severity",
    [
        (None, True, "benchmark_completed", "info"),
        ("infrastructure", True, "benchmark_failed", "error"),
        ("candidate", True, "benchmark_failed", "warning"),
        ("checksum", True, "benchmark_failed", "critical"),
        (None, False, "benchmark_result_discarded", "warning"),
        ("infrastructure", False, "benchmark_result_discarded", "warning"),
    ],
)
def test_benchmark_events_follow_fenced_writes(monkeypatch, error, saved, event, severity):
    capture = Capture()
    axiom.init("torchlean-benchmark-worker", client=capture)
    job = SimpleNamespace(
        id=3,
        token="private-token",
        lease_seconds=30,
        request={
            "timing": {
                "rounds": 1,
                "inner": 1,
                "cell_ms": 1,
                "run_seconds": 1,
                "build_seconds": 1,
                "benchmark_seconds": 1,
            }
        },
    )
    repo = SimpleNamespace(
        sources=lambda _: (b"candidate", b"reference"),
        complete=lambda *a: saved,
        fail=lambda *a: saved,
    )

    def run(command, *args, **kwargs):
        value = {"error": {"kind": error, "reason": "test failure"}} if error else {"result": {}}
        Path(command[command.index("--result") + 1]).write_text(json.dumps(value))

    # Telemetry must not depend on the host's prepared workspace or binary cache.
    monkeypatch.setattr(benchmark_worker, "preparation_mode", lambda _: "cold")
    monkeypatch.setattr(benchmark_worker, "supervised", run)
    benchmark_worker.execute(repo, job, "host")
    expected = ["benchmark_claimed"]
    if saved:
        expected.append("benchmark_job_finished")
    expected.append(event)
    assert [e["event_type"] for e in capture.events] == expected
    assert job.token not in json.dumps(capture.events)
    assert capture.events[-1]["severity"] == severity


def monitor_renderer():
    path = Path(__file__).resolve().parents[1] / "scripts/render-axiom-monitors.py"
    spec = importlib.util.spec_from_file_location("monitor_renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_monitor_rendering_pins_environment_host_and_notifier():
    monitors = monitor_renderer().render("conjectures-validator", "dev", "audit-host", "notify-1")
    assert len(monitors) == 4
    assert all(m["notifierIds"] == ["notify-1"] for m in monitors)
    assert all("competition == 'torchlean'" in m["aplQuery"] for m in monitors)
    assert all("environ == 'dev'" in m["aplQuery"] for m in monitors)
    heartbeat = monitors[-1]
    assert "host == 'audit-host'" in heartbeat["aplQuery"]
    assert "summarize heartbeats=count()" in heartbeat["aplQuery"]
    assert heartbeat["operator"] == "Below" and heartbeat["threshold"] == 1
    assert "$" not in json.dumps(monitors)


def test_monitor_rendering_rejects_query_injection():
    with pytest.raises(ValueError):
        monitor_renderer().render("dataset'; injected", "prod", "host", "notifier")


@pytest.mark.parametrize(
    "worker,source",
    [
        ("coordinator", "competition-coordinator"),
        ("benchmark_worker", "competition-benchmark-worker"),
    ],
)
def test_worker_entrypoints_keep_one_lifecycle_client_on_startup_failure(
    monkeypatch, worker, source
):
    import importlib
    import sys

    import sandbox

    module = importlib.import_module("workers." + worker)
    capture = Capture()
    monkeypatch.setattr(axiom, "client_from_env", lambda env=None: capture)
    monkeypatch.setattr(sys, "argv", [worker])
    monkeypatch.delenv("COMPETITION_CONFIG", raising=False)
    monkeypatch.delenv("BENCHMARK_ENVIRONMENT", raising=False)
    monkeypatch.setattr(module, "install_signals", lambda: None)
    monkeypatch.setattr(sandbox, "require_backend", lambda: None)
    if worker == "benchmark_worker":
        monkeypatch.setattr(module, "load", lambda _: None)
    with pytest.raises(SystemExit):
        module.main()
    types = [e["event_type"] for e in capture.events]
    assert types.count("service_started") == types.count("service_stopped") == 1
    assert "service_heartbeat" in types
    assert "service_misconfigured" in types
    assert "service_failed" in types
    assert all(e["source"] == source for e in capture.events)
