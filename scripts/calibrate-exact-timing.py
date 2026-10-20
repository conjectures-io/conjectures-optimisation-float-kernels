#!/usr/bin/env python3
"""Collect a local, bounded timing pilot using frozen, proved exact-runtime binaries."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def prepare(raw):
    """Build only trusted repository fixtures; restore the operator's work file afterward."""
    work = ROOT / "lean/work/Submission.lean"
    original = work.read_bytes() if work.exists() else None
    binaries = {}
    lake = shutil.which("lake") or str(Path.home() / ".elan/bin/lake")
    try:
        for name in ("slowdepthwise", "incumbent"):
            source = ROOT / f"kernels/exact/{name}/Kernel.lean"
            directory = raw / name
            directory.mkdir()
            shutil.copy2(source, work)
            shutil.copy2(source, directory / "Kernel.lean")
            with (directory / "build.log").open("w") as log:
                subprocess.run(
                    [lake, "build", "VerifyExact", "benchExact"],
                    cwd=ROOT / "lean",
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=600,
                )
            binary = directory / "benchExact"
            shutil.copy2(ROOT / "lean/.lake/build/bin/benchExact", binary)
            binaries[name] = {
                "path": str(binary.resolve()),
                "sha256": sha(binary),
                "source_sha256": sha(source),
            }
            print(f"Built and typechecked {name}: {binaries[name]['sha256'][:16]}", flush=True)
    finally:
        if original is None:
            work.unlink(missing_ok=True)
        else:
            work.write_bytes(original)
    return binaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minutes", type=float, default=29)
    parser.add_argument("--cpu", type=int, default=3)
    args = parser.parse_args()
    if args.cpu not in os.sched_getaffinity(0):
        parser.error("chosen CPU is outside the process affinity")
    if args.minutes <= 0:
        parser.error("minutes must be positive")
    output = args.output.resolve()
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + args.minutes * 60
    with (ROOT / "gate.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binaries = prepare(raw)
        sources = [ROOT / "validator/scoring.py", ROOT / "validator/config.py", Path(__file__)]
        sources += sorted((ROOT / "lean/bench").glob("*Exact*.lean"))
        sources += sorted((ROOT / "lean/contract").rglob("*.lean"))
        manifest = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "git_status": subprocess.check_output(
                ["git", "status", "--short"], cwd=ROOT, text=True
            ),
            "host": platform.uname()._asdict(),
            "cpu": args.cpu,
            "cpu_info": subprocess.check_output(["lscpu"], text=True),
            "binaries": binaries,
            "budget_minutes": args.minutes,
            "sources": {str(p.relative_to(ROOT)): sha(p) for p in sources},
            "method": "Trusted frozen binaries, direct taskset execution; no sandbox overhead. "
            "Actual gate scoring and admission are replayed by the analysis script. "
            "Not a full gate/security test or production-machine calibration.",
        }
        save(raw / "manifest.json", manifest)
        # Capture the source behind the hashes, so later branch switches remain reproducible.
        for source in sources:
            target = raw / "sources" / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        events = (raw / "processes.jsonl").open("a", buffering=1)
        counter = 0

        def run(name, seed, inner, reps, session, phase, round_index=None, position=None):
            nonlocal counter
            counter += 1
            prefix = f"{counter:04d}"
            env = dict(
                os.environ,
                BENCH_SEED=str(seed),
                BENCH_INNER=str(inner),
                BENCH_REPS=",".join(f"{c}={n}" for c, n in reps.items()),
                LEAN_ABORT_ON_PANIC="1",
            )
            t0 = time.monotonic()
            load = os.getloadavg()
            process = subprocess.run(
                ["taskset", "-c", str(args.cpu), binaries[name]["path"]],
                env=env,
                text=True,
                capture_output=True,
                timeout=max(1, min(300, deadline - t0)),
            )
            (raw / f"{prefix}.stdout").write_text(process.stdout)
            (raw / f"{prefix}.stderr").write_text(process.stderr)
            cells = {}
            for line in process.stdout.splitlines():
                fields = line.split()
                if len(fields) != 4 or fields[1] != "true":
                    raise RuntimeError(f"unexpected bench output: {line}")
                cell, _, ns, checksum = fields
                if cell in cells or int(ns) <= 0:
                    raise RuntimeError(f"invalid/duplicate cell {cell}")
                cells[cell] = {"ns": int(ns), "checksum": int(checksum)}
            record = dict(
                id=counter,
                binary=name,
                session=session,
                phase=phase,
                seed=seed,
                inner=inner,
                reps=reps,
                round=round_index,
                position=position,
                seconds=time.monotonic() - t0,
                load_before=load,
                load_after=os.getloadavg(),
                cells=cells,
                returncode=process.returncode,
            )
            events.write(json.dumps(record) + "\n")
            if process.returncode != 0 or len(cells) != 30:
                raise RuntimeError(f"bench {prefix} failed: {process.stderr[-600:]}")
            return record

        plan = [
            ("self-42-a", "incumbent", "incumbent", 42, 9, 100, 3),
            ("self-fresh-a", "incumbent", "incumbent", 20261020, 9, 100, 3),
            ("slower-control", "incumbent", "slowdepthwise", 42, 7, 25, 2),
            ("faster-control", "slowdepthwise", "incumbent", 42, 7, 25, 2),
            ("self-42-b", "incumbent", "incumbent", 42, 9, 100, 3),
            ("self-fresh-b", "incumbent", "incumbent", 20261020, 9, 100, 3),
            ("self-default-25", "incumbent", "incumbent", 42, 25, 100, 3),
        ]
        save(raw / "plan.json", plan)
        calibrations = {}
        checksums = {}
        sessions = []
        try:
            for name, ref, candidate, seed, rounds, target_ms, inner in plan:
                if time.monotonic() > deadline - 30:
                    break
                key = (ref, seed, target_ms)
                if key not in calibrations:
                    cal = run(ref, seed, 1, {}, name, "calibration")
                    calibrations[key] = {
                        c: max(1, math.ceil(target_ms * 1e6 / row["ns"]))
                        for c, row in cal["cells"].items()
                    }
                reps = calibrations[key]
                # Warm both identities before collecting; a self comparison uses the same binary.
                for binary in dict.fromkeys((ref, candidate)):
                    warm = run(binary, seed, 1, {}, name, "warmup")
                    for cell, value in warm["cells"].items():
                        identity = (seed, cell)
                        old = checksums.setdefault(identity, value["checksum"])
                        if old != value["checksum"]:
                            raise RuntimeError(f"warmup checksum mismatch: {name}/{cell}")
                session = dict(
                    name=name,
                    reference=ref,
                    candidate=candidate,
                    seed=seed,
                    rounds=rounds,
                    target_ms=target_ms,
                    inner=inner,
                    completed_rounds=0,
                    complete=False,
                )
                sessions.append(session)
                print(f"Session {name}: {rounds} rounds, {target_ms}ms × inner {inner}", flush=True)
                begin = time.monotonic()
                for r in range(rounds):
                    # Swap the starting order in repeat sessions to separate session and order.
                    ref_first = (r + int(name.endswith("-b"))) % 2 == 0
                    identities = [("reference", ref), ("candidate", candidate)]
                    for position, (role, binary) in enumerate(
                        identities if ref_first else identities[::-1]
                    ):
                        result = run(binary, seed, inner, reps, name, role, r, position)
                        for cell, value in result["cells"].items():
                            if checksums[seed, cell] != value["checksum"]:
                                raise RuntimeError(f"timed checksum mismatch: {name}/{cell}")
                    session["completed_rounds"] += 1
                    print(
                        f"  round {r + 1}/{rounds}; elapsed {time.monotonic() - started:.0f}s",
                        flush=True,
                    )
                    save(raw / "sessions.json", sessions)
                    # Stop cleanly between pairs; partial sessions are excluded from decisions.
                    estimated_pair = (time.monotonic() - begin) / (r + 1)
                    if time.monotonic() + estimated_pair * 1.3 > deadline:
                        break
                session["seconds"] = time.monotonic() - begin
                session["complete"] = session["completed_rounds"] == rounds
                save(raw / "sessions.json", sessions)
                if not session["complete"]:
                    break
        finally:
            events.close()
            manifest["total_seconds"] = time.monotonic() - started
            save(raw / "manifest.json", manifest)
        print(f"Saved {len(sessions)} sessions to {output}", flush=True)


if __name__ == "__main__":
    main()
