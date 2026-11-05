"""Build and measurement time are bounded independently; samples use fresh processes."""

import hashlib
import json
import shutil
import struct
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
    return dict(rounds=1, inner=1, run_seconds=10, build_seconds=10, benchmark_seconds=4) | extra


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
    assert caps == pytest.approx([4, 3])
    assert result["stage_seconds"] == pytest.approx(
        dict(build=9, baseline_process=0, rounds=2, benchmark=2)
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
        return (124, "") if seconds > timeout else (0, rows)

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
    monkeypatch.setattr(sandbox, "run", lambda command, timeout, **kw: (0, output))
    rows = benchmark.run_binary(
        Path("bench"), request, {}, 1, "reference", benchmark.Deadline("benchmark", 10)
    )
    expected_checksum = ((2166136261 ^ 123) * 16777619) % 2**32
    assert {key: value for key, value in rows.items() if not key.startswith("__")} == {
        f"{w['id']}|{CELL}": (100, expected_checksum) for w in workloads(42)
    }


def test_each_sample_uses_a_new_process_and_distinct_seed_file(monkeypatch, clock):
    draws = workloads(42)
    request = {"timing": timing(inner=3), "cells": [CELL], "seed": 42, "workloads": draws}
    keys = {f"{draw['id']}|{CELL}": 1 for draw in draws}
    inputs = benchmark.make_input_sequences(request, keys, 3)
    calls = []
    output = "\n".join(f"{draw['id']}|{CELL} true 100 123" for draw in draws)

    def run(command, timeout, **kwargs):
        calls.append((kwargs["env_extra"]["BENCH_INNER"], kwargs["input_file"].read_text()))
        return 0, output

    monkeypatch.setattr(sandbox, "run", run)
    rows = benchmark.run_binary(
        Path("bench"), request, keys, 3, "candidate", benchmark.Deadline("benchmark", 10), inputs
    )
    assert len(calls) == 3
    assert all(inner == "1" for inner, _ in calls)
    assert len({seeds for _, seeds in calls}) == 3
    assert rows["__raw_ns__"] == {f"{draw['id']}|{CELL}": [100, 100, 100] for draw in draws}
    for draw in draws:
        samples = [inputs[f"{draw['id']}|{CELL}"][i][0] for i in range(3)]
        assert len(set(samples)) == 3


def test_real_sample_selection_stays_distinct_across_rounds():
    spec = {"id": "har-mlp", "family": "mlp", "samples_per_run": 2, "sample_count": 12}
    request = {"workloads": [], "cells": [], "real_workloads": [spec]}
    used = set()
    first = benchmark.make_input_sequences(request, {}, 2, used_real_samples=used)
    second = benchmark.make_input_sequences(request, {}, 2, used_real_samples=used)
    selected = first["har-mlp|mlp"] + second["har-mlp|mlp"]
    flattened = [index for group in selected for index in group]
    assert len(flattened) == len(set(flattened)) == 8
    benchmark.make_input_sequences(request, {}, 2, used_real_samples=used)
    with pytest.raises(benchmark.MeasurementFailure, match="more distinct samples than remain"):
        benchmark.make_input_sequences(request, {}, 2, used_real_samples=used)


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
    assert result["__raw_ns__"][f"{draw['id']}|{CELL}"] == [100, 100, 100]


def test_a_timeout_keeps_what_the_measurement_had_reached(measurement, clock, monkeypatch):
    # Round two runs the candidate first; the reference then runs out of the shared deadline,
    # which the slower candidate used most of.
    request, _ = measurement
    request["timing"] = timing(rounds=2, benchmark_seconds=6.5)
    rows = "\n".join(f"{w['id']}|{CELL} true 100 123" for w in workloads(42))

    def run(command, timeout, **kwargs):
        seconds = 3 if Path(command[-1]).name == "candidate" else 1
        clock.advance(min(seconds, timeout))
        return (124, "") if seconds > timeout else (0, rows)

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
        return (1, "panic") if Path(command[-1]).name == "candidate" else (0, rows)

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
    asset = tmp_path / "real.bin"
    tensors = [32 * 784, 32, 16 * 32, 16, 10 * 16, 10, 2 * 784, 2]
    with asset.open("wb") as stream:
        for count in tensors:
            stream.write(struct.pack("<I", count))
            stream.write(bytes(count * 4))
    real = {
        "id": "digits-mlp",
        "family": "mlp",
        "model": "small-mlp",
        "dataset": "digits",
        "adapter": "image-zoo-v1",
        "asset_path": "real.bin",
        "asset_sha256": benchmark.file_hash(asset),
        "asset_revision": "fixture-v1",
        "sample_count": 2,
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
        "timing": timing(rounds=1, inner=1, baseline_rounds=1),
        "workloads": workloads(42),
        "suites": ("synthetic", "real"),
        "real_workloads": [real],
    }
    binaries = {}

    def build(source, expected, side, deadline):
        deadline.remaining(side)
        path = tmp_path / f"{side}.bin"
        path.write_bytes(source)
        binaries[side] = path
        return path

    def run(command, timeout, **kwargs):
        clock.advance(0.01)
        env = kwargs["env_extra"]
        synthetic = [item.split(":", 1)[0] for item in env["BENCH_WORKLOADS"].split(",")]
        rows = [f"{name}|{CELL} true 100 123" for name in synthetic]
        rows.append(f"digits-mlp|{CELL} true 120 123")
        rows.append("REAL_PREP|digits-mlp|200|50")
        if Path(command[-1]).stem == "baseline":
            rows = [
                row.replace(" true 100 ", " true 300 ").replace(" true 120 ", " true 320 ")
                for row in rows
            ]
        return 0, "\n".join(rows)

    monkeypatch.setattr(benchmark, "build", build)
    monkeypatch.setattr(sandbox, "run", run)
    result = benchmark.measure(request, b"candidate source", b"reference source", mode="cold")
    assert result["baseline"]["summary"][f"s0|{CELL}"]["candidate_to_baseline"] == 1 / 3
    assert result["real"]["summary"]["digits-mlp"]["candidate_to_baseline"] == 120 / 320
    assert result["real"]["rounds"][0]["workloads"]["digits-mlp"]["sample_indices"] == [
        result["real"]["rounds"][0]["workloads"]["digits-mlp"]["baseline_sample_indices"]
    ]
    assert len(result["rounds"][0]["process_wall_ns"]["candidate"]) == 1
    assert len(result["baseline"]["rounds"][0]["process_wall_ns"]) == 1
    from scoring import validate_result

    validate_result(request, result)


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
