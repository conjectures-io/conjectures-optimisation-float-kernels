"""Build and measurement time are bounded independently; samples use fresh processes."""

import hashlib
import json
import shutil
import struct
import sys
import time
from pathlib import Path

import benchmark
import pytest
import sandbox
import scoring
from multiseed import workloads
from sandbox import CgroupMemorySampler
from timing import supervisor_seconds, validate_timing

CELL = "mlp:b1:normal"
NATIVE_BUILD = benchmark.build


def emit_timing_markers(output, callback):
    if callback is not None:
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 4 and fields[1] == "true":
                callback(f"BENCH_READY|{fields[0]}", time.monotonic_ns())
                callback(f"BENCH_DONE|{fields[0]}", time.monotonic_ns())
    return output


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
    return dict(rounds=1, inner=1, run_seconds=10, build_seconds=10, benchmark_seconds=4) | extra


def coordinator_input_plan(request):
    seed = 1000
    plan = {}
    for draw in request["workloads"]:
        for cell in request["cells"]:
            rounds = []
            for _ in range(request["timing"]["rounds"]):
                passes = []
                for _ in range(request["timing"]["inner"]):
                    passes.append([seed])
                    seed += 1
                rounds.append(passes)
            plan[f"{draw['id']}|{cell}"] = rounds
    return plan


@pytest.fixture
def measurement(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(sandbox, "WORK", tmp_path / "Submission.lean")
    config = tmp_path / "lake-config"
    config.mkdir()
    monkeypatch.setattr(sandbox, "CONFIG", config)
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

    def build(source, expected, side, deadline, **_kwargs):
        clock.advance(3 if side == "reference" else 4)
        deadline.remaining(side)
        return binaries[side]

    monkeypatch.setattr(benchmark, "build", build)
    caps = []

    def run(command, timeout, **kwargs):
        caps.append(timeout)
        clock.advance(1 if kwargs["env_extra"]["BENCH_REPS"] else 0.5)
        output = "\n".join(f"{w['id']}|{CELL} true 100 123" for w in workloads(42))
        return 0, emit_timing_markers(output, kwargs.get("on_output_line"))

    monkeypatch.setattr(sandbox, "run", run)
    return request, caps


def test_build_does_not_consume_measurement_time_and_durations_are_recorded(measurement):
    request, caps = measurement
    result = benchmark.measure(request, b"candidate", b"reference")
    assert caps == pytest.approx([4, 3])
    assert {
        key: result["stage_seconds"][key]
        for key in ("build", "baseline_process", "rounds", "benchmark")
    } == pytest.approx(dict(build=9, baseline_process=0, rounds=2, benchmark=2))
    assert result["stage_seconds"]["build_stages"] == {
        "shared": {
            "status": "complete",
            "budget_seconds": 10,
            "seconds": 2,
            "cache_lookup_seconds": 0,
            "compile_seconds": 2,
            "other_seconds": 0,
        },
        "baseline": {
            "status": "skipped",
            "seconds": 0.0,
            "cache_lookup_seconds": 0.0,
            "compile_seconds": 0.0,
            "other_seconds": 0.0,
        },
        "reference": {
            "status": "complete",
            "budget_seconds": 10,
            "seconds": 3,
            "cache_lookup_seconds": 0,
            "compile_seconds": 3,
            "other_seconds": 0,
        },
        "candidate": {
            "status": "complete",
            "budget_seconds": 10,
            "seconds": 4,
            "cache_lookup_seconds": 0,
            "compile_seconds": 4,
            "other_seconds": 0,
        },
    }
    assert result["timing_seconds"] == pytest.approx(2)
    assert sandbox.WORK.read_bytes() == b"operator source"


def test_make_input_sequences_uses_coordinator_seed_plan(measurement, monkeypatch):
    request, _ = measurement
    plan = {
        f"{draw['id']}|{CELL}": [[[100 + i], [200 + i]]]
        for i, draw in enumerate(request["workloads"])
    }
    request["synthetic_input_plan"] = plan
    monkeypatch.setattr(
        benchmark.secrets,
        "randbits",
        lambda _bits: pytest.fail("random input seed used despite coordinator plan"),
    )

    selected = benchmark.make_input_sequences(
        request, {key: 1 for key in plan}, 2, real_round_index=0
    )

    assert selected == {key: values[0] for key, values in plan.items()}


def test_fresh_pass_runs_and_times_frozen_warmups_before_scored_rows(measurement, monkeypatch):
    request, _ = measurement
    request["warmup_policy"] = "independent-unscored-input-v1"
    reps = {f"{draw['id']}|{CELL}": 1 for draw in request["workloads"]}
    real_id = "fixture-mlp"
    real_key = f"{real_id}|mlp"
    request["real_workloads"] = [
        {
            "id": real_id,
            "adapter": "image-zoo-v1",
            "family": "mlp",
            "weights_path": "assets/weights.bin",
            "samples_path": "assets/samples.bin",
            "sample_words": 2,
            "sample_count": 2,
            "samples_per_run": 1,
        }
    ]
    (sandbox.HERE / "assets").mkdir()
    (sandbox.HERE / "assets/weights.bin").write_bytes(b"weights")
    (sandbox.HERE / "assets/samples.bin").write_bytes(struct.pack("<4f", 1.0, 2.0, 3.0, 4.0))
    synthetic_keys = list(reps)
    request["synthetic_input_plan"] = {
        key: [[[1000 + index]]] for index, key in enumerate(synthetic_keys)
    }
    request["synthetic_warmup_plan"] = {
        key: [[[50_000 + index]]] for index, key in enumerate(synthetic_keys)
    }
    request["real_sample_plan"] = {real_id: [[[0]]]}
    request["real_warmup_plan"] = {real_id: [[[1]]]}
    inputs = benchmark.make_input_sequences(request, reps, 1, real_round_index=0)
    warmups = benchmark.make_warmup_sequences(request, reps, 1, real_round_index=0)
    binary = Path("unused-binary")
    binary.write_text("binary")

    def run(_command, _timeout, **kwargs):
        assert (
            kwargs["input_file"].read_text()
            != Path(kwargs["env_extra"]["BENCH_WARMUP_INPUT_SEED_FILE"]).read_text()
        )
        staged = kwargs["asset_files"]
        header = struct.pack("<I", 2)
        assert Path(staged[f"{real_id}-selected-input"]).read_bytes() == (
            header + struct.pack("<2f", 1.0, 2.0)
        )
        assert Path(staged[f"{real_id}-warmup-input"]).read_bytes() == (
            header + struct.pack("<2f", 3.0, 4.0)
        )
        output = [f"REAL_PREP|{real_id}|100|200"]
        timestamp = 1_000
        for printed_key, _public_key in benchmark.row_names(request).items():
            callback = kwargs["on_output_line"]
            callback(f"BENCH_WARMUP_READY|{printed_key}", timestamp)
            timestamp += 1_000
            callback(f"BENCH_WARMUP_DONE|{printed_key}", timestamp)
            output.append(f"WARMUP|{printed_key} true 800 22")
            timestamp += 1_000
            callback(f"BENCH_READY|{printed_key}", timestamp)
            timestamp += 2_000
            callback(f"BENCH_DONE|{printed_key}", timestamp)
            output.append(f"{printed_key} true 2000 42")
            timestamp += 1_000
        return 0, "\n".join(output)

    monkeypatch.setattr(sandbox, "run", run)
    result = benchmark.run_binary(
        binary,
        request,
        reps,
        1,
        "reference",
        benchmark.Deadline("benchmark", 10),
        inputs,
        warmup_sequences=warmups,
    )
    assert set(result["__warmup_ns__"]) == {*reps, real_key}
    assert all(samples == [1_000] for samples in result["__warmup_ns__"].values())


def test_warmup_rows_are_frozen_timed_telemetry_and_excluded_from_admission(
    measurement, monkeypatch, clock
):
    request, _ = measurement
    request.update(suites=["synthetic"], warmup_policy="independent-unscored-input-v1")
    request["synthetic_input_plan"] = coordinator_input_plan(request)
    request["synthetic_warmup_plan"] = {
        key: [[[50_000 + index]]] for index, key in enumerate(request["synthetic_input_plan"])
    }

    def run(_command, _timeout, **kwargs):
        clock.advance(0.001)
        output = []
        timestamp = 1_000
        for printed_key in benchmark.row_names(request):
            callback = kwargs["on_output_line"]
            callback(f"BENCH_WARMUP_READY|{printed_key}", timestamp)
            timestamp += 100
            callback(f"BENCH_WARMUP_DONE|{printed_key}", timestamp)
            output.append(f"WARMUP|{printed_key} true 90 123")
            timestamp += 100
            callback(f"BENCH_READY|{printed_key}", timestamp)
            timestamp += 200
            callback(f"BENCH_DONE|{printed_key}", timestamp)
            output.append(f"{printed_key} true 200 123")
            timestamp += 100
        return 0, "\n".join(output)

    monkeypatch.setattr(sandbox, "run", run)
    result = benchmark.measure(request, b"candidate", b"reference")
    scoring.validate_result(request, result)
    assert result["measurement_policy"]["unscored_warmup_per_row_per_process"] is True
    assert all(
        values == [100]
        for side in result["rounds"][0]["warmup_ns"].values()
        for values in side.values()
    )
    assert "warmup_ns" not in result["rounds"][0]["cells"][next(iter(result["rounds"][0]["cells"]))]


def test_real_warmup_measurement_passes_result_validation(measurement, monkeypatch, clock):
    request, _ = measurement
    request.update(suites=["synthetic", "real"], warmup_policy="independent-unscored-input-v1")
    request["synthetic_input_plan"] = coordinator_input_plan(request)
    request["synthetic_warmup_plan"] = {
        key: [[[50_000 + index]]] for index, key in enumerate(request["synthetic_input_plan"])
    }
    real_id = "fixture-mlp"
    assets = Path("assets")
    (sandbox.HERE / assets).mkdir()
    weights = b"fixture weights"
    samples = struct.pack("<2f", 1.0, 2.0)
    (sandbox.HERE / assets / "weights.bin").write_bytes(weights)
    (sandbox.HERE / assets / "samples.bin").write_bytes(samples)
    request["real_workloads"] = [
        {
            "id": real_id,
            "family": "mlp",
            "model": "fixture-mlp",
            "dataset": "fixture",
            "adapter": "image-zoo-v1",
            "weights_path": "assets/weights.bin",
            "weights_sha256": hashlib.sha256(weights).hexdigest(),
            "samples_path": "assets/samples.bin",
            "samples_sha256": hashlib.sha256(samples).hexdigest(),
            "asset_revision": "a" * 64,
            "sample_count": 2,
            "sample_words": 1,
            "samples_per_run": 1,
        }
    ]
    request["real_sample_plan"] = {real_id: [[[0]]]}
    request["real_warmup_plan"] = {real_id: [[[1]]]}
    monkeypatch.setattr(benchmark.RealWorkloadSpec, "validate_assets", lambda *_args: None)

    def run(_command, _timeout, **kwargs):
        clock.advance(0.001)
        output = [f"REAL_PREP|{real_id}|100|200"]
        timestamp = 1_000
        for printed_key in benchmark.row_names(request):
            callback = kwargs["on_output_line"]
            callback(f"BENCH_WARMUP_READY|{printed_key}", timestamp)
            timestamp += 100
            callback(f"BENCH_WARMUP_DONE|{printed_key}", timestamp)
            output.append(f"WARMUP|{printed_key} true 90 123")
            timestamp += 100
            callback(f"BENCH_READY|{printed_key}", timestamp)
            timestamp += 200
            callback(f"BENCH_DONE|{printed_key}", timestamp)
            output.append(f"{printed_key} true 200 123")
            timestamp += 100
        return 0, "\n".join(output)

    monkeypatch.setattr(sandbox, "run", run)
    result = benchmark.measure(request, b"candidate", b"reference")
    scoring.validate_result(request, result)
    observation = result["real"]["rounds"][0]["workloads"][real_id]
    assert observation["reference_warmup_ns"] == 100
    assert observation["candidate_warmup_ns"] == 100


def test_candidate_build_has_an_independent_timeout_and_partial_stage_record(measurement):
    request, _ = measurement
    request["timing"]["candidate_build_seconds"] = 2
    with pytest.raises(benchmark.MeasurementFailure, match="candidate_build-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    assert exc.value.kind == "candidate"
    assert exc.value.timeout["phase"] == "candidate_build"
    assert exc.value.timeout["configured_phase_seconds"] == 2
    assert not exc.value.timeout["bounded_by_overall"]
    assert exc.value.partial["build_stages"]["shared"]["status"] == "complete"
    assert exc.value.partial["build_stages"]["reference"]["status"] == "complete"
    assert exc.value.partial["build_stages"]["candidate"]["status"] == "failed"


def test_build_stage_evidence_is_bound_to_the_scoring_request(measurement):
    request, _ = measurement
    request["suites"] = ["synthetic"]
    request["synthetic_input_plan"] = coordinator_input_plan(request)
    result = benchmark.measure(request, b"candidate", b"reference")
    scoring.validate_result(request, result)
    changed_input = json.loads(json.dumps(result))
    key = next(iter(changed_input["rounds"][0]["input_plan"]["synthetic"]))
    changed_input["rounds"][0]["input_plan"]["synthetic"][key][0] = 2**32 - 1
    with pytest.raises(ValueError, match="differ from coordinator plan"):
        scoring.validate_result(request, changed_input)
    result["preparation"]["stage_budgets_seconds"]["candidate"] += 1
    with pytest.raises(ValueError, match="build phase caps"):
        scoring.validate_result(request, result)


def test_build_wall_time_accounts_for_between_stage_overhead(measurement):
    request, _ = measurement
    request["suites"] = ["synthetic"]
    request["synthetic_input_plan"] = coordinator_input_plan(request)
    result = benchmark.measure(request, b"candidate", b"reference")

    result["stage_seconds"]["build"] += 0.05
    result["stage_seconds"]["build_overhead"] = 0.05
    scoring.validate_result(request, result)

    result["stage_seconds"]["build_overhead"] = 0
    with pytest.raises(ValueError, match="overall build timing evidence"):
        scoring.validate_result(request, result)


def test_measure_emits_incremental_build_and_side_progress(measurement):
    request, _ = measurement
    events = []
    benchmark.measure(request, b"candidate", b"reference", progress_callback=events.append)
    assert [event["stage"] for event in events[:4]] == [
        "shared",
        "baseline",
        "reference",
        "candidate",
    ]
    assert [event["side"] for event in events[4:6]] == ["reference", "candidate"]
    assert events[4]["observations"] and events[5]["observations"]


def test_shared_preparation_is_charged_to_build_not_benchmark(measurement, clock, monkeypatch):
    request, caps = measurement
    monkeypatch.setattr(sandbox, "prepare_shared", lambda **kw: clock.advance(11))
    with pytest.raises(benchmark.MeasurementFailure, match="shared_build-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    assert exc.value.kind == "infrastructure"
    assert not caps
    assert sandbox.WORK.read_bytes() == b"operator source"


def test_first_paired_process_consumes_the_benchmark_budget(measurement, clock, monkeypatch):
    request, caps = measurement

    def calibrate(command, timeout, **kwargs):
        clock.advance(timeout)
        return 124, ""

    monkeypatch.setattr(sandbox, "run", calibrate)
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    assert exc.value.kind == "reference"
    assert not caps


@pytest.mark.parametrize("slow", ["candidate", "reference"])
def test_shared_round_deadline_blames_the_side_that_used_it(measurement, clock, monkeypatch, slow):
    # The deadline runs out during the fast side's run: the slow side is still at fault.
    request, _ = measurement
    request["timing"] = timing(rounds=2, benchmark_seconds=5)
    rows = "\n".join(f"{w['id']}|{CELL} true 100 123" for w in workloads(42))

    def run(command, timeout, **kwargs):
        seconds = 3 if Path(command[-1]).name == slow else 1
        clock.advance(min(seconds, timeout))
        return (
            (124, "")
            if seconds > timeout
            else (
                0,
                emit_timing_markers(rows, kwargs.get("on_output_line")),
            )
        )

    monkeypatch.setattr(sandbox, "run", run)
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    assert exc.value.kind == slow


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


def test_a_stopped_run_keeps_the_rows_it_finished_and_names_the_cell_it_was_on(monkeypatch, clock):
    request = {
        "timing": timing(run_seconds=2),
        "cells": [CELL, "resnet:b1:normal"],
        "seed": 42,
        "workloads": workloads(42)[:1],
    }
    w = request["workloads"][0]["id"]
    # Markers come from stderr, rows from stdout; the kill cut the second cell's row short.
    output = "\n".join(
        [f"{w}|{CELL} start", f"{w}|{CELL} true 100 123", f"{w}|cnn:b1:normal start", f"{w}|cnn:b1"]
    )
    monkeypatch.setattr(sandbox, "run", lambda command, timeout, **kw: (124, output))
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout") as exc:
        benchmark.run_binary(
            Path("bench"), request, {}, 1, "candidate", benchmark.Deadline("benchmark", 10)
        )
    assert exc.value.error() == {
        "kind": "candidate",
        "reason": "benchmark-timeout: candidate invocation exceeded its 2s time allowance",
        "timeout": {
            "phase": "benchmark",
            "limit": "invocation",
            "side": "candidate",
            "running": "candidate",
            "budget_seconds": 10,
            "elapsed_seconds": 0,
            "stopped_after_seconds": 0,
            "allowance_seconds": 2,
            "peak_memory_bytes": None,
            "cell": f"{w}|resnet:b1:normal",
            "cells_finished": 1,
            "cells_total": 2,
            "finished": {f"{w}|{CELL}": [100, 123]},
        },
    }


def test_start_markers_are_not_rows(monkeypatch, clock):
    request = {"timing": timing(), "cells": [CELL], "seed": 42, "workloads": workloads(42)}
    output = "\n".join(
        line
        for w in workloads(42)
        for line in (f"{w['id']}|{CELL} start", f"{w['id']}|{CELL} true 100 123")
    )
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda command, timeout, **kw: (0, emit_timing_markers(output, kw.get("on_output_line"))),
    )
    rows = benchmark.run_binary(
        Path("bench"), request, {}, 1, "reference", benchmark.Deadline("benchmark", 10)
    )
    expected_checksum = ((2166136261 ^ 123) * 16777619) % 2**32
    assert all(
        rows[f"{w['id']}|{CELL}"][0] > 0 and rows[f"{w['id']}|{CELL}"][1] == expected_checksum
        for w in workloads(42)
    )


def test_sandbox_streams_flushed_lines_to_the_trusted_timer(monkeypatch):
    monkeypatch.setattr(sandbox, "_SANDBOX", False)
    monkeypatch.setattr(sandbox, "_BENCH_MEMORY_MB", 0)
    monkeypatch.setattr(sandbox, "_FILE_MB", 0)
    events = []
    script = (
        "import sys,time; "
        "print('BENCH_READY|row', flush=True); time.sleep(0.03); "
        "print('BENCH_DONE|row', flush=True)"
    )
    resource_usage = {}
    code, output = sandbox.run(
        [sys.executable, "-c", script],
        3,
        memory_mb=0,
        on_output_line=lambda line, received_ns: events.append((line, received_ns)),
        resource_usage=resource_usage,
    )
    assert code == 0
    assert [line for line, _ in events] == ["BENCH_READY|row", "BENCH_DONE|row"]
    assert events[0][1] < events[1][1]
    assert output.splitlines() == [line for line, _ in events]
    assert resource_usage["peak_memory_bytes"] is None


def test_cgroup_peak_memory_sampler_reads_v2_peak_counter(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    process = proc_root / "42"
    scope = cgroup_root / "user.slice" / "worker.scope"
    process.mkdir(parents=True)
    scope.mkdir(parents=True)
    (process / "cgroup").write_text("0::/user.slice/worker.scope\n")
    counter = scope / "memory.peak"
    counter.write_text("123456\n")

    sampler = CgroupMemorySampler(proc_root, cgroup_root)
    sampler.sample(42)
    counter.write_text("234567\n")
    sampler.sample(42)

    assert sampler.peak_bytes == 234567


def test_cgroup_memory_sampler_reads_v2_scope_limit(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    process = proc_root / "42"
    scope = cgroup_root / "user.slice" / "worker.scope"
    process.mkdir(parents=True)
    scope.mkdir(parents=True)
    (process / "cgroup").write_text("0::/user.slice/worker.scope\n")
    (scope / "memory.max").write_text(str(512 * 2**20))

    sampler = CgroupMemorySampler(proc_root, cgroup_root)
    assert sampler.memory_limit_bytes(42) == 512 * 2**20


def test_cgroup_memory_sampler_reports_missing_v2_controller(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    process = proc_root / "42"
    scope = cgroup_root / "user.slice" / "worker.scope"
    process.mkdir(parents=True)
    scope.mkdir(parents=True)
    (process / "cgroup").write_text("0::/user.slice/worker.scope\n")
    (scope / "cgroup.controllers").write_text("cpu pids\n")

    sampler = CgroupMemorySampler(proc_root, cgroup_root)
    assert sampler.memory_limit_bytes(42) is None


def test_memory_scope_preflight_rejects_an_unapplied_systemd_property(monkeypatch):
    class FinishedScope:
        pid = 42
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def communicate(timeout=None):
            return "", None

    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *_args, **_kwargs: FinishedScope())
    monkeypatch.setattr(CgroupMemorySampler, "memory_limit_bytes", lambda *_args: None)
    with pytest.raises(RuntimeError, match="accepted MemoryMax but.*no effective cgroup"):
        sandbox._check_memory_scope(["systemd-run"], 512)


def test_cgroup_peak_memory_sampler_uses_child_scope_not_worker_cgroup(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    parent = proc_root / "41"
    child = proc_root / "42"
    (parent / "task" / "41").mkdir(parents=True)
    child.mkdir(parents=True)
    (parent / "task" / "41" / "children").write_text("42\n")
    (parent / "cgroup").write_text("0::/user.slice/worker.service\n")
    (child / "cgroup").write_text("0::/user.slice/worker.service/run-1.scope\n")
    scope = cgroup_root / "user.slice" / "worker.service" / "run-1.scope"
    scope.mkdir(parents=True)
    (scope / "memory.peak").write_text("345678\n")

    sampler = CgroupMemorySampler(proc_root, cgroup_root)
    sampler.sample(41)

    assert sampler.peak_bytes == 345678
    assert sampler.counter == scope / "memory.peak"


def test_cgroup_memory_sampler_does_not_mislabel_current_as_peak(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    process = proc_root / "43"
    scope = cgroup_root / "user.slice" / "worker.scope"
    process.mkdir(parents=True)
    scope.mkdir(parents=True)
    (process / "cgroup").write_text("0::/user.slice/worker.scope\n")
    counter = scope / "memory.current"
    counter.write_text("100\n")

    sampler = CgroupMemorySampler(proc_root, cgroup_root)
    sampler.sample(43)
    counter.write_text("250\n")
    sampler.sample(43)

    assert sampler.peak_bytes is None


def test_scored_duration_uses_parent_markers_not_child_duration(monkeypatch, clock):
    draw = workloads(42)[0]
    key = f"{draw['id']}|{CELL}"
    request = {"timing": timing(), "cells": [CELL], "seed": 42, "workloads": [draw]}

    def run(command, timeout, **kwargs):
        callback = kwargs["on_output_line"]
        callback(f"BENCH_READY|{key}", 10_000)
        callback(f"BENCH_DONE|{key}", 110_000)
        kwargs["resource_usage"]["peak_memory_bytes"] = 987654
        return 0, f"{key} true 999999 123"

    monkeypatch.setattr(sandbox, "run", run)
    result = benchmark.run_binary(
        Path("bench"), request, {key: 1}, 1, "candidate", benchmark.Deadline("benchmark", 10)
    )
    assert result[key][0] == 100_000
    assert result["__child_ns__"][key] == [999999]
    assert result["__peak_memory_bytes__"] == [987654]


def test_each_sample_uses_a_new_process_and_distinct_seed_file(monkeypatch, clock):
    draws = workloads(42)
    request = {"timing": timing(inner=3), "cells": [CELL], "seed": 42, "workloads": draws}
    keys = {f"{draw['id']}|{CELL}": 1 for draw in draws}
    inputs = benchmark.make_input_sequences(request, keys, 3)
    calls = []
    output = "\n".join(f"{draw['id']}|{CELL} true 100 123" for draw in draws)

    def run(command, timeout, **kwargs):
        calls.append((kwargs["env_extra"]["BENCH_INNER"], kwargs["input_file"].read_text()))
        return 0, emit_timing_markers(output, kwargs.get("on_output_line"))

    monkeypatch.setattr(sandbox, "run", run)
    rows = benchmark.run_binary(
        Path("bench"), request, keys, 3, "candidate", benchmark.Deadline("benchmark", 10), inputs
    )
    assert len(calls) == 3
    assert all(inner == "1" for inner, _ in calls)
    assert len({seeds for _, seeds in calls}) == 3
    assert all(
        len(rows["__raw_ns__"][f"{draw['id']}|{CELL}"]) == 3
        and all(value > 0 for value in rows["__raw_ns__"][f"{draw['id']}|{CELL}"])
        for draw in draws
    )
    for draw in draws:
        samples = [inputs[f"{draw['id']}|{CELL}"][i][0] for i in range(3)]
        assert len(set(samples)) == 3


def test_real_sample_selection_can_recur_across_fresh_processes():
    spec = {"id": "har-mlp", "family": "mlp", "samples_per_run": 1, "sample_count": 1}
    request = {"workloads": [], "cells": [], "real_workloads": [spec]}
    selected = benchmark.make_input_sequences(request, {}, 3)["har-mlp|mlp"]
    assert selected == [[0], [0], [0]]


def test_real_sample_plan_overrides_worker_random_sampling():
    request = {
        "workloads": [],
        "cells": [],
        "real_workloads": [
            {"id": "har-mlp", "family": "mlp", "samples_per_run": 1, "sample_count": 12}
        ],
        "real_sample_plan": {"har-mlp": [[[8], [3]]]},
    }
    selected = benchmark.make_input_sequences(request, {}, 2, real_round_index=0)
    assert selected["har-mlp|mlp"] == [[8], [3]]


def test_planted_in_memory_memo_cannot_survive_between_samples(tmp_path, monkeypatch):
    # This executable mimics the reported attack: repeat the same request and return a cached
    # result after its first call. The harness must start it afresh for each timed sample.
    executable = tmp_path / "memo-bench"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "memo = {}\n"
        "seed_file = os.environ['BENCH_INPUT_SEED_FILE']\n"
        "seed = open(seed_file).read().strip().split('=')[1]\n"
        "cached = seed in memo\n"
        "memo[seed] = True\n"
        "key = 's0|mlp:b1:normal'\n"
        "print('BENCH_READY|' + key, flush=True)\n"
        "print('BENCH_DONE|' + key, flush=True)\n"
        "print('s0|mlp:b1:normal true ' + ('1' if cached else '100') + ' 123')\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(sandbox, "_SANDBOX", False)
    monkeypatch.setattr(sandbox, "_BENCH_MEMORY_MB", 0)
    monkeypatch.setattr(sandbox, "_FILE_MB", 0)
    monkeypatch.setattr(sandbox, "_BENCH_CPU", "")
    draw = workloads(42)[0]
    request = {"timing": timing(inner=3), "cells": [CELL], "seed": 42, "workloads": [draw]}
    # Intentionally keep the input identical: this isolates process reset from seed randomization.
    inputs = {f"{draw['id']}|{CELL}": [[42], [42], [42]]}
    result = benchmark.run_binary(
        executable,
        request,
        {f"{draw['id']}|{CELL}": 1},
        3,
        "candidate",
        benchmark.Deadline("benchmark", 10),
        inputs,
    )
    assert result["__child_ns__"][f"{draw['id']}|{CELL}"] == [100, 100, 100]
    assert len(result["__raw_ns__"][f"{draw['id']}|{CELL}"]) == 3
    assert all(value > 0 for value in result["__raw_ns__"][f"{draw['id']}|{CELL}"])


def test_a_timeout_keeps_what_the_measurement_had_reached(measurement, clock, monkeypatch):
    # Round two runs the candidate first; the reference then runs out of the shared deadline,
    # which the slower candidate used most of.
    request, _ = measurement
    request["timing"] = timing(rounds=2, benchmark_seconds=6.5)
    rows = "\n".join(f"{w['id']}|{CELL} true 100 123" for w in workloads(42))

    def run(command, timeout, **kwargs):
        seconds = 3 if Path(command[-1]).name == "candidate" else 1
        clock.advance(min(seconds, timeout))
        return (
            (124, "")
            if seconds > timeout
            else (
                0,
                emit_timing_markers(rows, kwargs.get("on_output_line")),
            )
        )

    monkeypatch.setattr(sandbox, "run", run)
    with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    error = json.loads(json.dumps(exc.value.error()))
    assert error["kind"] == "candidate"
    timeout, partial = error["timeout"], error["partial"]
    assert {k: timeout[k] for k in ("limit", "side", "running", "allowance_seconds")} == {
        "limit": "budget",
        "side": "candidate",
        "running": "candidate",
        "allowance_seconds": 2.5,
    }
    # The first paired round completed before the second candidate process timed out.
    assert timeout["runs"] == {
        "reference": {"finished": 1, "mean_seconds": 1},
        "candidate": {"finished": 1, "mean_seconds": 3},
    }
    assert timeout["stopped_after_seconds"] == 2.5
    names = [f"{w['id']}|{CELL}" for w in workloads(42)]
    assert set(partial["repetitions"]) == set(names)
    assert [r["reference_first"] for r in partial["rounds"]] == [True]
    assert set(partial["round_in_progress"]) == {"input_plan"}
    assert partial["rounds_planned"] == 2
    assert partial["build_seconds"] == 9 and partial["benchmark_seconds"] == 6.5
    assert sandbox.WORK.read_bytes() == b"operator source"


def test_a_crash_keeps_the_measurement_but_is_no_timeout(measurement, monkeypatch):
    request, _ = measurement
    rows = "\n".join(f"{w['id']}|{CELL} true 100 123" for w in workloads(42))

    def run(command, timeout, **kwargs):
        return (
            (1, "panic")
            if Path(command[-1]).name == "candidate"
            else (
                0,
                emit_timing_markers(rows, kwargs.get("on_output_line")),
            )
        )

    monkeypatch.setattr(sandbox, "run", run)
    with pytest.raises(benchmark.MeasurementFailure, match="bench exit 1") as exc:
        benchmark.measure(request, b"candidate", b"reference")
    error = exc.value.error()
    assert error["kind"] == "candidate" and "timeout" not in error
    assert error["partial"]["rounds"] == [] and set(error["partial"]["round_in_progress"]) == {
        "input_plan",
        "reference",
    }


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


def test_sandboxed_run_removes_writable_config_scratch_after_child_exit(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "lakefile.olean").write_text("read only source")
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("s0|mlp:b1:normal=42\n")
    asset_file = tmp_path / "weights.bin"
    asset_file.write_bytes(b"fixture")
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(sandbox, "CONFIG", config)
    monkeypatch.setattr(sandbox, "SCRATCH", scratch)
    monkeypatch.setattr(sandbox, "HERE", tmp_path / "repo")
    monkeypatch.setattr(sandbox, "LIB", tmp_path / "lean")
    monkeypatch.setattr(sandbox, "ELAN", tmp_path / "elan")
    monkeypatch.setattr(sandbox, "_SYSTEM", ())
    monkeypatch.setattr(sandbox, "_SANDBOX", True)
    monkeypatch.setattr(sandbox, "require_backend", lambda: None)

    class Child:
        pid = 0
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, input=None, timeout=None):
            (scratch / "writable-marker").write_text("created by child")
            return "ok", None

    spawned = {}

    def popen(argv, **kwargs):
        spawned.update(argv=argv, env=kwargs["env"])
        return Child()

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    removed = []

    def wipe(path):
        removed.append(path)
        shutil.rmtree(path, ignore_errors=True)

    monkeypatch.setattr(sandbox, "wipe", wipe)
    code, output = sandbox.run(
        ["true"], 10, input_file=seed_file, asset_files={"weights": asset_file}
    )
    assert (code, output) == (0, "ok")
    assert any(
        spawned["argv"][i : i + 3]
        == ["--ro-bind", str(seed_file.resolve()), "/benchmark-input-seeds"]
        for i in range(len(spawned["argv"]) - 2)
    )
    assert any(
        spawned["argv"][i : i + 3]
        == ["--ro-bind", str(asset_file.resolve()), "/benchmark-assets/weights.bin"]
        for i in range(len(spawned["argv"]) - 2)
    )
    assert spawned["env"]["BENCH_INPUT_SEED_FILE"] == "/benchmark-input-seeds"
    assert removed[-1] == scratch
    assert not scratch.exists()


def test_measure_reports_paired_real_rows_and_baseline_grounding(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(sandbox, "WORK", tmp_path / "lean/work/Submission.lean")
    monkeypatch.setattr(sandbox, "check_limits", lambda: None)
    monkeypatch.setattr(sandbox, "prepare_shared", lambda **kw: None)
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "a" * 64)
    monkeypatch.setattr(benchmark.fingerprint, "benchmark", lambda: "b" * 64)
    baseline_path = tmp_path / "kernels/exact/baseline/Kernel.lean"
    baseline_path.parent.mkdir(parents=True)
    baseline_path.write_bytes(b"baseline source")
    weights_asset = tmp_path / "weights.bin"
    samples_asset = tmp_path / "samples.bin"
    tensors = [32 * 784, 32, 16 * 32, 16, 10 * 16, 10]
    with weights_asset.open("wb") as stream:
        for count in tensors:
            stream.write(struct.pack("<I", count))
            stream.write(bytes(count * 4))
    samples_asset.write_bytes(bytes(2 * 784 * 4))
    real = {
        "id": "digits-mlp",
        "family": "mlp",
        "model": "small-mlp",
        "dataset": "digits",
        "adapter": "image-zoo-v1",
        "weights_path": "weights.bin",
        "weights_sha256": benchmark.file_hash(weights_asset),
        "samples_path": "samples.bin",
        "samples_sha256": benchmark.file_hash(samples_asset),
        "asset_revision": "fixture-v1",
        "sample_count": 2,
        "sample_words": 784,
        "samples_per_run": 1,
    }
    request = {
        "environment": "test",
        "identity": "a" * 64,
        "seed": 42,
        "candidate_hash": hashlib.sha256(b"candidate source").hexdigest(),
        "reference_hash": hashlib.sha256(b"reference source").hexdigest(),
        "baseline_hash": hashlib.sha256(b"baseline source").hexdigest(),
        "benchmark_identity": "b" * 64,
        "cells": [CELL],
        "timing": timing(rounds=2, inner=1, baseline_rounds=2),
        "workloads": workloads(42),
        "suites": ("synthetic", "real"),
        "real_workloads": [real],
        "real_sample_plan": {"digits-mlp": [[[1]], [[0]]]},
    }
    request["synthetic_input_plan"] = coordinator_input_plan(request)
    ticks = iter(range(0, 10**9))
    monkeypatch.setattr(benchmark.time, "monotonic_ns", lambda: next(ticks))
    binaries = {}
    run_counts = {"baseline": 0, "reference": 0, "candidate": 0}

    def build(source, expected, side, deadline, **_kwargs):
        deadline.remaining(side)
        path = tmp_path / f"{side}.bin"
        path.write_bytes(source)
        binaries[side] = path
        return path

    def run(command, timeout, **kwargs):
        clock.advance(0.01)
        side = Path(command[-1]).stem
        repetition = run_counts[side]
        run_counts[side] += 1
        env = kwargs["env_extra"]
        synthetic = [item.split(":", 1)[0] for item in env["BENCH_WORKLOADS"].split(",")]
        synthetic_ns = 300 + repetition if side == "baseline" else 100 + repetition
        real_ns = 320 + repetition if side == "baseline" else 120 + repetition
        prep_ns = 300 + repetition if side == "baseline" else 200 + repetition
        rows = [f"{name}|{CELL} true {synthetic_ns} 123" for name in synthetic]
        rows.append(f"digits-mlp|{CELL} true {real_ns} 123")
        rows.append(f"REAL_PREP|digits-mlp|{prep_ns}|{50 + repetition}")
        output = "\n".join(rows)
        elapsed_ns = 0
        for row in rows:
            fields = row.split()
            if len(fields) == 4 and fields[1] == "true":
                kwargs["on_output_line"](f"BENCH_READY|{fields[0]}", elapsed_ns)
                elapsed_ns += int(fields[2])
                kwargs["on_output_line"](f"BENCH_DONE|{fields[0]}", elapsed_ns)
        return 0, output

    monkeypatch.setattr(benchmark, "build", build)
    monkeypatch.setattr(sandbox, "run", run)
    result = benchmark.measure(request, b"candidate source", b"reference source", mode="cold")
    assert result["baseline"]["summary"][f"s0|{CELL}"]["baseline_median_ns"] == 300.5
    assert result["baseline"]["summary"][f"s0|{CELL}"]["candidate_median_ns_per_request"] == 100.5
    assert result["real"]["summary"]["digits-mlp"]["candidate_to_baseline"] == pytest.approx(
        120.5 / 320.5
    )
    assert result["real"]["summary"]["digits-mlp"]["candidate_median_ns"] == 120.5
    assert result["real"]["summary"]["digits-mlp"]["candidate_model_load_median_ns"] == 200.5
    assert result["real"]["rounds"][0]["workloads"]["digits-mlp"]["sample_indices"] == [
        result["real"]["rounds"][0]["workloads"]["digits-mlp"]["baseline_sample_indices"]
    ]
    assert result["real"]["rounds"][0]["workloads"]["digits-mlp"]["sample_indices"] == [[1]]
    assert len(result["rounds"][0]["process_wall_ns"]["candidate"]) == 1
    assert len(result["baseline"]["rounds"][0]["process_wall_ns"]) == 1
    from config import MeasurementRequest
    from scoring import validate_result

    MeasurementRequest.parse(request)
    validate_result(request, result)
    with pytest.raises(ValueError, match="batch size one"):
        MeasurementRequest.parse(request | {"real_workloads": [real | {"samples_per_run": 2}]})
    result["measurement_policy"]["inference_clock"] = "child-reported"
    with pytest.raises(ValueError, match="timing/isolation policy mismatch"):
        validate_result(request, result)
    with pytest.raises(ValueError, match="coordinator-selected sample IDs"):
        MeasurementRequest.parse(request | {"real_sample_plan": None})
    repeated_sample_plan = request | {
        "timing": request["timing"] | {"rounds": 2},
        "real_sample_plan": {"digits-mlp": [[[1]], [[1]]]},
    }
    repeated_sample_plan["synthetic_input_plan"] = coordinator_input_plan(repeated_sample_plan)
    assert MeasurementRequest.parse(repeated_sample_plan).real_sample_plan == {
        "digits-mlp": [[[1]], [[1]]]
    }


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
    request["suites"] = ["synthetic"]
    request["synthetic_input_plan"] = coordinator_input_plan(request)
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
    assert result["preparation"]["mode"] == "cached"
    assert result["preparation"]["budget_seconds"] == 2
    assert result["stage_seconds"]["build"] == 0
    assert result["stage_seconds"]["build_stages"]["reference"]["status"] == "cached"
    scoring.validate_result(request, result)


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


def test_bundle_preparation_mode_checks_each_distinct_source(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(sandbox, "CONFIG", config)
    present = {"candidate", "baseline", "reference-a"}
    monkeypatch.setattr(
        benchmark,
        "cached_binary",
        lambda expected: Path("/cached") if expected in present else None,
    )
    request = {
        "candidate_hash": "candidate",
        "baseline_hash": "baseline",
        "comparisons": {
            "mlp": {"reference_hash": "reference-a"},
            "overall": {"reference_hash": "reference-a"},
            "resnet": {"reference_hash": "reference-b"},
        },
    }
    assert benchmark.preparation_mode(request) == "cold"
    present.add("reference-b")
    assert benchmark.preparation_mode(request) == "cached"


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
        output = "\n".join(
            f"{w['id']}|{cell} true 100 123" for w in request["workloads"] for cell in cells
        )
        return 0, emit_timing_markers(output, kwargs.get("on_output_line"))

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
