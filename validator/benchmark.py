"""A supervised subprocess for sandboxed architecture-local builds and paired measurements."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import socket
import statistics
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import sandbox
from comparisons import board_request
from config import MeasurementRequest, digest, measurement_hash
from timing import baseline_round_indices, build_stage_seconds, preparation_seconds
from workload import RealWorkloadSpec, WorkloadSpec, baseline_sentinel_rows

import fingerprint

_CACHE_UNCHECKED = object()

# The only translation of legacy Lean suite labels; persisted requests use public names.
LEAN_MODELS = {
    "resnet": "cnn",
    "mobilenet": "mnet",
    "normalized_mlp": "nmlp",
    "transformer": "tfm",
    "recurrent": "rnn",
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
    comparisons = request.get("comparisons")
    if isinstance(comparisons, dict):
        sources = [request["candidate_hash"]] + [
            comparison["reference_hash"] for comparison in comparisons.values()
        ]
    else:
        sources = [request["reference_hash"], request["candidate_hash"]]
    if request.get("baseline_hash") is not None:
        sources.append(request["baseline_hash"])
    if sandbox.CONFIG.is_dir() and all(
        cached_binary(source) is not None for source in set(sources)
    ):
        return "cached"
    return "cold"


def build(source, expected, side, deadline, *, cached=_CACHE_UNCHECKED):
    deadline.remaining(side)
    if hashlib.sha256(source).hexdigest() != expected:
        raise MeasurementFailure("infrastructure", "immutable source hash mismatch")
    binary = cached_binary(expected) if cached is _CACHE_UNCHECKED else cached
    if binary:
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


def make_input_sequences(request, reps, groups, used_seeds=None, real_round_index=None):
    used = used_seeds if used_seeds is not None else set()
    result = {}
    for draw in request["workloads"]:
        for cell in request["cells"]:
            key = f"{draw['id']}|{cell}"
            count = reps.get(key, 1)
            selected = request.get("synthetic_input_plan")
            if selected is not None:
                if real_round_index is None:
                    raise MeasurementFailure(
                        "infrastructure", "synthetic input round is unspecified"
                    )
                try:
                    sequence_groups = selected[key][real_round_index]
                except (KeyError, IndexError, TypeError) as exc:
                    raise MeasurementFailure(
                        "infrastructure", "coordinator synthetic input plan is incomplete"
                    ) from exc
                if len(sequence_groups) != groups or any(
                    len(values) < count for values in sequence_groups
                ):
                    raise MeasurementFailure(
                        "infrastructure", "coordinator synthetic input plan has invalid pass sizes"
                    )
                result[key] = [list(values[:count]) for values in sequence_groups]
            else:
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
        selected = request.get("real_sample_plan")
        if selected is not None:
            if real_round_index is None:
                raise MeasurementFailure("infrastructure", "real sample round is unspecified")
            try:
                result[key] = selected[workload["id"]][real_round_index]
            except (KeyError, IndexError, TypeError) as exc:
                raise MeasurementFailure(
                    "infrastructure", "coordinator real sample plan is incomplete"
                ) from exc
            continue
        count = workload["samples_per_run"] * groups
        if workload["samples_per_run"] != 1:
            raise MeasurementFailure("infrastructure", "real inference must use batch size one")
        selected = [secrets.randbelow(workload["sample_count"]) for _ in range(count)]
        result[key] = [
            selected[i * workload["samples_per_run"] : (i + 1) * workload["samples_per_run"]]
            for i in range(groups)
        ]
    return result


def make_warmup_sequences(request, reps, groups, real_round_index):
    """Read the frozen unscored warm-up schedule through the ordinary input-plan validator."""
    warmup_request = request | {
        "synthetic_input_plan": request.get("synthetic_warmup_plan"),
        "real_sample_plan": request.get("real_warmup_plan"),
    }
    return make_input_sequences(warmup_request, reps, groups, real_round_index=real_round_index)


def input_plan_record(sequences, request, reps):
    if sequences is None:
        return None
    return {
        "synthetic": {
            key: [group[0] for group in values] for key, values in sequences.items() if key in reps
        },
        "real": {
            item["id"]: sequences[f"{item['id']}|{item['family']}"]
            for item in request.get("real_workloads", ())
        },
    }


def run_binary(
    binary, request, reps, inner, side, deadline, input_sequences=None, *, warmup_sequences=None
):
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
    for item in request.get("real_workloads", ()):
        asset_id = item["id"]
        real_assets[f"{asset_id}-weights"] = (sandbox.HERE / item["weights_path"]).resolve()
    command = [str(binary)]
    if sandbox._BENCH_CPU:
        command = ["taskset", "-c", sandbox._BENCH_CPU, *command]
    public = row_names(request)
    groups = []
    warmup_groups = []
    process_wall_ns = []
    peak_memory_bytes = []
    with tempfile.TemporaryDirectory(prefix="bench-inputs-") as directory:
        temporary_inputs = Path(directory)
        seed_file = Path(directory) / "input-seeds.txt"
        warmup_seed_file = Path(directory) / "warmup-input-seeds.txt"
        warmup_required = warmup_sequences is not None
        for repetition in range(inner):
            seed_rows = []
            warmup_seed_rows = []
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
                if warmup_required:
                    warmup_input_groups = warmup_sequences.get(public_key)
                    if (
                        not isinstance(warmup_input_groups, list)
                        or len(warmup_input_groups) != inner
                        or len(warmup_input_groups[repetition]) < count
                    ):
                        raise MeasurementFailure(
                            "infrastructure", "warm-up input plan is incomplete"
                        )
                    warmup_seed_rows.append(
                        printed_key
                        + "="
                        + ";".join(map(str, warmup_input_groups[repetition][:count]))
                    )
            seed_file.write_text("\n".join(seed_rows) + "\n")
            warmup_seed_file.write_text("\n".join(warmup_seed_rows) + "\n")
            real_entries = []
            invocation_assets = dict(real_assets)
            for item in request.get("real_workloads", ()):
                asset_id = item["id"]
                groups_for_key = input_sequences[f"{asset_id}|{item['family']}"]
                chosen = groups_for_key[repetition][: item["samples_per_run"]]
                source = (sandbox.HERE / item["samples_path"]).resolve()
                staged = temporary_inputs / f"{asset_id}-samples-{repetition}.bin"
                with source.open("rb") as stream:
                    payload = bytearray()
                    for index in chosen:
                        stream.seek(index * item["sample_words"] * 4)
                        record = stream.read(item["sample_words"] * 4)
                        if len(record) != item["sample_words"] * 4:
                            raise MeasurementFailure(
                                "infrastructure", f"truncated sample record in {asset_id}"
                            )
                        payload.extend(record)
                staged.write_bytes(struct.pack("<I", len(payload) // 4) + payload)
                warmup_path = ""
                if warmup_required:
                    warmup_chosen = warmup_sequences[f"{asset_id}|{item['family']}"][repetition][
                        : item["samples_per_run"]
                    ]
                    warmup_staged = temporary_inputs / f"{asset_id}-warmup-samples-{repetition}.bin"
                    with source.open("rb") as stream:
                        warmup_payload = bytearray()
                        for index in warmup_chosen:
                            stream.seek(index * item["sample_words"] * 4)
                            record = stream.read(item["sample_words"] * 4)
                            if len(record) != item["sample_words"] * 4:
                                raise MeasurementFailure(
                                    "infrastructure",
                                    f"truncated warm-up sample record in {asset_id}",
                                )
                            warmup_payload.extend(record)
                    warmup_staged.write_bytes(
                        struct.pack("<I", len(warmup_payload) // 4) + warmup_payload
                    )
                sample_asset_id = f"{asset_id}-selected-input"
                invocation_assets[sample_asset_id] = staged
                if warmup_required:
                    warmup_asset_id = f"{asset_id}-warmup-input"
                    invocation_assets[warmup_asset_id] = warmup_staged
                weight_path = (
                    f"/benchmark-assets/{asset_id}-weights.bin"
                    if sandbox._SANDBOX
                    else str(real_assets[f"{asset_id}-weights"])
                )
                sample_path = (
                    f"/benchmark-assets/{sample_asset_id}.bin" if sandbox._SANDBOX else str(staged)
                )
                if warmup_required:
                    warmup_path = (
                        f"/benchmark-assets/{warmup_asset_id}.bin"
                        if sandbox._SANDBOX
                        else str(warmup_staged)
                    )
                real_entries.append(
                    "|".join(
                        (
                            asset_id,
                            item["adapter"],
                            item["family"],
                            weight_path,
                            sample_path,
                            warmup_path,
                            str(item["samples_per_run"]),
                            str(item["sample_count"]),
                            str(item["sample_words"]),
                        )
                    )
                )
            invocation_env = dict(extra)
            invocation_env["BENCH_REAL_WORKLOADS"] = ",".join(real_entries)
            if warmup_required:
                invocation_env["BENCH_WARMUP_INPUT_SEED_FILE"] = str(warmup_seed_file.resolve())
            budget = deadline.remaining(side)
            timeout = min(request["timing"]["run_seconds"], budget)
            started = time.monotonic()

            # Lean only marks inference boundaries. The reported duration used for admission is
            # measured here by the trusted parent when those flushed markers cross the pipe.
            inference_events = {}
            warmup_events = {}

            def observe_output(line, received_ns):
                if line.startswith("BENCH_READY|"):
                    printed_key = line.removeprefix("BENCH_READY|")
                    if printed_key not in public or printed_key in inference_events:
                        raise MeasurementFailure(
                            "infrastructure", "invalid/duplicate inference ready marker"
                        )
                    inference_events[printed_key] = {"started_ns": received_ns}
                elif line.startswith("BENCH_DONE|"):
                    printed_key = line.removeprefix("BENCH_DONE|")
                    event = inference_events.get(printed_key)
                    if event is None or "finished_ns" in event:
                        raise MeasurementFailure(
                            "infrastructure", "invalid/duplicate inference done marker"
                        )
                    event["finished_ns"] = received_ns
                elif line.startswith("BENCH_WARMUP_READY|"):
                    printed_key = line.removeprefix("BENCH_WARMUP_READY|")
                    if printed_key not in public or printed_key in warmup_events:
                        raise MeasurementFailure(
                            "infrastructure", "invalid/duplicate warm-up ready marker"
                        )
                    warmup_events[printed_key] = {"started_ns": received_ns}
                elif line.startswith("BENCH_WARMUP_DONE|"):
                    printed_key = line.removeprefix("BENCH_WARMUP_DONE|")
                    event = warmup_events.get(printed_key)
                    if event is None or "finished_ns" in event:
                        raise MeasurementFailure(
                            "infrastructure", "invalid/duplicate warm-up done marker"
                        )
                    event["finished_ns"] = received_ns

            resource_usage = {}
            code, output = sandbox.run(
                command,
                timeout,
                env_extra=invocation_env,
                memory_mb=sandbox._BENCH_MEMORY_MB,
                input_file=seed_file,
                asset_files=invocation_assets,
                on_output_line=observe_output,
                resource_usage=resource_usage,
            )
            seconds = time.monotonic() - started
            if code == 124:
                # A fresh child per pass keeps candidate-global state out of the next run.
                # A shared-deadline timeout is attributed using all completed side runs so far.
                shared = budget < request["timing"]["run_seconds"]
                deadline.fail(
                    side,
                    f"{side} invocation exceeded its {timeout:g}s time allowance",
                    shared=shared,
                    stopped_after=seconds,
                    limit="budget" if shared else "invocation",
                    allowance_seconds=round(timeout, 3),
                    peak_memory_bytes=resource_usage.get("peak_memory_bytes"),
                    **finished(output, public),
                )
            deadline.finished(side, seconds)
            deadline.remaining(side)
            if code:
                raise MeasurementFailure(side, f"bench exit {code}: {output[-1000:]}")
            rows = {}
            child_ns = {}
            warmup_child_ns = {}
            warmup_trusted_ns = {}
            real_preparation = {}
            for line in output.splitlines():
                if start_marker(line) or line.startswith(
                    ("BENCH_READY|", "BENCH_DONE|", "BENCH_WARMUP_READY|", "BENCH_WARMUP_DONE|")
                ):
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
                if line.startswith("WARMUP|"):
                    fields = line.split()
                    if len(fields) != 4 or fields[0].removeprefix("WARMUP|") not in public:
                        raise MeasurementFailure("infrastructure", "invalid warm-up output row")
                    printed_key = fields[0].removeprefix("WARMUP|")
                    event = warmup_events.get(printed_key)
                    if event is None or "finished_ns" not in event or fields[1] != "true":
                        raise MeasurementFailure("infrastructure", "missing warm-up timing markers")
                    try:
                        child_duration = int(fields[2])
                        checksum = int(fields[3])
                    except ValueError as exc:
                        raise MeasurementFailure(
                            "infrastructure", "invalid warm-up output values"
                        ) from exc
                    if child_duration <= 0 or not 0 <= checksum < 2**32:
                        raise MeasurementFailure(
                            "infrastructure", "invalid warm-up duration/checksum"
                        )
                    trusted_duration = event["finished_ns"] - event["started_ns"]
                    if trusted_duration <= 0 or printed_key in warmup_child_ns:
                        raise MeasurementFailure(
                            "infrastructure", "invalid/duplicate warm-up timing"
                        )
                    key = public[printed_key]
                    warmup_child_ns[key] = child_duration
                    warmup_trusted_ns[key] = trusted_duration
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
                event = inference_events.get(fields[0])
                if event is None or "finished_ns" not in event:
                    raise MeasurementFailure(
                        "infrastructure", "missing trusted inference timing markers"
                    )
                trusted_ns = event["finished_ns"] - event["started_ns"]
                if trusted_ns <= 0:
                    raise MeasurementFailure("infrastructure", "invalid trusted inference duration")
                rows[key] = (trusted_ns, checksum)
                child_ns[key] = ns
            if set(rows) != set(public.values()):
                raise MeasurementFailure("infrastructure", "missing or extra benchmark cells")
            if warmup_required and set(warmup_trusted_ns) != set(public.values()):
                raise MeasurementFailure("infrastructure", "missing or extra warm-up rows")
            if set(real_preparation) != {item["id"] for item in request.get("real_workloads", ())}:
                raise MeasurementFailure(
                    "infrastructure", "missing real workload preparation timing"
                )
            groups.append((rows, real_preparation, child_ns))
            if warmup_required:
                warmup_groups.append((warmup_trusted_ns, warmup_child_ns))
            process_wall_ns.append(round(seconds * 1e9))
            peak_memory_bytes.append(resource_usage.get("peak_memory_bytes"))

    result = {}
    for cell in groups[0][0]:
        combined_checksum = 2166136261
        for group, _, _ in groups:
            combined_checksum = ((combined_checksum ^ group[cell][1]) * 16777619) % 2**32
        result[cell] = (
            int(statistics.median(g[cell][0] for g, _, _ in groups)),
            combined_checksum,
        )
    result["__raw_ns__"] = {cell: [rows[cell][0] for rows, _, _ in groups] for cell in groups[0][0]}
    result["__child_ns__"] = {cell: [rows[cell] for _, _, rows in groups] for cell in groups[0][0]}
    result["__process_wall_ns__"] = process_wall_ns
    result["__peak_memory_bytes__"] = peak_memory_bytes
    if warmup_sequences is not None:
        result["__warmup_ns__"] = {
            cell: [group[0][cell] for group in warmup_groups] for cell in groups[0][0]
        }
        result["__warmup_child_ns__"] = {
            cell: [group[1][cell] for group in warmup_groups] for cell in groups[0][0]
        }
    if request.get("real_workloads"):
        result["__real_preparation_ns__"] = {
            item["id"]: {
                "model_load_ns": int(
                    statistics.median(prep[item["id"]][0] for _, prep, _ in groups)
                ),
                "input_preparation_ns": int(
                    statistics.median(prep[item["id"]][1] for _, prep, _ in groups)
                ),
            }
            for item in request["real_workloads"]
        }
    return result


def measure(request, candidate, reference, mode=None, progress_callback=None, runner=None):
    if "comparisons" in request:
        return measure_bundle(
            request,
            candidate,
            reference,
            mode=mode,
            progress_callback=progress_callback,
            runner=runner,
        )
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
        paths = []
        for field, hash_field in (
            ("weights_path", "weights_sha256"),
            ("samples_path", "samples_sha256"),
        ):
            source_path = sandbox.HERE / item[field]
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
                or file_hash(path) != item[hash_field]
            ):
                raise MeasurementFailure(
                    "infrastructure",
                    f"real workload asset missing or hash mismatch: {item['id']} {field}",
                )
            paths.append(path)
        try:
            RealWorkloadSpec.parse(item).validate_assets(*paths)
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
        runner = runner or run_binary
        try:
            mode = mode or preparation_mode(request)
            build_clock = Deadline("build", preparation_seconds(request["timing"], mode))
            build_stages = {}
            partial = {
                "build_seconds": 0.0,
                "build_stages": build_stages,
                "rounds": [],
                "real_rounds": [],
                "rounds_planned": request["timing"]["rounds"],
            }

            def run_build_stage(name, side, operation, *, skipped=False):
                if skipped:
                    build_stages[name] = {
                        "status": "skipped",
                        "seconds": 0.0,
                        "cache_lookup_seconds": 0.0,
                        "compile_seconds": 0.0,
                        "other_seconds": 0.0,
                    }
                    if progress_callback is not None:
                        progress_callback(
                            {
                                "phase": "build_stage",
                                "stage": name,
                                "evidence": dict(build_stages[name]),
                                "build_elapsed_seconds": round(build_clock.elapsed(), 3),
                            }
                        )
                    return None
                cap = build_stage_seconds(request["timing"], name)
                overall_left = build_clock.remaining(side)
                phase_budget = min(cap, overall_left)
                bounded_by_overall = overall_left < cap
                stage = Deadline(f"{name}_build", phase_budget)
                row = build_stages[name] = {
                    "status": "running",
                    "budget_seconds": cap,
                    "seconds": 0.0,
                    "cache_lookup_seconds": 0.0,
                    "compile_seconds": 0.0,
                    "other_seconds": 0.0,
                }
                cache_key = request.get(f"{name}_hash")
                lookup_started = time.monotonic()
                cached = cached_binary(cache_key) if cache_key is not None else None
                row["cache_lookup_seconds"] = round(time.monotonic() - lookup_started, 3)
                try:
                    if mode == "cached" and name != "shared" and cached is None:
                        raise MeasurementFailure(
                            "infrastructure",
                            "cached preparation unavailable; retry with cold preparation",
                        )
                    result = operation(stage, cached)
                    stage.remaining(side)
                    build_clock.remaining(side)
                    row["status"] = "cached" if cached is not None else "complete"
                    return result
                except TimeoutError as exc:
                    row["status"] = "failed"
                    try:
                        stage.fail(
                            side, f"{name} preparation exceeded its time allowance", shared=False
                        )
                    except MeasurementFailure as failure:
                        failure.timeout.update(
                            phase_budget_seconds=phase_budget,
                            configured_phase_seconds=cap,
                            bounded_by_overall=bounded_by_overall,
                        )
                        failure.partial = partial | {
                            "build_seconds": round(build_clock.elapsed(), 3)
                        }
                        raise failure from exc
                    raise AssertionError("unreachable") from exc
                except MeasurementFailure as exc:
                    row["status"] = "failed"
                    if exc.timeout is not None:
                        exc.timeout.update(
                            phase_budget_seconds=phase_budget,
                            configured_phase_seconds=cap,
                            bounded_by_overall=bounded_by_overall,
                        )
                    exc.partial = partial | {"build_seconds": round(build_clock.elapsed(), 3)}
                    raise
                except Exception as exc:
                    row["status"] = "failed"
                    failure = MeasurementFailure(
                        "infrastructure" if name == "shared" else side,
                        f"{name} build preparation: {exc}",
                    )
                    failure.partial = partial | {"build_seconds": round(build_clock.elapsed(), 3)}
                    raise failure from exc
                finally:
                    row["seconds"] = round(stage.elapsed(), 3)
                    row["compile_seconds"] = (
                        0.0
                        if cached is not None
                        else round(max(0, stage.elapsed() - row["cache_lookup_seconds"]), 3)
                    )
                    row["other_seconds"] = round(
                        max(
                            0,
                            row["seconds"] - row["cache_lookup_seconds"] - row["compile_seconds"],
                        ),
                        3,
                    )
                    if progress_callback is not None:
                        progress_callback(
                            {
                                "phase": "build_stage",
                                "stage": name,
                                "evidence": dict(row),
                                "build_elapsed_seconds": round(build_clock.elapsed(), 3),
                            }
                        )

            try:
                if mode == "cached":
                    build_stages["shared"] = {
                        "status": "cached",
                        "budget_seconds": build_stage_seconds(request["timing"], "shared"),
                        "seconds": 0.0,
                        "cache_lookup_seconds": 0.0,
                        "compile_seconds": 0.0,
                        "other_seconds": 0.0,
                    }
                    base = run_build_stage(
                        "baseline",
                        "infrastructure",
                        lambda _stage, hit: hit,
                        skipped=baseline is None,
                    )
                    ref = run_build_stage(
                        "reference",
                        "infrastructure",
                        lambda _stage, hit: hit,
                    )
                    sub = run_build_stage(
                        "candidate",
                        "infrastructure",
                        lambda _stage, hit: hit,
                    )
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

                    def prepare_shared(stage):
                        sandbox._budget.reset(stage.seconds, deadline=stage.end)
                        sandbox.prepare_shared(native=True, deadline=stage.end)

                    run_build_stage(
                        "shared",
                        "infrastructure",
                        lambda stage, _hit: prepare_shared(stage),
                    )
                    base = run_build_stage(
                        "baseline",
                        "baseline",
                        lambda stage, hit: build(
                            baseline,
                            request["baseline_hash"],
                            "baseline",
                            stage,
                            cached=hit,
                        ),
                        skipped=baseline is None,
                    )
                    ref = run_build_stage(
                        "reference",
                        "reference",
                        lambda stage, hit: build(
                            reference,
                            request["reference_hash"],
                            "reference",
                            stage,
                            cached=hit,
                        ),
                    )
                    sub = run_build_stage(
                        "candidate",
                        "candidate",
                        lambda stage, hit: build(
                            candidate,
                            request["candidate_hash"],
                            "candidate",
                            stage,
                            cached=hit,
                        ),
                    )
                build_clock.remaining("infrastructure" if mode == "cached" else "candidate")
            except MeasurementFailure as exc:
                if exc.partial is None:
                    exc.partial = partial | {"build_seconds": round(build_clock.elapsed(), 3)}
                raise
            build_seconds = build_clock.elapsed()
            partial["build_seconds"] = round(build_seconds, 3)
            build_stage_total = sum(row["seconds"] for row in build_stages.values())
            build_overhead_seconds = round(max(0.0, build_seconds - build_stage_total), 3)
            benchmark_clock = Deadline("benchmark", request["timing"]["benchmark_seconds"])
            # What the measurement has reached, kept with a failure that stops it partway.
            rounds = []
            real_rounds = []
            partial["rounds"] = rounds
            partial["real_rounds"] = real_rounds
            try:
                # One inference per workload/cell in each fresh pass. A later paired repetition
                # runs in a new process, so state cannot carry across repetitions of that row.
                reps = {
                    f"{draw['id']}|{cell}": 1
                    for draw in request["workloads"]
                    for cell in request["cells"]
                }
                partial["measurement_policy"] = {
                    "observations_per_workload_per_process": 1,
                    "fresh_process_per_pass": True,
                    "weights_reloaded_each_pass": True,
                    "inference_clock": "supervisor-monotonic-ready-done-v1",
                    "baseline_sentinel_policy": "spaced-same-input-per-family-v1",
                }
                if request.get("warmup_policy") == "independent-unscored-input-v1":
                    partial["measurement_policy"].update(
                        unscored_warmup_per_row_per_process=True,
                        warmup_inputs_independent_and_frozen=True,
                    )
                partial["repetitions"] = reps
                baseline_reps = {}
                baseline_rounds = []
                baseline_sentinel_inputs = None
                baseline_sentinel_warmups = None
                sentinel_rows = baseline_sentinel_rows(
                    request["workloads"], request["cells"], request.get("real_workloads", ())
                )
                sentinel_synthetic = set(sentinel_rows["synthetic"])
                sentinel_real = set(sentinel_rows["real"])
                if base is not None:
                    baseline_reps = dict.fromkeys(reps, 1)
                    baseline_rounds_count = request["timing"].get("baseline_rounds", 3)
                    baseline_indices = set(
                        baseline_round_indices(request["timing"]["rounds"], baseline_rounds_count)
                    )
                    baseline_positions = {
                        index: position for position, index in enumerate(sorted(baseline_indices))
                    }
                    partial["baseline_repetitions"] = baseline_reps
                # Each paired measurement has one inference per fresh process. The `inner`
                # setting controls independent processes per paired round, not calls within one.
                benchmark_clock.share()
                t0 = time.monotonic()
                used_input_seeds = set()
                for i in range(request["timing"]["rounds"]):
                    baseline_this_round = base is not None and i in baseline_indices
                    side_binaries = {"reference": ref, "candidate": sub}
                    pair_order = (
                        ["reference", "candidate"] if i % 2 == 0 else ["candidate", "reference"]
                    )
                    if baseline_this_round:
                        side_binaries["baseline"] = base
                        baseline_position = min(baseline_positions[i], 2)
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
                        real_round_index=i,
                    )
                    paired_warmups = (
                        make_warmup_sequences(
                            request,
                            run_reps,
                            request["timing"]["inner"],
                            i,
                        )
                        if request.get("synthetic_warmup_plan") is not None
                        or request.get("real_warmup_plan") is not None
                        else None
                    )
                    paired_baseline_inputs = {
                        key: [sequences[0]] for key, sequences in paired_inputs.items()
                    }
                    if i == 0:
                        baseline_sentinel_inputs = paired_baseline_inputs
                        if paired_warmups is not None:
                            baseline_sentinel_warmups = {
                                key: [sequences[0]] for key, sequences in paired_warmups.items()
                            }
                    baseline_inputs_for_round = dict(paired_baseline_inputs)
                    baseline_warmups_for_round = (
                        {key: [sequences[0]] for key, sequences in paired_warmups.items()}
                        if paired_warmups is not None
                        else None
                    )
                    baseline_input_plan = None
                    if baseline_this_round:
                        for key in sentinel_synthetic:
                            baseline_inputs_for_round[key] = baseline_sentinel_inputs[key]
                            if baseline_warmups_for_round is not None:
                                baseline_warmups_for_round[key] = baseline_sentinel_warmups[key]
                        for item in request.get("real_workloads", ()):
                            if item["id"] in sentinel_real:
                                key = f"{item['id']}|{item['family']}"
                                baseline_inputs_for_round[key] = baseline_sentinel_inputs[key]
                                if baseline_warmups_for_round is not None:
                                    baseline_warmups_for_round[key] = baseline_sentinel_warmups[key]
                        baseline_inputs_for_round["__cache_pass__"] = baseline_positions[i]
                        baseline_input_plan = {
                            "synthetic": {
                                key: values[0]
                                for key, values in baseline_inputs_for_round.items()
                                if key in reps
                            },
                            "real": {
                                item["id"]: baseline_sentinel_inputs[
                                    f"{item['id']}|{item['family']}"
                                ]
                                if item["id"] in sentinel_real
                                else baseline_inputs_for_round[f"{item['id']}|{item['family']}"]
                                for item in request.get("real_workloads", ())
                            },
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
                    paired_warmup_plan = input_plan_record(paired_warmups, request, run_reps)
                    baseline_warmup_plan = (
                        input_plan_record(baseline_warmups_for_round, request, baseline_reps)
                        if baseline_this_round
                        else None
                    )
                    # A side that finished before the other was stopped is kept with the failure.
                    observed = partial["round_in_progress"] = {"input_plan": paired_plan}
                    if paired_warmup_plan is not None:
                        observed["warmup_plan"] = paired_warmup_plan
                    if baseline_this_round:
                        observed["baseline_input_plan"] = baseline_input_plan
                        if baseline_warmup_plan is not None:
                            observed["baseline_warmup_plan"] = baseline_warmup_plan
                    for side in side_order:
                        repetitions = baseline_reps if side == "baseline" else reps
                        args = (
                            side_binaries[side],
                            request,
                            repetitions,
                            1 if side == "baseline" else request["timing"]["inner"],
                            side,
                            benchmark_clock,
                            baseline_inputs_for_round if side == "baseline" else paired_inputs,
                        )
                        if paired_warmups is not None:
                            observed[side] = runner(
                                *args,
                                warmup_sequences=(
                                    baseline_warmups_for_round
                                    if side == "baseline"
                                    else paired_warmups
                                ),
                            )
                        else:
                            observed[side] = runner(*args)
                        if progress_callback is not None:
                            progress_callback(
                                {
                                    "phase": "measurement_side",
                                    "round": i,
                                    "side": side,
                                    "input_plan": (
                                        baseline_input_plan if side == "baseline" else paired_plan
                                    ),
                                    "warmup_plan": (
                                        baseline_warmup_plan
                                        if side == "baseline"
                                        else paired_warmup_plan
                                    ),
                                    "observations": observed[side],
                                    "benchmark_elapsed_seconds": round(
                                        benchmark_clock.elapsed(), 3
                                    ),
                                }
                            )
                    cells = {}
                    for c in reps:
                        a, b = observed["reference"][c], observed["candidate"][c]
                        if a[1] != b[1]:
                            raise MeasurementFailure("checksum", f"output mismatch on {c}")
                        baseline_reference = (
                            rounds[0]["cells"][c]["reference_checksum"]
                            if baseline_this_round and c in sentinel_synthetic and i > 0
                            else a[1]
                        )
                        if baseline_this_round and observed["baseline"][c][1] != baseline_reference:
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
                    if paired_warmup_plan is not None:
                        rounds[-1]["warmup_plan"] = paired_warmup_plan
                    rounds[-1]["process_wall_ns"] = {
                        side: observed[side]["__process_wall_ns__"]
                        for side in ("reference", "candidate")
                    }
                    rounds[-1]["process_peak_memory_bytes"] = {
                        side: observed[side]["__peak_memory_bytes__"]
                        for side in ("reference", "candidate")
                    }
                    if paired_warmup_plan is not None:
                        rounds[-1]["warmup_ns"] = {
                            side: observed[side]["__warmup_ns__"]
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
                            baseline_reference = (
                                real_rounds[0]["workloads"][item["id"]]["reference_checksum"]
                                if baseline_this_round and item["id"] in sentinel_real and i > 0
                                else ref_row[1]
                            )
                            if (
                                baseline_this_round
                                and observed["baseline"][key][1] != baseline_reference
                            ):
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
                                    baseline_inputs_for_round[key][0]
                                    if baseline_this_round
                                    else None
                                ),
                            }
                            if paired_warmups is not None:
                                row["reference_warmup_ns"] = observed["reference"]["__warmup_ns__"][
                                    key
                                ][0]
                                row["candidate_warmup_ns"] = observed["candidate"]["__warmup_ns__"][
                                    key
                                ][0]
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
                                if paired_warmups is not None:
                                    row["baseline_warmup_ns"] = observed["baseline"][
                                        "__warmup_ns__"
                                    ][key][0]
                            real_cells[item["id"]] = row
                        real_rounds.append({"paired_round": i, "workloads": real_cells})
                    if baseline_this_round:
                        baseline_rounds.append(
                            {
                                "paired_round": i,
                                "input_plan": baseline_input_plan,
                                **(
                                    {"warmup_plan": baseline_warmup_plan}
                                    if baseline_warmup_plan is not None
                                    else {}
                                ),
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
                                "process_peak_memory_bytes": observed["baseline"][
                                    "__peak_memory_bytes__"
                                ],
                                **(
                                    {
                                        "warmup_ns": {
                                            key: values[0]
                                            for key, values in observed["baseline"][
                                                "__warmup_ns__"
                                            ].items()
                                        }
                                    }
                                    if paired_warmups is not None
                                    else {}
                                ),
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
                candidate_median = statistics.median(row["candidate_ns"] for row in observations)
                reference_median = statistics.median(row["reference_ns"] for row in observations)
                baseline_values = [
                    row["baseline_ns"] for row in observations if "baseline_ns" in row
                ]
                baseline_median = statistics.median(baseline_values) if baseline_values else None
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
                    "candidate_model_load_median_ns": statistics.median(
                        row["candidate_model_load_ns"] for row in observations
                    ),
                    "reference_model_load_median_ns": statistics.median(
                        row["reference_model_load_ns"] for row in observations
                    ),
                    "candidate_input_preparation_median_ns": statistics.median(
                        row["candidate_input_preparation_ns"] for row in observations
                    ),
                    "reference_input_preparation_median_ns": statistics.median(
                        row["reference_input_preparation_ns"] for row in observations
                    ),
                    "baseline_model_load_median_ns": statistics.median(
                        row["baseline_model_load_ns"]
                        for row in observations
                        if "baseline_model_load_ns" in row
                    )
                    if baseline_median
                    else None,
                    "baseline_input_preparation_median_ns": statistics.median(
                        row["baseline_input_preparation_ns"]
                        for row in observations
                        if "baseline_input_preparation_ns" in row
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
                    baseline_median = statistics.median(values)
                    candidate_median = statistics.median(
                        row["cells"][cell]["candidate_ns"] for row in rounds
                    )
                    reference_median = statistics.median(
                        row["cells"][cell]["reference_ns"] for row in rounds
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
                        "interpretation": (
                            "unpaired diagnostic; admission remains candidate/reference"
                        ),
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
                    "sentinel_rows": sentinel_rows,
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
                    "build_overhead": build_overhead_seconds,
                    "build_stages": build_stages,
                    "baseline_process": sum(sum(row["process_wall_ns"]) for row in baseline_rounds)
                    / 1e9,
                    "rounds": round_seconds,
                    "benchmark": benchmark_clock.elapsed(),
                },
                "preparation": {
                    "mode": mode,
                    "budget_seconds": build_clock.seconds,
                    "stage_budgets_seconds": {
                        stage: build_stage_seconds(request["timing"], stage)
                        for stage in ("shared", "baseline", "reference", "candidate")
                    },
                },
            }
        finally:
            if original is not None:
                sandbox.WORK.write_bytes(original)
            else:
                sandbox.WORK.unlink(missing_ok=True)


def measure_bundle(request, candidate, references, mode=None, progress_callback=None, runner=None):
    """Measure a candidate once, then compare those same observations to each board champion."""
    comparisons = request.get("comparisons")
    if not isinstance(comparisons, dict) or not comparisons:
        raise MeasurementFailure("infrastructure", "invalid comparison bundle")
    if isinstance(references, bytes):
        references = {request["reference_hash"]: references}
    if not isinstance(references, dict):
        raise MeasurementFailure("infrastructure", "bundle references must be keyed by hash")

    board_requests = {board: board_request(request, board) for board in comparisons}
    required_hashes = {item["reference_hash"] for item in comparisons.values()}
    if set(references) != required_hashes:
        raise MeasurementFailure("infrastructure", "bundle reference source set mismatch")
    if hashlib.sha256(candidate).hexdigest() != request.get("candidate_hash"):
        raise MeasurementFailure("infrastructure", "bundle candidate source hash mismatch")
    if any(hashlib.sha256(source).hexdigest() != sha for sha, source in references.items()):
        raise MeasurementFailure("infrastructure", "bundle reference source hash mismatch")
    for row in board_requests.values():
        MeasurementRequest.parse(row)
    key_sets = {board: set(row_names(board_requests[board]).values()) for board in board_requests}
    all_keys = set().union(*key_sets.values())
    anchors = [board for board, keys in key_sets.items() if keys == all_keys]
    if not anchors:
        raise MeasurementFailure(
            "infrastructure", "no board covers every workload needed for shared candidate timing"
        )
    ordered_boards = [anchors[0]] + [board for board in board_requests if board != anchors[0]]

    # Candidate and baseline passes are already trusted, fresh-process observations. Reuse their
    # rows only after checking the exact coordinator-selected inputs for every projected key.
    role_cache = {}
    trusted_runner = runner or run_binary

    def shared_runner(binary, board_request_value, reps, inner, side, deadline, input_sequences):
        if side not in ("candidate", "baseline", "reference"):
            return trusted_runner(
                binary,
                board_request_value,
                reps,
                inner,
                side,
                deadline,
                input_sequences,
            )
        keys = set(row_names(board_request_value).values())
        cache_key = side, str(binary)
        cache_pass = input_sequences.get("__cache_pass__")
        for cached in role_cache.get(cache_key, ()):
            if cached.get("cache_pass") != cache_pass:
                continue
            if not keys <= set(cached["rows"]):
                continue
            for key in keys:
                if input_sequences.get(key) != cached["inputs"].get(key):
                    break
            else:
                projected = {key: cached["rows"][key] for key in keys}
                projected["__raw_ns__"] = {
                    key: cached["raw_ns"][key] for key in keys if key in cached["raw_ns"]
                }
                projected["__real_preparation_ns__"] = {
                    key: value
                    for key, value in cached["real_preparation_ns"].items()
                    if key in {item["id"] for item in board_request_value.get("real_workloads", ())}
                }
                projected["__process_wall_ns__"] = cached["process_wall_ns"]
                projected["__peak_memory_bytes__"] = cached["peak_memory_bytes"]
                return projected

        observed = trusted_runner(
            binary,
            board_request_value,
            reps,
            inner,
            side,
            deadline,
            input_sequences,
        )
        role_cache.setdefault(cache_key, []).append(
            {
                "binary": str(binary),
                "rows": {key: observed[key] for key in keys},
                "raw_ns": dict(observed.get("__raw_ns__", {})),
                "real_preparation_ns": dict(observed.get("__real_preparation_ns__", {})),
                "process_wall_ns": observed.get("__process_wall_ns__"),
                "peak_memory_bytes": observed.get("__peak_memory_bytes__"),
                "cache_pass": cache_pass,
                "inputs": {key: input_sequences[key] for key in keys},
            }
        )
        return observed

    started = time.monotonic()
    board_results = {}
    for board in ordered_boards:
        board_request_value = board_requests[board]

        def report_board_progress(event, selected_board=board):
            if progress_callback is not None:
                progress_callback({"board": selected_board, **event})

        board_mode = mode
        if board_mode is None:
            board_mode = preparation_mode(board_request_value)
        try:
            board_results[board] = measure(
                board_request_value,
                candidate,
                references[comparisons[board]["reference_hash"]],
                mode=board_mode,
                progress_callback=report_board_progress,
                runner=shared_runner,
            )
        except MeasurementFailure as exc:
            board_partial = exc.partial
            exc.partial = {
                "completed_comparisons": board_results,
                "failed_comparison": board,
                "comparison_partial": board_partial,
                "bundle_elapsed_seconds": round(time.monotonic() - started, 3),
            }
            raise
        except Exception as exc:
            failure = MeasurementFailure("infrastructure", f"bundle comparison {board}: {exc}")
            failure.partial = {
                "completed_comparisons": board_results,
                "failed_comparison": board,
                "bundle_elapsed_seconds": round(time.monotonic() - started, 3),
            }
            raise failure from exc

    def shared_processes(side, field):
        return [
            value
            for (cached_side, _), entries in role_cache.items()
            if cached_side == side
            for entry in entries
            for value in (entry[field] or [])
        ]

    return {
        "bundle_hash": digest(request),
        "comparisons": board_results,
        "shared_candidate_process_wall_ns": shared_processes("candidate", "process_wall_ns"),
        "shared_candidate_peak_memory_bytes": shared_processes("candidate", "peak_memory_bytes"),
        "shared_baseline_process_wall_ns": shared_processes("baseline", "process_wall_ns"),
        "timing_seconds": round(time.monotonic() - started, 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--references-dir", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--progress", type=Path)
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
    sequence = 0

    def persist_progress(event):
        nonlocal sequence
        if args.progress is None:
            return
        args.progress.parent.mkdir(parents=True, exist_ok=True)
        with args.progress.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"sequence": sequence, "event": event}, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        sequence += 1

    try:
        if args.references_dir is not None:
            references = {
                path.name: path.read_bytes()
                for path in args.references_dir.iterdir()
                if path.is_file()
            }
        elif args.reference is not None:
            references = args.reference.read_bytes()
        else:
            raise ValueError("a reference source or reference directory is required")
        value = {
            "result": measure(
                json.loads(args.request.read_text()),
                args.candidate.read_bytes(),
                references,
                mode=args.preparation_mode,
                progress_callback=persist_progress,
            )
        }
    except MeasurementFailure as exc:
        value = {"error": exc.error()}
    except Exception as exc:
        value = {"error": {"kind": "infrastructure", "reason": str(exc)}}
    args.result.write_text(json.dumps(value))


if __name__ == "__main__":
    main()
