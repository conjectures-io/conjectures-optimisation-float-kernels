"""A supervised subprocess for sandboxed architecture-local builds and paired measurements."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

import sandbox
from config import MeasurementRequest, digest, measurement_hash
from multiseed import calibrated_counts
from timing import preparation_seconds

import fingerprint

# The only translation of legacy Lean suite labels; persisted requests use public names.
LEAN_MODELS = {"resnet": "cnn", "mobilenet": "mnet", "normalized_mlp": "nmlp"}


def lean_cell(cell):
    model, rest = cell.split(":", 1)
    return LEAN_MODELS.get(model, model) + ":" + rest


class MeasurementFailure(Exception):
    def __init__(self, kind, reason):
        self.kind = kind
        super().__init__(reason)


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

    def fail(self, side, detail=None, *, shared=True, stopped_after=None):
        if shared and self.runs is not None:
            side = self.at_fault(side, stopped_after)
        detail = detail or f"exceeded {self.seconds:g}s {self.phase} budget"
        raise MeasurementFailure(side, f"{self.phase}-timeout: {detail}")

    def remaining(self, side):
        left = self.end - time.monotonic()
        if left <= 0:
            self.fail(side)
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
    if sandbox.CONFIG.is_dir() and all(
        cached_binary(request[k]) is not None for k in ("reference_hash", "candidate_hash")
    ):
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


def run_binary(binary, request, reps, inner, side, deadline):
    budget = deadline.remaining(side)
    timeout = min(request["timing"]["run_seconds"], budget)
    extra = {
        "BENCH_CELLS": ",".join(lean_cell(c) for c in request["cells"]),
        "BENCH_REPS": ",".join(f"{lean_cell(c)}={n}" for c, n in reps.items()),
        "BENCH_SEED": str(request["seed"]),
        "BENCH_INNER": str(inner),
        "LEAN_ABORT_ON_PANIC": "1",
    }
    extra["BENCH_WORKLOADS"] = ",".join(
        f"{w['id']}:{w['seed']}:{w['profile']}" for w in request["workloads"]
    )
    extra["BENCH_REPS"] = ",".join(
        f"{k.split('|')[0]}|{lean_cell(k.split('|')[1])}={n}" for k, n in reps.items()
    )
    command = [str(binary)]
    if sandbox._BENCH_CPU:
        command = ["taskset", "-c", sandbox._BENCH_CPU, *command]
    started = time.monotonic()
    code, output = sandbox.run(
        command,
        timeout,
        env_extra=extra,
        memory_mb=sandbox._BENCH_MEMORY_MB,
    )
    seconds = time.monotonic() - started
    if code == 124:
        # The per-run cap is this side's own; running out of the shared deadline is decided by
        # each side's time per run.
        deadline.fail(
            side,
            f"{side} invocation exceeded its {timeout:g}s time allowance",
            shared=budget < request["timing"]["run_seconds"],
            stopped_after=seconds,
        )
    deadline.finished(side, seconds)
    deadline.remaining(side)
    if code:
        raise MeasurementFailure(side, f"bench exit {code}: {output[-1000:]}")
    public = {
        f"{w['id']}|{lean_cell(c)}": f"{w['id']}|{c}"
        for w in request["workloads"]
        for c in request["cells"]
    }
    rows = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 4 or fields[1] != "true" or public.get(fields[0]) in rows:
            raise MeasurementFailure("infrastructure", "invalid or duplicate benchmark row")
        try:
            ns, checksum = int(fields[2]), int(fields[3])
        except ValueError as exc:
            raise MeasurementFailure("infrastructure", "noninteger benchmark output") from exc
        if ns <= 0 or not 0 <= checksum < 2**32:
            raise MeasurementFailure("infrastructure", "invalid duration or checksum")
        if fields[0] not in public:
            raise MeasurementFailure("infrastructure", "unknown benchmark cell")
        rows[public[fields[0]]] = (ns, checksum)
    if set(rows) != set(public.values()):
        raise MeasurementFailure("infrastructure", "missing or extra benchmark cells")
    deadline.remaining(side)
    return rows


def measure(request, candidate, reference, mode=None):
    MeasurementRequest.parse(request)
    if request["identity"] != machine_identity():
        raise MeasurementFailure("infrastructure", "hardware/execution identity mismatch")
    if request["benchmark_identity"] != fingerprint.benchmark():
        raise MeasurementFailure(
            "infrastructure", "checkout does not match pinned verifier/benchmark"
        )
    for content, key in ((candidate, "candidate_hash"), (reference, "reference_hash")):
        if hashlib.sha256(content).hexdigest() != request[key]:
            raise MeasurementFailure("infrastructure", "immutable source hash mismatch")
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
                ref, sub = (
                    cached_binary(request["reference_hash"]),
                    cached_binary(request["candidate_hash"]),
                )
                if ref is None or sub is None or not sandbox.CONFIG.is_dir():
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
                ref = build(reference, request["reference_hash"], "reference", build_clock)
                sub = build(candidate, request["candidate_hash"], "candidate", build_clock)
            build_clock.remaining("infrastructure" if mode == "cached" else "candidate")
            build_seconds = build_clock.elapsed()
            benchmark_clock = Deadline("benchmark", request["timing"]["benchmark_seconds"])
            initial = run_binary(ref, request, {}, 3, "reference", benchmark_clock)
            reps = calibrated_counts(initial, request["timing"]["cell_ms"])
            checked = run_binary(ref, request, reps, 3, "reference", benchmark_clock)
            reps = calibrated_counts(checked, request["timing"]["cell_ms"], reps)
            # Calibration runs only the reference; the rounds share what is left between both sides.
            benchmark_clock.share()
            rounds = []
            t0 = time.monotonic()
            for i in range(request["timing"]["rounds"]):
                sides = [("reference", ref), ("candidate", sub)]
                if i % 2:
                    sides.reverse()
                observed = {
                    side: run_binary(
                        binary, request, reps, request["timing"]["inner"], side, benchmark_clock
                    )
                    for side, binary in sides
                }
                cells = {}
                for c in reps:
                    a, b = observed["reference"][c], observed["candidate"][c]
                    if a[1] != b[1]:
                        raise MeasurementFailure("checksum", f"output mismatch on {c}")
                    cells[c] = {
                        "reference_ns": a[0],
                        "candidate_ns": b[0],
                        "reference_checksum": a[1],
                        "candidate_checksum": b[1],
                    }
                rounds.append({"reference_first": i % 2 == 0, "cells": cells})
            round_seconds = time.monotonic() - t0
            benchmark_clock.remaining("candidate")
            if fingerprint.benchmark() != request["benchmark_identity"]:
                raise MeasurementFailure("infrastructure", "checkout changed during benchmark")
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
                "calibration": {c: ns for c, (ns, _) in initial.items()},
                "repetitions": reps,
                "rounds": rounds,
                "timing_seconds": round_seconds,
                "stage_seconds": {
                    "build": build_seconds,
                    "calibration": t0 - benchmark_clock.started,
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
        value = {"error": {"kind": exc.kind, "reason": str(exc)}}
    except Exception as exc:
        value = {"error": {"kind": "infrastructure", "reason": str(exc)}}
    args.result.write_text(json.dumps(value))


if __name__ == "__main__":
    main()
