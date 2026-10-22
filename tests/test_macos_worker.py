"""Unsupported macOS execution must fail before credentials, claims or spawning."""

import sys

import pytest
import sandbox


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("GATE_SANDBOX_BACKEND", raising=False)


@pytest.mark.parametrize("disabled", [False, True])
def test_mac_run_cannot_bypass_with_legacy_unsandboxed_setting(mac, monkeypatch, disabled):
    monkeypatch.setattr(sandbox, "_SANDBOX", not disabled)
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(RuntimeError, match="native macOS benchmark workers are unsupported"):
        sandbox.run(["untrusted-candidate"], 1)
    with pytest.raises(RuntimeError, match="unsupported"):
        sandbox.check_limits()


@pytest.mark.parametrize("selected", ["linux-bwrap", "none", "", "typo"])
def test_mac_cannot_select_another_backend(mac, monkeypatch, selected):
    monkeypatch.setenv("GATE_SANDBOX_BACKEND", selected)
    with pytest.raises(RuntimeError, match="unsupported sandbox backend"):
        sandbox.require_backend()


def test_linux_selection_is_unchanged(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.delenv("GATE_SANDBOX_BACKEND", raising=False)
    assert sandbox.backend() == "linux-bwrap"
    sandbox.require_backend()
    monkeypatch.setenv("GATE_SANDBOX_BACKEND", "macos-seatbelt")
    with pytest.raises(RuntimeError, match="unsupported sandbox backend"):
        sandbox.require_backend()


def test_mac_supervisor_refuses_before_creating_log(mac, tmp_path):
    from process import supervised

    log = tmp_path / "no-log"
    with pytest.raises(RuntimeError, match="unsupported"):
        supervised(["untrusted"], lambda: pytest.fail("heartbeat"), 1, 1, log)
    assert not log.exists()


@pytest.mark.parametrize("module", ["tools.worker_preflight", "workers.benchmark_worker"])
def test_mac_startup_refuses_before_loading_credentials(mac, monkeypatch, module):
    import importlib

    worker = importlib.import_module(module)
    monkeypatch.setattr(sys, "argv", [module, "--credentials", "/synthetic/never-read"])
    monkeypatch.setattr(worker, "load", lambda *a: pytest.fail("credentials read"))
    monkeypatch.setattr(worker.db, "connect", lambda: pytest.fail("DB opened"))
    with pytest.raises(RuntimeError, match="unsupported"):
        worker.main()


def test_mac_identity_reports_actual_backend_without_affinity(mac, monkeypatch):
    import benchmark
    import macos_worker

    values = {
        ("/usr/sbin/sysctl", "-n", "hw.model"): "Mac15,14",
        ("/usr/sbin/sysctl", "-n", "hw.ncpu"): "32",
        ("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"): "Apple M2 Ultra",
        ("/usr/bin/sw_vers",): "macOS 27.0.1",
        ("/usr/bin/cc", "--version"): "Apple clang version test",
    }
    monkeypatch.setattr(macos_worker.subprocess, "check_output", lambda args, **kw: values[args])
    data = macos_worker.identity()
    assert data["cpu_affinity"] is None
    assert data["supported"] is False
    assert data["backend"] == "macos-seatbelt"
    assert data["limits"]["aggregate_memory"] is None
    first = benchmark.machine_identity()
    assert len(first) == 64
    values[("/usr/bin/sw_vers",)] = "macOS changed"
    assert benchmark.machine_identity() != first
