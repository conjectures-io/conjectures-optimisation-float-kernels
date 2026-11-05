"""A supervised subprocess for sandboxed architecture-local builds and paired measurements."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import secrets
import socket
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import sandbox
from config import MeasurementRequest, digest, measurement_hash
from timing import preparation_seconds
from workload import RealWorkloadSpec, WorkloadSpec

import fingerprint

# The only translation of legacy Lean suite labels; persisted requests use public names.
LEAN_MODELS = {
    "resnet": "cnn",
    "mobilenet": "mnet",
    "normalized_mlp": "nmlp",
    "transformer": "tfm",
}


def lean_cell(cell):
    model, rest = cell.split(":", 1)
    return LEAN_MODELS.get(model, model) + ":" + rest


class MeasurementFailure(Exception):
    def __init__(self, kind, reason, timeout=None):
        self.kind = kind
        # What a timeout knew when it struck, and what the measurement had reached by then: the
        # worker keeps both with the failed job, so a timeout is evidence, not just a reason.
        self.timeout, self.partial = timeout, None
        super().__init__(reason)

    def error(self):
        value = {"kind": self.kind, "reason": str(self)}
        if self.timeout is not None:
            value["timeout"] = self.timeout
        if self.partial is not None:
            value["partial"] = self.partial
        return value


class Deadline:
    """One absolute wall-clock deadline shared by every command in a phase.

    Paired rounds give both sides the same deadline (`share`). When it runs out there, the side
    whose runs take longer is at fault, not whichever was running at the time: otherwise a slow
    candidate's overrun lands on the reference half the time, and a reference failure is retried
    as infrastructure, pausing the board, instead of deciding the evaluation. Sides are compared
    per run, not by total time: when the deadline strikes, one side may have finished one run more
    than the other, and its total would count that run against it.
    """

    def __init__(self, phase, seconds):
        self.phase, self.seconds = phase, seconds
        self.started = time.monotonic()
        self.end = self.started + seconds
        self.runs = None

    def share(self):
        # From here on each finished run is kept with its side, for `fail` to find who is at fault.
        self.runs = {}

    def finished(self, side, seconds):
        if self.runs is not None:
            self.runs.setdefault(side, []).append(seconds)

    def at_fault(self, side, stopped_after=None):
        """The side whose runs take longer, by its mean over the runs it finished. The run that was
        stopped only bounds its own side's from below. A tie, or no evidence, stays with `side`,
        the one that was running."""
        estimate = {s: sum(runs) / len(runs) for s, runs in self.runs.items()}
        if stopped_after is not None:
            estimate[side] = max(estimate.get(side, 0.0), stopped_after)
        return max(estimate, key=lambda s: (estimate[s], s == side), default=side)

    def fail(self, side, detail=None, *, shared=True, stopped_after=None, limit=None, **evidence):
        running = side
        if shared and self.runs is not None:
            side = self.at_fault(side, stopped_after)
        if side != running:
            # `detail` describes the run that was stopped; the reason names the side at fault.
            detail = (
                f"{side} runs take longer than the {running}'s, which was stopped when the shared "
                f"{self.seconds:g}s {self.phase} budget ran out"
            )
        detail = detail or f"exceeded {self.seconds:g}s {self.phase} budget"
        timeout = {
            "phase": self.phase,
            "limit": limit,
            "side": side,
            "running": running,
            "budget_seconds": self.seconds,
            "elapsed_seconds": round(self.elapsed(), 3),
        }
        if stopped_after is not None:
            timeout["stopped_after_seconds"] = round(stopped_after, 3)
        if self.runs is not None:
            # What blame was decided on: each side's finished runs, counted, and their mean.
            timeout["runs"] = {
                s: {"finished": len(runs), "mean_seconds": round(sum(runs) / len(runs), 3)}
                for s, runs in self.runs.items()
            }
        raise MeasurementFailure(side, f"{self.phase}-timeout: {detail}", timeout | evidence)

    def remaining(self, side):
        left = self.end - time.monotonic()
        if left <= 0:
            self.fail(side, limit="budget")
        return left

    def elapsed(self):
        return time.monotonic() - self.started


def machine_identity():
    """Stable execution identity, computed on the worker, not asserted by the coordinator."""
    if sandbox.backend() == "macos-seatbelt":
        from macos_worker import identity

        return digest(identity() | {"toolchain": (sandbox.LIB / "lean-toolchain").read_text()})
    fields = {}
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if ":" in line:
            key, value = (p.strip() for p in line.split(":", 1))
            if key in (
                "vendor_id",
                "model name",
                "cpu family",
                "model",
                "stepping",
                "Features",
                "flags",
                "CPU implementer",
                "CPU part",
                "CPU revision",
            ):
                fields.setdefault(key, value)
    return digest(
        {
            "architecture": platform.machine(),
            "cpu": fields,
            "affinity": sandbox._BENCH_CPU,
            "memory_mb": sandbox._BENCH_MEMORY_MB,
            "sandbox": sandbox._SANDBOX,
            "toolchain": (sandbox.LIB / "lean-toolchain").read_text(),
            "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
            "kernel": platform.release(),
            "bubblewrap": subprocess.check_output([sandbox.BWRAP, "--version"], text=True).strip(),
            "policy": "exact-local-pairs-v1",
        }
    )


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cache_location(expected):
    identity, benchmark_identity = machine_identity(), fingerprint.benchmark()
    cache = (
        sandbox.HERE / "pinned" / "exact-jobs" / digest([expected, benchmark_identity, identity])
    )
    return cache, dict(
        source_sha256=expected, benchmark_identity=benchmark_identity, identity=identity
    )


def cached_binary(expected):
    cache, evidence = cache_location(expected)
    binary = cache / "bench"
    try:
        manifest = json.loads((cache / "manifest.json").read_text())
        if any(manifest.get(k) != v for k, v in evidence.items()):
            return None
        if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
            return None
        size = binary.stat().st_size
        if (
            type(manifest.get("bytes")) is not int
            or not 0 < size == manifest["bytes"] <= 512 * 2**20
        ):
            return None
        if file_hash(binary) == manifest.get("binary_sha256"):
            return binary
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def preparation_mode(request):
    keys = ["reference_hash", "candidate_hash"]
    if request.get("baseline_hash") is not None:
        keys.append("baseline_hash")
    if sandbox.CONFIG.is_dir() and all(cached_binary(request[k]) is not None for k in keys):
        return "cached"
    return "cold"


def build(source, expected, side, deadline):
    deadline.remaining(side)
    if hashlib.sha256(source).hexdigest() != expected:
        raise MeasurementFailure("infrastructure", "immutable source hash mismatch")
    if binary := cached_binary(expected):
        deadline.remaining(side)
        return binary
    cache, evidence = cache_location(expected)
    binary = cache / "bench"
    cache.mkdir(parents=True, exist_ok=True)
    src = cache / "Kernel.lean"
    src.write_bytes(source)
    verdict = sandbox.Verdict(submission=expected)
    sandbox.wipe(sandbox.LAYER)
    (sandbox.LAYER / "upper").mkdir(parents=True)
    (sandbox.LAYER / "work").mkdir()
    sandbox._budget.reset(deadline.seconds, deadline=deadline.end)
    try:
        for stage in (sandbox.intake, sandbox.policy, sandbox.statement):
            if reason := stage(src, verdict):
                if sandbox._budget.timed_out:
                    deadline.fail(side, "build command exceeded its time allowance")
                deadline.remaining(side)
                raise MeasurementFailure(side, reason)
            deadline.remaining(side)
        code, output = sandbox.own(
            [sandbox.LAKE, "build", "benchExact"], sandbox._BUILD_TIMEOUT, build="write"
        )
        if code == 124:
            deadline.fail(side, "native build command exceeded its time allowance")
        deadline.remaining(side)
        if code or sandbox.foreign(sandbox.LAYER / "upper"):
            raise MeasurementFailure(side, f"native build failed: {sandbox.errors(output, code)}")
        built = sandbox.LAYER / "upper" / "bin" / "benchExact"
        if not built.exists():
            built = sandbox.BUILD / "bin" / "benchExact"
        tmp = binary.with_suffix(".tmp")
        shutil.copy2(built, tmp)
        deadline.remaining(side)
        if (
            fingerprint.benchmark() != evidence["benchmark_identity"]
            or machine_identity() != evidence["identity"]
        ):
            raise MeasurementFailure(
                "infrastructure", "build environment changed before caching binary"
            )
        tmp.replace(binary)
        manifest = evidence | {"bytes": binary.stat().st_size, "binary_sha256": file_hash(binary)}
        temporary = cache / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest))
        temporary.replace(cache / "manifest.json")
    finally:
        sandbox.wipe(sandbox.LAYER)
    deadline.remaining(side)
    return binary


def row_names(request):
    # Each row's name as the bench prints it -> its public name, in the order the bench runs them.
    rows = {}
    for draw in request["workloads"]:
        for cell in request["cells"]:
            spec = WorkloadSpec.synthetic_cell(draw, cell)
            rows[f"{draw['id']}|{lean_cell(cell)}"] = spec.id
    for workload in request.get("real_workloads", ()):
        family = workload["family"]
        cell = lean_cell(f"{family}:b1:normal")
        rows[f"{workload['id']}|{cell}"] = f"{workload['id']}|{family}"
    return rows


def start_marker(line):
    # The bench's `<row> start` marker, on stderr before each cell; not a result row.
    fields = line.split()
    return len(fields) == 2 and fields[1] == "start"


def finished(output, public):
    """What a killed run had done: the rows it printed in full, and the cell it was running.

    The bench flushes each row as it finishes and marks each cell's start, so a kill loses only
    the cell in progress; a cut-off last line is not a row.
    """
    rows, cell = {}, None
    for line in output.splitlines():
        fields = line.split()
        if start_marker(line) and fields[0] in public:
            cell = public[fields[0]]
        elif len(fields) == 4 and fields[1] == "true" and fields[0] in public:
            try:
                rows[public[fields[0]]] = [int(fields[2]), int(fields[3])]
            except ValueError:
                pass
    return {
        "cell": cell if cell not in rows else None,
        "cells_finished": len(rows),
        "cells_total": len(public),
        "finished": rows,
    }


def make_input_sequences(request, reps, groups, used_seeds=None, used_real_samples=None):
    used = used_seeds if used_seeds is not None else set()
    used_samples = used_real_samples if used_real_samples is not None else set()
    result = {}
    for draw in request["workloads"]:
        for cell in request["cells"]:
            key = f"{draw['id']}|{cell}"
            count = reps.get(key, 1)
            sequence_groups = []
            for _ in range(groups):
                values = []
                while len(values) < count:
                    value = secrets.randbits(32)
                    if value not in used:
                        used.add(value)
                        values.append(value)
                sequence_groups.append(values)
            result[key] = sequence_groups
    for workload in request.get("real_workloads", ()):
        key = f"{workload['id']}|{workload['family']}"
        if workload["samples_per_run"] * groups > workload["sample_count"]:
            raise MeasurementFailure(
                "infrastructure",
                f"real workload {workload['id']} needs more distinct samples per round",
            )
        available = [
            index
            for index in range(workload["sample_count"])
            if (workload["id"], index) not in used_samples
        ]
        count = workload["samples_per_run"] * groups
        if count > len(available):
            raise MeasurementFailure(
                "infrastructure",
                f"real workload {workload['id']} needs more distinct samples than remain",
            )
        selected = secrets.SystemRandom().sample(available, count)
        used_samples.update((workload["id"], index) for index in selected)
        result[key] = [
            selected[i * workload["samples_per_run"] : (i + 1) * workload["samples_per_run"]]
            for i in range(groups)
        ]
    return result


def run_binary(binary, request, reps, inner, side, deadline, input_sequences=None):
    if input_sequences is None:
        input_sequences = make_input_sequences(request, reps, inner)
    extra = {
        "BENCH_CELLS": ",".join(lean_cell(c) for c in request["cells"]),
        "BENCH_REPS": ",".join(f"{lean_cell(c)}={n}" for c, n in reps.items()),
        "BENCH_SEED": str(request["seed"]),
        "BENCH_INNER": "1",
        "LEAN_ABORT_ON_PANIC": "1",
    }
    extra["BENCH_WORKLOADS"] = ",".join(
        f"{w['id']}:{w['seed']}:{w['profile']}" for w in request["workloads"]
    )
    extra["BENCH_REPS"] = ",".join(
        f"{k.split('|')[0]}|{lean_cell(k.split('|')[1])}={n}" for k, n in reps.items()
    )
    real_assets = {}
    real_entries = []
    for item in request.get("real_workloads", ()):
        asset_id = item["id"]
        path = (sandbox.HERE / item["asset_path"]).resolve()
        real_assets[asset_id] = path
        mounted = f"/benchmark-assets/{asset_id}.bin" if sandbox._SANDBOX else str(path)
        real_entries.append(
            "|".join(
                (asset_id, item["adapter"], item["family"], mounted, str(item["samples_per_run"]))
            )
        )
    extra["BENCH_REAL_WORKLOADS"] = ",".join(real_entries)
    command = [str(binary)]
    if sandbox._BENCH_CPU:
        command = ["taskset", "-c", sandbox._BENCH_CPU, *command]
    public = row_names(request)
    groups = []
    process_wall_ns = []
    with tempfile.TemporaryDirectory(prefix="bench-inputs-") as directory:
        seed_file = Path(directory) / "input-seeds.txt"
        for repetition in range(inner):
            seed_rows = []
            for printed_key, public_key in public.items():
                groups_for_key = input_sequences.get(public_key)
                real_item = next(
                    (
                        item
                        for item in request.get("real_workloads", ())
                        if public_key == f"{item['id']}|{item['family']}"
                    ),
                    None,
                )
                count = real_item["samples_per_run"] if real_item else reps.get(public_key, 1)
                if (
                    not isinstance(groups_for_key, list)
                    or len(groups_for_key) != inner
                    or len(groups_for_key[repetition]) < count
                ):
                    raise MeasurementFailure("infrastructure", "input seed plan is incomplete")
                seed_rows.append(
                    printed_key + "=" + ";".join(map(str, groups_for_key[repetition][:count]))
                )
            seed_file.write_text("\n".join(seed_rows) + "\n")
            budget = deadline.remaining(side)
            timeout = min(request["timing"]["run_seconds"], budget)
            started = time.monotonic()
            code, output = sandbox.run(
                command,
                timeout,
                env_extra=extra,
                memory_mb=sandbox._BENCH_MEMORY_MB,
                input_file=seed_file,
                asset_files=real_assets,
            )
            seconds = time.monotonic() - started
            if code == 124:
                # A fresh child per timed repetition keeps candidate-global state out of the next run.
                # A shared-deadline timeout is attributed using all completed side runs so far.
                shared = budget < request["timing"]["run_seconds"]
                deadline.fail(
                    side,
                    f"{side} invocation exceeded its {timeout:g}s time allowance",
                    shared=shared,
                    stopped_after=seconds,
                    limit="budget" if shared else "invocation",
                    allowance_seconds=round(timeout, 3),
                    **finished(output, public),
                )
            deadline.finished(side, seconds)
            deadline.remaining(side)
            if code:
                raise MeasurementFailure(side, f"bench exit {code}: {output[-1000:]}")
            rows = {}
            real_preparation = {}
            for line in output.splitlines():
                if start_marker(line):
                    continue
                prep_fields = line.split("|")
                if len(prep_fields) == 4 and prep_fields[0] == "REAL_PREP":
                    try:
                        real_preparation[prep_fields[1]] = (
                            int(prep_fields[2]),
                            int(prep_fields[3]),
                        )
                    except ValueError as exc:
                        raise MeasurementFailure(
                            "infrastructure", "invalid real prep timing"
                        ) from exc
                    continue
                fields = line.split()
                if len(fields) != 4 or fields[1] != "true" or fields[0] not in public:
                    raise MeasurementFailure("infrastructure", "invalid benchmark row")
                key = public[fields[0]]
                if key in rows:
                    raise MeasurementFailure("infrastructure", "duplicate benchmark row")
                try:
                    ns, checksum = int(fields[2]), int(fields[3])
                except ValueError as exc:
                    raise MeasurementFailure(
                        "infrastructure", "noninteger benchmark output"
                    ) from exc
                if ns <= 0 or not 0 <= checksum < 2**32:
                    raise MeasurementFailure("infrastructure", "invalid duration or checksum")
                rows[key] = (ns, checksum)
            if set(rows) != set(public.values()):
                raise MeasurementFailure("infrastructure", "missing or extra benchmark cells")
            if set(real_preparation) != {item["id"] for item in request.get("real_workloads", ())}:
                raise MeasurementFailure(
                    "infrastructure", "missing real workload preparation timing"
                )
            groups.append((rows, real_preparation))
            process_wall_ns.append(round(seconds * 1e9))

    result = {}
    for cell in groups[0][0]:
        combined_checksum = 2166136261
        for group, _ in groups:
            combined_checksum = ((combined_checksum ^ group[cell][1]) * 16777619) % 2**32
        result[cell] = (int(statistics.median(g[cell][0] for g, _ in groups)), combined_checksum)
    result["__raw_ns__"] = {cell: [rows[cell][0] for rows, _ in groups] for cell in groups[0][0]}
    result["__process_wall_ns__"] = process_wall_ns
    if request.get("real_workloads"):
        result["__real_preparation_ns__"] = {
            item["id"]: {
                "model_load_ns": int(statistics.median(prep[item["id"]][0] for _, prep in groups)),
                "input_preparation_ns": int(
                    statistics.median(prep[item["id"]][1] for _, prep in groups)
                ),
            }
            for item in request["real_workloads"]
        }
    return result


def measure(request, candidate, reference, mode=None):
    MeasurementRequest.parse(request)
    if request["identity"] != machine_identity():
        raise MeasurementFailure("infrastructure", "hardware/execution identity mismatch")
    if request["benchmark_identity"] != fingerprint.benchmark():
        raise MeasurementFailure(
            "infrastructure", "checkout does not match pinned verifier/benchmark"
        )
    sources = [(candidate, "candidate_hash"), (reference, "reference_hash")]
    baseline = None
    if request.get("baseline_hash") is not None:
        baseline_path = sandbox.HERE / "kernels/exact/baseline/Kernel.lean"
        try:
            baseline = baseline_path.read_bytes()
        except OSError as exc:
            raise MeasurementFailure("infrastructure", f"shipped baseline unavailable: {exc}")
        sources.append((baseline, "baseline_hash"))
    for content, key in sources:
        if hashlib.sha256(content).hexdigest() != request[key]:
            raise MeasurementFailure("infrastructure", "immutable source hash mismatch")
    for item in request.get("real_workloads", ()):
        source_path = sandbox.HERE / item["asset_path"]
        path = source_path.resolve()
        try:
            path.relative_to(sandbox.HERE.resolve())
        except ValueError as exc:
            raise MeasurementFailure(
                "infrastructure", "real workload path escapes checkout"
            ) from exc
        if (
            not path.is_file()
            or source_path.is_symlink()
            or path.stat().st_size > 512 * 2**20
            or file_hash(path) != item["asset_sha256"]
        ):
            raise MeasurementFailure(
                "infrastructure", f"real workload asset missing or hash mismatch: {item['id']}"
            )
        try:
            RealWorkloadSpec.parse(item).validate_asset(path)
        except (OSError, ValueError) as exc:
            raise MeasurementFailure(
                "infrastructure", f"invalid real workload asset {item['id']}: {exc}"
            ) from exc
    sandbox.check_limits()
    # sandbox.run uses shared Lake/overlay scratch paths; hold this checkout lock throughout.
    with (
        open(sandbox.HERE / "gate.lock", "a") as build_lock,
        open(sandbox.HERE / "sandbox.lock", "a") as lock,
    ):
        if os.environ.get("GATE_LOCK_HELD") != "1":
            fcntl.flock(build_lock, fcntl.LOCK_EX)
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = sandbox.WORK.read_bytes() if sandbox.WORK.exists() else None
        try:
            mode = mode or preparation_mode(request)
            build_clock = Deadline("build", preparation_seconds(request["timing"], mode))
            sandbox._budget.reset(build_clock.seconds, deadline=build_clock.end)
            if mode == "cached":
                ref = cached_binary(request["reference_hash"])
                sub = cached_binary(request["candidate_hash"])
                base = cached_binary(request["baseline_hash"]) if baseline is not None else None
                if (
                    ref is None
                    or sub is None
                    or (baseline is not None and base is None)
                    or not sandbox.CONFIG.is_dir()
                ):
                    raise MeasurementFailure(
                        "infrastructure",
                        "cached preparation unavailable; retry with cold preparation",
                    )
            else:
                try:
                    sandbox.prepare_shared(native=True, deadline=build_clock.end)
                    build_clock.remaining("infrastructure")
                except TimeoutError:
                    build_clock.fail(
                        "infrastructure", "shared build preparation exceeded its time allowance"
                    )
                except MeasurementFailure:
                    raise
                except Exception as exc:
                    raise MeasurementFailure(
                        "infrastructure", f"shared build preparation: {exc}"
                    ) from exc
                base = (
                    build(baseline, request["baseline_hash"], "baseline", build_clock)
                    if baseline is not None
                    else None
                )
                ref = build(reference, request["reference_hash"], "reference", build_clock)
                sub = build(candidate, request["candidate_hash"], "candidate", build_clock)
            build_clock.remaining("infrastructure" if mode == "cached" else "candidate")
            build_seconds = build_clock.elapsed()
            benchmark_clock = Deadline("benchmark", request["timing"]["benchmark_seconds"])
            # What the measurement has reached, kept with a failure that stops it partway.
            rounds = []
            real_rounds = []
            partial = {
                "build_seconds": build_seconds,
                "rounds": rounds,
                "real_rounds": real_rounds,
                "rounds_planned": request["timing"]["rounds"],
            }
            try:
                # One inference per fresh subprocess is the cache boundary. Repeating a cell
                # inside MainExact would let submitted global state survive between its timed
                # calls, even when each call had a distinct input.
                reps = {
                    f"{draw['id']}|{cell}": 1
                    for draw in request["workloads"]
                    for cell in request["cells"]
                }
                partial["measurement_policy"] = {
                    "samples_per_process": 1,
                    "fresh_process_each_sample": True,
                }
                partial["repetitions"] = reps
                baseline_reps = {}
                baseline_rounds = []
                if base is not None:
                    baseline_reps = dict.fromkeys(reps, 1)
                    baseline_rounds_count = request["timing"].get("baseline_rounds", 3)
                    partial["baseline_repetitions"] = baseline_reps
                # Each paired measurement has one inference per fresh process. The `inner`
                # setting controls independent processes per paired round, not calls within one.
                benchmark_clock.share()
                t0 = time.monotonic()
                used_input_seeds = set()
                used_real_samples = set()
                for i in range(request["timing"]["rounds"]):
                    baseline_this_round = base is not None and i < baseline_rounds_count
                    side_binaries = {"reference": ref, "candidate": sub}
                    pair_order = (
                        ["reference", "candidate"] if i % 2 == 0 else ["candidate", "reference"]
                    )
                    if baseline_this_round:
                        side_binaries["baseline"] = base
                        baseline_position = i % 3
                        side_order = pair_order.copy()
                        side_order.insert(baseline_position, "baseline")
                    else:
                        side_order = pair_order
                    run_reps = reps
                    paired_inputs = make_input_sequences(
                        request,
                        run_reps,
                        request["timing"]["inner"],
                        used_seeds=used_input_seeds,
                        used_real_samples=used_real_samples,
                    )
                    baseline_inputs = {
                        key: [sequences[i % len(sequences)]]
                        for key, sequences in paired_inputs.items()
                    }
                    paired_plan = {
                        "synthetic": {
                            key: [group[0] for group in groups]
                            for key, groups in paired_inputs.items()
                            if key in reps
                        },
                        "real": {
                            item["id"]: paired_inputs[f"{item['id']}|{item['family']}"]
                            for item in request.get("real_workloads", ())
                        },
                    }
                    # A side that finished before the other was stopped is kept with the failure.
                    observed = partial["round_in_progress"] = {"input_plan": paired_plan}
                    for side in side_order:
                        repetitions = baseline_reps if side == "baseline" else reps
                        observed[side] = run_binary(
                            side_binaries[side],
                            request,
                            repetitions,
                            1 if side == "baseline" else request["timing"]["inner"],
                            side,
                            benchmark_clock,
                            baseline_inputs if side == "baseline" else paired_inputs,
                        )
                    cells = {}
                    for c in reps:
                        a, b = observed["reference"][c], observed["candidate"][c]
                        if a[1] != b[1]:
                            raise MeasurementFailure("checksum", f"output mismatch on {c}")
                        if baseline_this_round and observed["baseline"][c][1] != a[1]:
                            raise MeasurementFailure("checksum", f"baseline output mismatch on {c}")
                        cells[c] = {
                            "reference_ns": a[0],
                            "candidate_ns": b[0],
                            "reference_checksum": a[1],
                            "candidate_checksum": b[1],
                            "reference_inner_ns": observed["reference"]["__raw_ns__"][c],
                            "candidate_inner_ns": observed["candidate"]["__raw_ns__"][c],
                        }
                    rounds.append({"reference_first": i % 2 == 0, "cells": cells})
                    rounds[-1]["input_plan"] = paired_plan
                    rounds[-1]["process_wall_ns"] = {
                        side: observed[side]["__process_wall_ns__"]
                        for side in ("reference", "candidate")
                    }
                    if request.get("real_workloads"):
                        real_cells = {}
                        for item in request["real_workloads"]:
                            key = f"{item['id']}|{item['family']}"
                            ref_row = observed["reference"][key]
                            candidate_row = observed["candidate"][key]
                            if ref_row[1] != candidate_row[1]:
                                raise MeasurementFailure(
                                    "checksum", f"real workload output mismatch on {item['id']}"
                                )
                            if baseline_this_round and observed["baseline"][key][1] != ref_row[1]:
                                raise MeasurementFailure(
                                    "checksum",
                                    f"real baseline output mismatch on {item['id']}",
                                )
                            row = {
                                "reference_ns": ref_row[0],
                                "candidate_ns": candidate_row[0],
                                "reference_checksum": ref_row[1],
                                "candidate_checksum": candidate_row[1],
                                "reference_inner_ns": observed["reference"]["__raw_ns__"][key],
                                "candidate_inner_ns": observed["candidate"]["__raw_ns__"][key],
                                "reference_model_load_ns": observed["reference"][
                                    "__real_preparation_ns__"
                                ][item["id"]]["model_load_ns"],
                                "candidate_model_load_ns": observed["candidate"][
                                    "__real_preparation_ns__"
                                ][item["id"]]["model_load_ns"],
                                "reference_input_preparation_ns": observed["reference"][
                                    "__real_preparation_ns__"
                                ][item["id"]]["input_preparation_ns"],
                                "candidate_input_preparation_ns": observed["candidate"][
                                    "__real_preparation_ns__"
                                ][item["id"]]["input_preparation_ns"],
                                "samples": item["samples_per_run"],
                                "sample_indices": paired_inputs[key],
                                "baseline_sample_indices": (
                                    paired_inputs[key][0] if baseline_this_round else None
                                ),
                            }
                            if baseline_this_round:
                                base_row = observed["baseline"][key]
                                row.update(
                                    baseline_ns=base_row[0],
                                    baseline_checksum=base_row[1],
                                    baseline_inner_ns=observed["baseline"]["__raw_ns__"][key],
                                    baseline_model_load_ns=observed["baseline"][
                                        "__real_preparation_ns__"
                                    ][item["id"]]["model_load_ns"],
                                    baseline_input_preparation_ns=observed["baseline"][
                                        "__real_preparation_ns__"
                                    ][item["id"]]["input_preparation_ns"],
                                )
                            real_cells[item["id"]] = row
                        real_rounds.append({"paired_round": i, "workloads": real_cells})
                    if baseline_this_round:
                        baseline_rounds.append(
                            {
                                "paired_round": i,
                                "input_plan": {
                                    "synthetic": {
                                        key: [groups[0][0]]
                                        for key, groups in paired_inputs.items()
                                        if key in reps
                                    },
                                    "real": {
                                        item["id"]: [
                                            paired_inputs[f"{item['id']}|{item['family']}"][0]
                                        ]
                                        for item in request.get("real_workloads", ())
                                    },
                                },
                                "cells": {
                                    c: {
                                        "baseline_ns": ns,
                                        "baseline_checksum": checksum,
                                        "requests": baseline_reps[c],
                                        "baseline_inner_ns": observed["baseline"]["__raw_ns__"][c],
                                        "baseline_ns_per_request": round(ns / baseline_reps[c], 3),
                                    }
                                    for c in reps
                                    for ns, checksum in [observed["baseline"][c]]
                                },
                                "process_wall_ns": observed["baseline"]["__process_wall_ns__"],
                            }
                        )
                    del partial["round_in_progress"]
                round_seconds = time.monotonic() - t0
                benchmark_clock.remaining("candidate")
            except MeasurementFailure as exc:
                exc.partial = partial | {"benchmark_seconds": round(benchmark_clock.elapsed(), 3)}
                raise
            if fingerprint.benchmark() != request["benchmark_identity"]:
                raise MeasurementFailure("infrastructure", "checkout changed during benchmark")
            real_summaries = {}
            for item in request.get("real_workloads", ()):
                observations = [row["workloads"][item["id"]] for row in real_rounds]
                candidate_median = int(
                    statistics.median(row["candidate_ns"] for row in observations)
                )
                reference_median = int(
                    statistics.median(row["reference_ns"] for row in observations)
                )
                baseline_values = [
                    row["baseline_ns"] for row in observations if "baseline_ns" in row
                ]
                baseline_median = (
                    int(statistics.median(baseline_values)) if baseline_values else None
                )
                real_summaries[item["id"]] = {
                    "family": item["family"],
                    "model": item["model"],
                    "dataset": item["dataset"],
                    "samples_per_run": item["samples_per_run"],
                    "candidate_median_ns": candidate_median,
                    "reference_median_ns": reference_median,
                    "candidate_to_reference": candidate_median / reference_median,
                    "baseline_median_ns": baseline_median,
                    "candidate_to_baseline": (
                        candidate_median / baseline_median if baseline_median else None
                    ),
                    "candidate_model_load_median_ns": int(
                        statistics.median(row["candidate_model_load_ns"] for row in observations)
                    ),
                    "reference_model_load_median_ns": int(
                        statistics.median(row["reference_model_load_ns"] for row in observations)
                    ),
                    "candidate_input_preparation_median_ns": int(
                        statistics.median(
                            row["candidate_input_preparation_ns"] for row in observations
                        )
                    ),
                    "reference_input_preparation_median_ns": int(
                        statistics.median(
                            row["reference_input_preparation_ns"] for row in observations
                        )
                    ),
                    "baseline_model_load_median_ns": int(
                        statistics.median(
                            row["baseline_model_load_ns"]
                            for row in observations
                            if "baseline_model_load_ns" in row
                        )
                    )
                    if baseline_median
                    else None,
                    "baseline_input_preparation_median_ns": int(
                        statistics.median(
                            row["baseline_input_preparation_ns"]
                            for row in observations
                            if "baseline_input_preparation_ns" in row
                        )
                    )
                    if baseline_median
                    else None,
                }
            real_families = {}
            for family in sorted({item["family"] for item in request.get("real_workloads", ())}):
                family_rows = {
                    item["id"]: real_summaries[item["id"]]
                    for item in request["real_workloads"]
                    if item["family"] == family
                }
                reference_ratios = [row["candidate_to_reference"] for row in family_rows.values()]
                baseline_ratios = [
                    row["candidate_to_baseline"]
                    for row in family_rows.values()
                    if row["candidate_to_baseline"] is not None
                ]
                real_families[family] = {
                    "workloads": family_rows,
                    "equal_weight_candidate_to_reference_geomean": math.exp(
                        sum(math.log(value) for value in reference_ratios) / len(reference_ratios)
                    ),
                    "equal_weight_candidate_to_baseline_geomean": (
                        math.exp(
                            sum(math.log(value) for value in baseline_ratios) / len(baseline_ratios)
                        )
                        if baseline_ratios
                        else None
                    ),
                }
            baseline_summary = {}
            if base is not None:
                for cell in reps:
                    values = [row["cells"][cell]["baseline_ns"] for row in baseline_rounds]
                    baseline_median = int(statistics.median(values))
                    candidate_median = int(
                        statistics.median(row["cells"][cell]["candidate_ns"] for row in rounds)
                    )
                    reference_median = int(
                        statistics.median(row["cells"][cell]["reference_ns"] for row in rounds)
                    )
                    baseline_per_request = baseline_median / baseline_reps[cell]
                    candidate_per_request = candidate_median / reps[cell]
                    reference_per_request = reference_median / reps[cell]
                    baseline_summary[cell] = {
                        "baseline_median_ns": baseline_median,
                        "baseline_repetitions": baseline_reps[cell],
                        "baseline_ns_per_request": baseline_per_request,
                        "candidate_median_ns_per_request": candidate_per_request,
                        "reference_median_ns_per_request": reference_per_request,
                        "candidate_to_baseline": candidate_per_request / baseline_per_request,
                        "reference_to_baseline": reference_per_request / baseline_per_request,
                        "interpretation": "unpaired diagnostic; admission remains candidate/reference",
                    }
            return {
                "request_hash": measurement_hash(request),
                "workloads": request["workloads"],
                "provenance": {
                    "identity": machine_identity(),
                    "machine": socket.gethostname(),
                    "architecture": platform.machine(),
                    "cpu_affinity": sandbox._BENCH_CPU,
                    "load": os.getloadavg(),
                    "candidate_binary": file_hash(sub),
                    "reference_binary": file_hash(ref),
                },
                "measurement_policy": partial["measurement_policy"],
                "repetitions": reps,
                "rounds": rounds,
                "baseline": {
                    "source_hash": request["baseline_hash"],
                    "rounds": baseline_rounds,
                    "repetitions": baseline_reps,
                    "summary": baseline_summary,
                }
                if baseline is not None
                else None,
                "real": {
                    "workloads": request.get("real_workloads", []),
                    "rounds": real_rounds,
                    "summary": real_summaries,
                    "families": real_families,
                },
                "timing_seconds": round_seconds,
                "stage_seconds": {
                    "build": build_seconds,
                    "baseline_process": sum(sum(row["process_wall_ns"]) for row in baseline_rounds)
                    / 1e9,
                    "rounds": round_seconds,
                    "benchmark": benchmark_clock.elapsed(),
                },
                "preparation": {"mode": mode, "budget_seconds": build_clock.seconds},
            }
        finally:
            if original is not None:
                sandbox.WORK.write_bytes(original)
            else:
                sandbox.WORK.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--preparation-mode", choices=("cold", "cached"))
    args = parser.parse_args()
    if args.identity:
        print(
            json.dumps(
                {
                    "identity": machine_identity(),
                    "proof_identity": fingerprint.proof(),
                    "benchmark_identity": fingerprint.benchmark(),
                }
            )
        )
        return
    try:
        value = {
            "result": measure(
                json.loads(args.request.read_text()),
                args.candidate.read_bytes(),
                args.reference.read_bytes(),
                mode=args.preparation_mode,
            )
        }
    except MeasurementFailure as exc:
        value = {"error": exc.error()}
    except Exception as exc:
        value = {"error": {"kind": "infrastructure", "reason": str(exc)}}
    args.result.write_text(json.dumps(value))


if __name__ == "__main__":
    main()
