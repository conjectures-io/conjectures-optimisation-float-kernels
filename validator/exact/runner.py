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
from dataclasses import replace
from pathlib import Path

import fingerprint
import gate

from .config import digest, measurement_hash


class MeasurementFailure(Exception):
    def __init__(self, kind, reason):
        self.kind = kind
        super().__init__(reason)


def machine_identity():
    """Stable execution identity, computed on the worker, not asserted by the coordinator."""
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
            "affinity": gate._BENCH_CPU,
            "memory_mb": gate._BENCH_MEMORY_MB,
            "sandbox": gate._SANDBOX,
            "toolchain": (gate.LIB / "lean-toolchain").read_text(),
            "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
            "kernel": platform.release(),
            "bubblewrap": subprocess.check_output([gate.BWRAP, "--version"], text=True).strip(),
            "policy": "exact-local-pairs-v1",
        }
    )


def build(source, expected, side):
    if hashlib.sha256(source).hexdigest() != expected:
        raise MeasurementFailure("infrastructure", "immutable source hash mismatch")
    cache = (
        gate.HERE
        / "pinned"
        / "exact-jobs"
        / digest([expected, fingerprint.current(), machine_identity()])
    )
    binary = cache / "bench"
    if binary.is_file():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    src = cache / "Kernel.lean"
    src.write_bytes(source)
    verdict = gate.Verdict(submission=expected, slot="exact")
    gate.wipe(gate.LAYER)
    (gate.LAYER / "upper").mkdir(parents=True)
    (gate.LAYER / "work").mkdir()
    gate._budget.reset()
    try:
        for stage in (gate.intake, gate.policy, gate.statement):
            if reason := stage(src, verdict):
                raise MeasurementFailure(side, reason)
        built = gate.LAYER / "upper" / "bin" / "benchExact"
        if not built.exists():
            built = gate.BUILD / "bin" / "benchExact"
        tmp = binary.with_suffix(".tmp")
        shutil.copy2(built, tmp)
        tmp.replace(binary)
    finally:
        gate.wipe(gate.LAYER)
    return binary


def run_binary(binary, request, reps, inner, side):
    extra = {
        "BENCH_CELLS": ",".join(request["cells"]),
        "BENCH_REPS": ",".join(f"{c}={n}" for c, n in reps.items()),
        "BENCH_SEED": str(request["seed"]),
        "BENCH_INNER": str(inner),
        "LEAN_ABORT_ON_PANIC": "1",
    }
    command = [str(binary)]
    if gate._BENCH_CPU:
        command = ["taskset", "-c", gate._BENCH_CPU, *command]
    code, output = gate.run(
        command, request["timing"]["run_seconds"], env_extra=extra, memory_mb=gate._BENCH_MEMORY_MB
    )
    if code:
        raise MeasurementFailure(side, f"bench exit {code}: {output[-1000:]}")
    rows = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 4 or fields[1] != "true" or fields[0] in rows:
            raise MeasurementFailure("infrastructure", "invalid or duplicate benchmark row")
        try:
            ns, checksum = int(fields[2]), int(fields[3])
        except ValueError as exc:
            raise MeasurementFailure("infrastructure", "noninteger benchmark output") from exc
        if ns <= 0 or not 0 <= checksum < 2**32:
            raise MeasurementFailure("infrastructure", "invalid duration or checksum")
        rows[fields[0]] = (ns, checksum)
    if set(rows) != set(request["cells"]):
        raise MeasurementFailure("infrastructure", "missing or extra benchmark cells")
    return rows


def measure(request, candidate, reference):
    if request["identity"] != machine_identity():
        raise MeasurementFailure("infrastructure", "hardware/execution identity mismatch")
    if (
        request["benchmark_identity"] != fingerprint.current()
        or request["proof_identity"] != fingerprint.current()
    ):
        raise MeasurementFailure(
            "infrastructure", "checkout does not match pinned verifier/benchmark"
        )
    gate.check_limits()
    # gate.run uses shared Lake/overlay scratch paths; hold this checkout lock throughout.
    with open(gate.HERE / "gate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = gate.WORK.read_bytes() if gate.WORK.exists() else None
        try:
            try:
                gate.pin(
                    replace(
                        gate.SLOTS["exact"], incumbent=gate.KERNELS / "exact/incumbent/Kernel.lean"
                    )
                )
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
            if fingerprint.current() != request["benchmark_identity"]:
                raise MeasurementFailure("infrastructure", "checkout changed during benchmark")
            return {
                "request_hash": measurement_hash(request),
                "provenance": {
                    "identity": machine_identity(),
                    "machine": socket.gethostname(),
                    "architecture": platform.machine(),
                    "cpu_affinity": gate._BENCH_CPU,
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
                gate.WORK.write_bytes(original)
            else:
                gate.WORK.unlink(missing_ok=True)


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
                    "proof_identity": fingerprint.current(),
                    "benchmark_identity": fingerprint.current(),
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
