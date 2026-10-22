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
from config import digest, measurement_hash

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


def build(source, expected, side):
    if hashlib.sha256(source).hexdigest() != expected:
        raise MeasurementFailure("infrastructure", "immutable source hash mismatch")
    cache = (
        sandbox.HERE
        / "pinned"
        / "exact-jobs"
        / digest([expected, fingerprint.benchmark(), machine_identity()])
    )
    binary = cache / "bench"
    if binary.is_file():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    src = cache / "Kernel.lean"
    src.write_bytes(source)
    verdict = sandbox.Verdict(submission=expected)
    sandbox.wipe(sandbox.LAYER)
    (sandbox.LAYER / "upper").mkdir(parents=True)
    (sandbox.LAYER / "work").mkdir()
    sandbox._budget.reset()
    try:
        for stage in (sandbox.intake, sandbox.policy, sandbox.statement):
            if reason := stage(src, verdict):
                raise MeasurementFailure(side, reason)
        code, output = sandbox.own(
            [sandbox.LAKE, "build", "benchExact"], sandbox._BUILD_TIMEOUT, build="write"
        )
        if code or sandbox.foreign(sandbox.LAYER / "upper"):
            raise MeasurementFailure(side, f"native build failed: {sandbox.errors(output, code)}")
        built = sandbox.LAYER / "upper" / "bin" / "benchExact"
        if not built.exists():
            built = sandbox.BUILD / "bin" / "benchExact"
        tmp = binary.with_suffix(".tmp")
        shutil.copy2(built, tmp)
        tmp.replace(binary)
    finally:
        sandbox.wipe(sandbox.LAYER)
    return binary


def run_binary(binary, request, reps, inner, side):
    extra = {
        "BENCH_CELLS": ",".join(lean_cell(c) for c in request["cells"]),
        "BENCH_REPS": ",".join(f"{lean_cell(c)}={n}" for c, n in reps.items()),
        "BENCH_SEED": str(request["seed"]),
        "BENCH_INNER": str(inner),
        "LEAN_ABORT_ON_PANIC": "1",
    }
    command = [str(binary)]
    if sandbox._BENCH_CPU:
        command = ["taskset", "-c", sandbox._BENCH_CPU, *command]
    code, output = sandbox.run(
        command,
        request["timing"]["run_seconds"],
        env_extra=extra,
        memory_mb=sandbox._BENCH_MEMORY_MB,
    )
    if code:
        raise MeasurementFailure(side, f"bench exit {code}: {output[-1000:]}")
    rows = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 4 or fields[1] != "true" or fields[0] in {lean_cell(c) for c in rows}:
            raise MeasurementFailure("infrastructure", "invalid or duplicate benchmark row")
        try:
            ns, checksum = int(fields[2]), int(fields[3])
        except ValueError as exc:
            raise MeasurementFailure("infrastructure", "noninteger benchmark output") from exc
        if ns <= 0 or not 0 <= checksum < 2**32:
            raise MeasurementFailure("infrastructure", "invalid duration or checksum")
        public = {lean_cell(c): c for c in request["cells"]}
        if fields[0] not in public:
            raise MeasurementFailure("infrastructure", "unknown benchmark cell")
        rows[public[fields[0]]] = (ns, checksum)
    if set(rows) != set(request["cells"]):
        raise MeasurementFailure("infrastructure", "missing or extra benchmark cells")
    return rows


def measure(request, candidate, reference):
    if request["identity"] != machine_identity():
        raise MeasurementFailure("infrastructure", "hardware/execution identity mismatch")
    if request["benchmark_identity"] != fingerprint.benchmark():
        raise MeasurementFailure(
            "infrastructure", "checkout does not match pinned verifier/benchmark"
        )
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
            try:
                sandbox.prepare_shared(native=True)
            except Exception as exc:
                raise MeasurementFailure(
                    "infrastructure", f"shared build preparation: {exc}"
                ) from exc
            ref = build(reference, request["reference_hash"], "reference")
            sub = build(candidate, request["candidate_hash"], "candidate")
            initial = run_binary(ref, request, {}, 1, "reference")
            reps = {
                c: max(1, min(1000000, int(request["timing"]["cell_ms"] * 1e6 / ns)))
                for c, (ns, _) in initial.items()
            }
            rounds = []
            t0 = time.monotonic()
            for i in range(request["timing"]["rounds"]):
                sides = [("reference", ref), ("candidate", sub)]
                if i % 2:
                    sides.reverse()
                observed = {
                    side: run_binary(binary, request, reps, request["timing"]["inner"], side)
                    for side, binary in sides
                }
                cells = {}
                for c in request["cells"]:
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
            if fingerprint.benchmark() != request["benchmark_identity"]:
                raise MeasurementFailure("infrastructure", "checkout changed during benchmark")
            return {
                "request_hash": measurement_hash(request),
                "provenance": {
                    "identity": machine_identity(),
                    "machine": socket.gethostname(),
                    "architecture": platform.machine(),
                    "cpu_affinity": sandbox._BENCH_CPU,
                    "load": os.getloadavg(),
                    "candidate_binary": hashlib.sha256(sub.read_bytes()).hexdigest(),
                    "reference_binary": hashlib.sha256(ref.read_bytes()).hexdigest(),
                },
                "calibration": {c: ns for c, (ns, _) in initial.items()},
                "repetitions": reps,
                "rounds": rounds,
                "timing_seconds": time.monotonic() - t0,
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
            )
        }
    except MeasurementFailure as exc:
        value = {"error": {"kind": exc.kind, "reason": str(exc)}}
    except Exception as exc:
        value = {"error": {"kind": "infrastructure", "reason": str(exc)}}
    args.result.write_text(json.dumps(value))


if __name__ == "__main__":
    main()
