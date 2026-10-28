"""Build time and calibration/measurement time have independent, bounded clocks."""

import hashlib
import shutil
import sys
from pathlib import Path

import benchmark
import pytest
import sandbox
from multiseed import workloads
from timing import supervisor_seconds, validate_timing

CELL = "mlp:b1:normal"
NATIVE_BUILD = benchmark.build


class Clock:
    now = 0.0

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: value.now)
    monkeypatch.setattr(sandbox, "_budget", sandbox.Budget())
    return value


def timing(**extra):
    return (
        dict(rounds=1, inner=1, cell_ms=1, run_seconds=10, build_seconds=10, benchmark_seconds=4)
        | extra
    )


@pytest.fixture
def measurement(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(sandbox, "WORK", tmp_path / "Submission.lean")
    sandbox.WORK.write_bytes(b"operator source")
    monkeypatch.setattr(sandbox, "check_limits", lambda: None)
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "a" * 64)
    monkeypatch.setattr(benchmark.fingerprint, "benchmark", lambda: "b" * 64)
    binaries = {}
    for side in ("reference", "candidate"):
        path = tmp_path / side
        path.write_bytes(side.encode())
        binaries[side] = path
    request = dict(
        identity="a" * 64,
        benchmark_identity="b" * 64,
        environment="test",
        seed=42,
        workloads=workloads(42),
        candidate_hash=hashlib.sha256(b"candidate").hexdigest(),
        reference_hash=hashlib.sha256(b"reference").hexdigest(),
        cells=[CELL],
        timing=timing(),
    )
    monkeypatch.setattr(sandbox, "prepare_shared", lambda **kw: clock.advance(2))

    def build(source, expected, side, deadline):
        clock.advance(3 if side == "reference" else 4)
        deadline.remaining(side)
        return binaries[side]

    monkeypatch.setattr(benchmark, "build", build)
    caps = []

    def run(command, timeout, **kwargs):
        caps.append(timeout)
        clock.advance(1 if kwargs["env_extra"]["BENCH_REPS"] else 0.5)
        return 0, "\n".join(f"{w['id']}|{CELL} true 100 123" for w in workloads(42))

    monkeypatch.setattr(sandbox, "run", run)
    return request, caps


def test_build_does_not_consume_measurement_time_and_durations_are_recorded(measurement):
    request, caps = measurement
    result = benchmark.measure(request, b"candidate", b"reference")
    assert caps == pytest.approx([4, 3.5, 2.5, 1.5])
    assert result["stage_seconds"] == pytest.approx(
        dict(build=9, calibration=1.5, rounds=2, benchmark=3.5)
    )
    assert result["timing_seconds"] == pytest.approx(2)
    assert sandbox.WORK.read_bytes() == b"operator source"


def test_shared_preparation_is_charged_to_build_not_benchmark(measurement, clock, monkeypatch):
    request, caps = measurement
    monkeypatch.setattr(sandbox, "prepare_shared", lambda **kw: clock.advance(11))
    with pytest.raises(benchmark.MeasurementFailure, match="build-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    assert exc.value.kind == "infrastructure"
    assert not caps
    assert sandbox.WORK.read_bytes() == b"operator source"


def test_calibration_consumes_the_same_budget_as_rounds(measurement, clock, monkeypatch):
    request, caps = measurement

    def calibrate(command, timeout, **kwargs):
        clock.advance(timeout)
        return 124, ""

    monkeypatch.setattr(sandbox, "run", calibrate)
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    assert exc.value.kind == "reference"
    assert not caps


@pytest.mark.parametrize("side", ["candidate", "reference"])
def test_per_run_timeout_is_explicit_and_preserves_responsible_side(monkeypatch, clock, side):
    request = {
        "timing": timing(run_seconds=2),
        "cells": [CELL],
        "seed": 42,
        "workloads": workloads(42),
    }
    monkeypatch.setattr(sandbox, "run", lambda command, timeout, **kw: (124, ""))
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout") as exc:
        benchmark.run_binary(
            Path("bench"), request, {}, 1, side, benchmark.Deadline("benchmark", 10)
        )
    assert exc.value.kind == side
    assert "2s" in str(exc.value)


def test_reference_and_candidate_share_one_build_deadline(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(sandbox, "LAYER", tmp_path / "layer")
    monkeypatch.setattr(sandbox, "wipe", lambda path: shutil.rmtree(path, ignore_errors=True))
    monkeypatch.setattr(sandbox, "foreign", lambda path: [])
    monkeypatch.setattr(sandbox, "intake", lambda *args: clock.advance(6))
    monkeypatch.setattr(sandbox, "policy", lambda *args: None)
    monkeypatch.setattr(sandbox, "statement", lambda *args: None)
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "machine")
    monkeypatch.setattr(benchmark.fingerprint, "benchmark", lambda: "benchmark")
    calls = []

    def native(*args, **kwargs):
        calls.append(args)
        clock.advance(1)
        binary = sandbox.LAYER / "upper/bin/benchExact"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"trusted test binary")
        binary.chmod(0o755)
        return 0, ""

    monkeypatch.setattr(sandbox, "own", native)
    deadline = benchmark.Deadline("build", 10)
    reference = b"reference"
    benchmark.build(reference, hashlib.sha256(reference).hexdigest(), "reference", deadline)
    # A cached executable requires no extra compile time and does not restart the phase.
    clock.advance(1)
    benchmark.build(reference, hashlib.sha256(reference).hexdigest(), "reference", deadline)
    assert len(calls) == 1 and deadline.remaining("reference") == 2
    with pytest.raises(benchmark.MeasurementFailure, match="build-timeout") as exc:
        benchmark.build(
            b"candidate", hashlib.sha256(b"candidate").hexdigest(), "candidate", deadline
        )
    assert exc.value.kind == "candidate"
    assert len(calls) == 1


def test_absolute_build_deadline_stops_expired_commands_before_spawn(monkeypatch, clock):
    sandbox._budget.reset(10, deadline=10)
    clock.advance(11)
    monkeypatch.setattr(sandbox, "run", lambda *a, **kw: pytest.fail("spawned after deadline"))
    assert sandbox.own(["untrusted"], 100)[0] == 124
    assert sandbox._budget.timed_out


def test_shared_preparation_passes_only_remaining_time(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(sandbox, "WORK", tmp_path / "work/Submission.lean")
    source = tmp_path / "shipped"
    source.write_bytes(b"operator source")
    monkeypatch.setattr(sandbox, "SHIPPED", source)
    caps = []

    def env(*args, **kwargs):
        assert kwargs["timeout"] == 10
        clock.advance(7)

    def build(*args, **kwargs):
        caps.append(args[1])
        return 0, ""

    monkeypatch.setattr(sandbox.subprocess, "run", env)
    monkeypatch.setattr(sandbox, "run", build)
    sandbox.prepare_shared(native=True, deadline=10)
    assert caps == [3]


@pytest.mark.parametrize(
    "field,value",
    [
        ("build_seconds", 0),
        ("benchmark_seconds", float("nan")),
        ("benchmark_seconds", True),
        ("build_seconds", "480"),
    ],
)
def test_invalid_phase_budgets_rejected(field, value):
    with pytest.raises(ValueError):
        validate_timing(timing(**{field: value}))


def test_supervisor_covers_both_phases_plus_shutdown_margin():
    assert supervisor_seconds(timing(build_seconds=480, benchmark_seconds=900)) == 1440


def test_real_slow_invocation_is_terminated_as_benchmark_timeout(tmp_path, monkeypatch):
    executable = tmp_path / "slow-bench"
    executable.write_text(f"#!{sys.executable}\nimport time; time.sleep(5)\n")
    executable.chmod(0o755)
    monkeypatch.setattr(sandbox, "_SANDBOX", False)
    monkeypatch.setattr(sandbox, "_BENCH_MEMORY_MB", 0)
    monkeypatch.setattr(sandbox, "_FILE_MB", 0)
    monkeypatch.setattr(sandbox, "_BENCH_CPU", "")
    request = {
        "timing": timing(run_seconds=0.05),
        "cells": [CELL],
        "seed": 42,
        "workloads": workloads(42),
    }
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout"):
        benchmark.run_binary(
            executable, request, {}, 1, "candidate", benchmark.Deadline("benchmark", 2)
        )


def test_absolute_deadline_charges_work_between_build_commands(monkeypatch, clock):
    caps = []

    def run(command, timeout, **kwargs):
        caps.append(timeout)
        clock.advance(0.25)
        return 0, ""

    monkeypatch.setattr(sandbox, "run", run)
    sandbox._budget.reset(10, deadline=10)
    sandbox.own(["first"], 100)
    clock.advance(8.75)
    sandbox.own(["second"], 100)
    assert caps == [10, 1]


def test_cached_job_skips_shared_and_native_builds(measurement, monkeypatch, clock, tmp_path):
    request, _ = measurement
    request["timing"]["cached_build_seconds"] = 2
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(sandbox, "CONFIG", config)
    paths = {}
    for key in ("candidate_hash", "reference_hash"):
        path = tmp_path / key
        path.write_bytes(key.encode())
        paths[request[key]] = path
    monkeypatch.setattr(benchmark, "cached_binary", lambda expected: paths[expected])
    monkeypatch.setattr(sandbox, "prepare_shared", lambda **kw: pytest.fail("shared rebuild"))
    monkeypatch.setattr(benchmark, "build", lambda *a: pytest.fail("native rebuild"))
    result = benchmark.measure(request, b"candidate", b"reference")
    assert result["preparation"] == {"mode": "cached", "budget_seconds": 2}
    assert result["stage_seconds"]["build"] == 0


def test_cache_evicted_after_parent_check_retries_as_infrastructure(measurement, monkeypatch):
    request, _ = measurement
    monkeypatch.setattr(benchmark, "cached_binary", lambda expected: None)
    with pytest.raises(benchmark.MeasurementFailure, match="retry with cold") as exc:
        benchmark.measure(request, b"candidate", b"reference", mode="cached")
    assert exc.value.kind == "infrastructure"


def test_cached_preparation_has_its_own_deadline(measurement, monkeypatch, clock):
    request, caps = measurement
    request["timing"]["cached_build_seconds"] = 2

    def lookup(expected):
        clock.advance(2)
        return Path(expected)

    monkeypatch.setattr(benchmark, "cached_binary", lookup)
    with pytest.raises(benchmark.MeasurementFailure, match="build-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference", mode="cached")
    assert exc.value.kind == "infrastructure"
    assert not caps


def test_cache_integrity_and_execution_identity_control_reuse(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "a" * 64)
    monkeypatch.setattr(benchmark.fingerprint, "benchmark", lambda: "b" * 64)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(sandbox, "CONFIG", config)
    expected = hashlib.sha256(b"source").hexdigest()
    cache, evidence = benchmark.cache_location(expected)
    cache.mkdir(parents=True)
    binary = cache / "bench"
    binary.write_bytes(b"valid native code")
    binary.chmod(0o755)
    request = dict(reference_hash=expected, candidate_hash=expected)
    assert benchmark.preparation_mode(request) == "cold"  # Unmanifested binary.
    manifest = evidence | {
        "bytes": binary.stat().st_size,
        "binary_sha256": benchmark.file_hash(binary),
    }
    (cache / "manifest.json").write_text(json.dumps(manifest))
    assert benchmark.preparation_mode(request) == "cached"
    binary.write_bytes(b"wrong native code")
    assert benchmark.preparation_mode(request) == "cold"
    binary.write_bytes(b"valid native code")
    binary.chmod(0o644)
    assert benchmark.preparation_mode(request) == "cold"
    binary.chmod(0o755)
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "c" * 64)
    assert benchmark.preparation_mode(request) == "cold"


def test_cold_build_is_reused_for_a_different_family(measurement, monkeypatch, clock, tmp_path):
    request, _ = measurement
    monkeypatch.setattr(benchmark, "build", NATIVE_BUILD)
    # Exercise real cache publication instead of the fixture's mocked build().
    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(sandbox, "WORK", tmp_path / "Submission.lean")
    monkeypatch.setattr(sandbox, "LAYER", tmp_path / "layer")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(sandbox, "CONFIG", config)
    monkeypatch.setattr(sandbox, "check_limits", lambda: None)
    monkeypatch.setattr(sandbox, "foreign", lambda path: [])
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "a" * 64)
    monkeypatch.setattr(benchmark.fingerprint, "benchmark", lambda: "b" * 64)
    calls = []
    monkeypatch.setattr(sandbox, "prepare_shared", lambda **kw: calls.append("shared"))
    for stage in ("intake", "policy", "statement"):
        monkeypatch.setattr(sandbox, stage, lambda *a: None)

    def native(*args, **kwargs):
        calls.append("native")
        path = sandbox.LAYER / "upper/bin/benchExact"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"native code")
        path.chmod(0o755)
        return 0, ""

    monkeypatch.setattr(sandbox, "own", native)

    def run(command, timeout, **kwargs):
        cells = kwargs["env_extra"]["BENCH_CELLS"].split(",")
        return 0, "\n".join(
            f"{w['id']}|{cell} true 100 123" for w in request["workloads"] for cell in cells
        )

    monkeypatch.setattr(sandbox, "run", run)
    first = benchmark.measure(request, b"candidate", b"reference")
    second = benchmark.measure(
        request | {"cells": ["resnet:b1:normal"]}, b"candidate", b"reference"
    )
    assert first["preparation"]["mode"] == "cold"
    assert second["preparation"]["mode"] == "cached"
    assert calls == ["shared", "native", "native"]


def test_warm_supervisor_uses_warm_limit():
    assert (
        supervisor_seconds(
            timing(build_seconds=960, cached_build_seconds=180, benchmark_seconds=360), "cached"
        )
        == 600
    )


def test_worker_freezes_preparation_mode_for_child_and_watchdog(monkeypatch):
    import json
    from types import SimpleNamespace

    from workers import benchmark_worker as worker

    monkeypatch.setattr(worker, "preparation_mode", lambda request: "cached")
    monkeypatch.setattr(worker, "report", lambda *a: None)
    repo = SimpleNamespace(
        sources=lambda job_id: (b"candidate", b"reference"),
        complete=lambda *a: True,
        fail=lambda *a: pytest.fail("unexpected job failure"),
    )
    job = SimpleNamespace(
        id=1,
        token="lease-token",
        lease_seconds=120,
        request={"timing": timing(cached_build_seconds=2)},
    )
    seen = []

    def supervised(command, heartbeat, seconds, *args, **kwargs):
        seen.append((command, seconds))
        Path(command[command.index("--result") + 1]).write_text(json.dumps({"result": {}}))

    monkeypatch.setattr(worker, "supervised", supervised)
    worker.execute(repo, job, "owner")
    command, seconds = seen[0]
    assert command[command.index("--preparation-mode") + 1] == "cached"
    assert seconds == 66  # Two seconds cached preparation, four measurement, sixty grace.
