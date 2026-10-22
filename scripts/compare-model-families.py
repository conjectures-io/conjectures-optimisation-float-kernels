#!/usr/bin/env python3
"""Bounded, resumable comparisons of trusted exact-runtime packages; no DB or chain writes."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "validator")]

import fingerprint  # noqa: E402
from exact.config import BATCHES, KINDS, MODELS, measurement_hash  # noqa: E402
from exact.scoring import assess  # noqa: E402

LEAN_MODELS = MODELS
SCORING_PATH = ROOT / "validator/exact/scoring.py"
CONFIG_PATH = ROOT / "validator/exact/config.py"

STAGES = {
    "pilot": dict(rounds=5, inner=1, cell_ms=5, seeds=[42], repeats=1),
    "short": dict(rounds=15, inner=3, cell_ms=50, seeds=[42, 20261020], repeats=2),
    "full": dict(rounds=25, inner=3, cell_ms=100, seeds=[42, 20261020, 8675309], repeats=3),
}
PAIRS = [
    ("incumbent", "incumbent"),
    ("slowdepthwise", "incumbent"),
    ("incumbent", "slowdepthwise"),
    ("spec-norm", "incumbent"),
    ("spec-maxpool", "incumbent"),
    ("spec-dense", "incumbent"),
]
CELLS = [f"{m}:b{b}:{k}" for m in MODELS for b in BATCHES[MODELS[m]] for k in KINDS]


def machine_identity():
    if platform.system() != "Darwin":
        from exact.runner import machine_identity as linux_identity

        return linux_identity()
    details = {
        key: subprocess.check_output(["sysctl", "-n", key], text=True).strip()
        for key in ("machdep.cpu.brand_string", "hw.ncpu", "hw.memsize")
    }
    details.update(
        host=platform.uname()._asdict(),
        compiler=subprocess.check_output(["cc", "--version"], text=True),
        affinity="not enforced; native macOS research",
    )
    return sha(json.dumps(details, sort_keys=True).encode())


def benchmark_command(binary, cpu):
    # macOS has no taskset equivalent providing strict per-process CPU affinity.
    if platform.system() == "Darwin":
        return [str(binary)]
    return ["taskset", "-c", str(cpu), str(binary)]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def packages():
    incumbent = (ROOT / "kernels/exact/incumbent/Kernel.lean").read_text()
    variants = {
        "incumbent": incumbent,
        "slowdepthwise": (ROOT / "kernels/exact/slowdepthwise/Kernel.lean").read_text(),
        "baseline": (ROOT / "kernels/exact/baseline/Kernel.lean").read_text(),
    }
    for name, old, new in (
        ("spec-dense", "dense := ⟨linearWith dotFn, gemmWith dotFn⟩", "dense := specDense"),
        ("spec-norm", "norm := ⟨rmsnormWith dotFn, layernormWith dotFn⟩", "norm := specNorm"),
        (
            "spec-maxpool",
            "elem := ⟨avgpoolWith dotFn, maxpoolW,",
            "elem := ⟨avgpoolWith dotFn, maxpoolSpec,",
        ),
    ):
        if incumbent.count(old) != 1:
            raise ValueError(f"cannot construct {name}; incumbent changed")
        variants[name] = incumbent.replace(old, new)
    return {name: source.encode() for name, source in variants.items()}


def build(output, name, source, deadline, audit=False):
    directory = output / "raw/packages" / name
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "benchExact"
    identity = {
        "source": sha(source),
        "benchmark": fingerprint.current(),
        "audit": audit,
        "platform": [platform.system(), platform.machine()],
    }
    stamp = directory / "build.json"
    if binary.exists() and stamp.exists():
        previous = json.loads(stamp.read_text())
        if previous["identity"] == identity and previous["binary"] == sha(binary.read_bytes()):
            return binary
    (directory / "Kernel.lean").write_bytes(source)
    work = ROOT / "lean/work/Submission.lean"
    original = work.read_bytes() if work.exists() else None
    start = time.monotonic()
    try:
        work.write_bytes(source)
        with (directory / "build.log").open("w") as log:
            lake = str(Path.home() / ".elan/bin/lake")
            subprocess.run(
                [lake, "build", "VerifyExact", "benchExact", *(["exactAudit"] if audit else [])],
                cwd=ROOT / "lean",
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=max(1, min(600, deadline - start)),
                check=True,
            )
            if audit:
                for module in ("Submission", "VerifyExact"):
                    subprocess.run(
                        [lake, "env", "leanchecker", module],
                        cwd=ROOT / "lean",
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=max(1, min(300, deadline - time.monotonic())),
                    )
                for options in (["8", "971"], ["--malformed", "8", "972"]):
                    subprocess.run(
                        [str(ROOT / "lean/.lake/build/bin/exactAudit"), *options],
                        cwd=ROOT / "lean",
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=max(1, min(300, deadline - time.monotonic())),
                    )
        shutil.copy2(ROOT / "lean/.lake/build/bin/benchExact", binary)
        save(
            stamp,
            {
                "identity": identity,
                "binary": sha(binary.read_bytes()),
                "seconds": time.monotonic() - start,
                "proof": "VerifyExact typechecked",
            },
        )
        return binary
    finally:
        if original is None:
            work.unlink(missing_ok=True)
        else:
            work.write_bytes(original)


def execute(binary, seed, inner, reps, cpu, directory, index, deadline):
    env = dict(
        os.environ,
        BENCH_SEED=str(seed),
        BENCH_INNER=str(inner),
        BENCH_CELLS=",".join(
            f"{LEAN_MODELS.get(c.split(':')[0], c.split(':')[0])}:{c.split(':', 1)[1]}"
            for c in CELLS
        ),
        BENCH_REPS=",".join(
            f"{LEAN_MODELS.get(c.split(':')[0], c.split(':')[0])}:{c.split(':', 1)[1]}={n}"
            for c, n in reps.items()
        ),
        LEAN_ABORT_ON_PANIC="1",
    )
    start = time.monotonic()
    process = subprocess.run(
        benchmark_command(binary, cpu),
        env=env,
        capture_output=True,
        text=True,
        timeout=max(1, min(300, deadline - start)),
    )
    (directory / f"{index}.stdout").write_text(process.stdout)
    (directory / f"{index}.stderr").write_text(process.stderr)
    process.check_returncode()
    public = {v: k for k, v in LEAN_MODELS.items()}
    rows = {}
    for line in process.stdout.splitlines():
        cell, valid, ns, checksum = line.split()
        model, tail = cell.split(":", 1)
        cell = f"{public.get(model, model)}:{tail}"
        if valid != "true" or cell in rows or int(ns) <= 0:
            raise ValueError("invalid benchmark output")
        rows[cell] = dict(ns=int(ns), checksum=int(checksum))
    if set(rows) != set(CELLS):
        raise ValueError("missing benchmark cells")
    with (directory / "processes.jsonl").open("a") as log:
        log.write(
            json.dumps(
                dict(index=index, seconds=time.monotonic() - start, load=os.getloadavg(), rows=rows)
            )
            + "\n"
        )
    return rows


def collect(args):
    output = args.output.resolve()
    sources = packages()
    if args.source_dir:
        sources.update({p.stem: p.read_bytes() for p in args.source_dir.glob("*.lean")})
    existing = output / "raw/manifest.json"
    pairs = (
        [tuple(p.split(":")) for p in args.pair]
        if args.pair
        else json.loads(existing.read_text())["pairs"]
        if existing.exists()
        else PAIRS
    )
    if any(len(p) != 2 or any(n not in sources for n in p) for p in pairs):
        raise ValueError("pairs must be candidate:reference using known package names")
    settings = dict(STAGES[args.stage])
    if args.seeds is not None:
        settings["seeds"] = args.seeds
    if args.repeats is not None:
        settings["repeats"] = args.repeats
    for key in ("rounds", "inner", "cell_ms"):
        if getattr(args, key) is not None:
            settings[key] = getattr(args, key)
    native_mac = platform.system() == "Darwin"
    if not native_mac and args.cpu not in os.sched_getaffinity(0):
        raise ValueError("CPU is outside process affinity")
    if native_mac:
        print("Native macOS: CPU affinity is not enforced; --cpu is ignored.", flush=True)
    manifest = dict(
        stage=args.stage,
        settings=settings,
        pairs=pairs,
        cpu=None if native_mac else args.cpu,
        affinity="not enforced" if native_mac else "taskset",
        machine=machine_identity(),
        host=platform.uname()._asdict(),
        benchmark=fingerprint.current(),
        sources={k: sha(v) for k, v in sources.items()},
        scoring=sha(SCORING_PATH.read_bytes()),
        config=sha(CONFIG_PATH.read_bytes()),
        warmup=args.warmup,
        audit=args.audit,
        prebuild=args.prebuild,
    )
    manifest_path = output / "raw/manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text()) != json.loads(json.dumps(manifest)):
            raise ValueError("experiment inputs changed; choose a new output directory")
    else:
        save(manifest_path, manifest)
        for relative in [
            "validator/exact/scoring.py",
            "validator/exact/config.py",
            "scripts/compare-model-families.py",
            "lean/bench/ExactSuite.lean",
            "lean/bench/MainExact.lean",
            "lean/contract/ContractExact.lean",
        ]:
            target = output / "raw/code" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        (output / "raw/git.diff").write_bytes(
            subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)
        )
        (output / "raw/commit.txt").write_bytes(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
        )
    deadline = time.monotonic() + args.minutes * 60
    # Both historical collectors and production builders must be excluded from this checkout.
    with (
        (ROOT / "gate.lock").open("a") as gate_lock,
        (ROOT / "sandbox.lock").open("a") as sandbox_lock,
    ):
        for lock in (gate_lock, sandbox_lock):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prebuild:
            for name in sorted({n for pair in pairs for n in pair}):
                build(output, name, sources[name], deadline, args.audit)
        for repeat in range(settings["repeats"]):
            for seed in settings["seeds"]:
                # Reverse comparison order between sessions to expose temporal drift.
                ordered = (
                    pairs if (repeat + settings["seeds"].index(seed)) % 2 == 0 else pairs[::-1]
                )
                for candidate, reference in ordered:
                    if fingerprint.current() != manifest["benchmark"]:
                        raise ValueError("benchmark inputs changed during collection; stop the run")
                    session = (
                        output / "raw/sessions" / f"{candidate}--{reference}--{seed}--{repeat}"
                    )
                    if (session / "result.json").exists():
                        continue
                    if time.monotonic() + 5 > deadline:
                        return
                    session.mkdir(parents=True, exist_ok=True)
                    attempts = list(session.glob("attempt-*"))
                    attempt = session / f"attempt-{len(attempts) + 1}"
                    attempt.mkdir()
                    start = time.monotonic()
                    try:
                        binaries = {
                            n: build(output, n, sources[n], deadline, args.audit)
                            for n in {reference, candidate}
                        }
                        calibration = execute(
                            binaries[reference],
                            seed,
                            1,
                            {},
                            args.cpu,
                            attempt,
                            "calibration",
                            deadline,
                        )
                        reps = {
                            c: max(1, min(1000000, int(settings["cell_ms"] * 1e6 / row["ns"])))
                            for c, row in calibration.items()
                        }
                        if args.warmup:
                            for role in ("reference", "candidate"):
                                name = reference if role == "reference" else candidate
                                execute(
                                    binaries[name],
                                    seed,
                                    1,
                                    reps,
                                    args.cpu,
                                    attempt,
                                    f"warmup-{role}",
                                    deadline,
                                )
                        rounds = []
                        for r in range(settings["rounds"]):
                            roles = [("reference", reference), ("candidate", candidate)]
                            if r % 2:
                                roles.reverse()
                            observations = {
                                role: execute(
                                    binaries[name],
                                    seed,
                                    settings["inner"],
                                    reps,
                                    args.cpu,
                                    attempt,
                                    f"{r}-{role}",
                                    deadline,
                                )
                                for role, name in roles
                            }
                            cells = {
                                c: dict(
                                    reference_ns=observations["reference"][c]["ns"],
                                    candidate_ns=observations["candidate"][c]["ns"],
                                    reference_checksum=observations["reference"][c]["checksum"],
                                    candidate_checksum=observations["candidate"][c]["checksum"],
                                )
                                for c in CELLS
                            }
                            if any(
                                o["reference_checksum"] != o["candidate_checksum"]
                                for o in cells.values()
                            ):
                                raise ValueError("checksum mismatch")
                            rounds.append(dict(reference_first=r % 2 == 0, cells=cells))
                            save(attempt / "partial.json", dict(repetitions=reps, rounds=rounds))
                        save(
                            session / "result.json",
                            dict(
                                candidate=candidate,
                                reference=reference,
                                seed=seed,
                                repeat=repeat,
                                repetitions=reps,
                                rounds=rounds,
                                seconds=time.monotonic() - start,
                            ),
                        )
                        print(
                            f"Completed {session.name} in {time.monotonic() - start:.1f}s",
                            flush=True,
                        )
                    except (
                        subprocess.TimeoutExpired,
                        subprocess.CalledProcessError,
                        ValueError,
                    ) as exc:
                        save(
                            attempt / "failure.json",
                            dict(error=str(exc), at=datetime.now(timezone.utc).isoformat()),
                        )
                        print(f"Stopped {session.name}: {exc}", flush=True)
                        return


def runtime_estimates(output):
    """Extrapolate repetitions explicitly: slow cells have a one-call minimum."""
    estimates = []
    manifest = json.loads((output / "raw/manifest.json").read_text())
    for session_path in sorted((output / "raw/sessions").glob("*/result.json")):
        session = json.loads(session_path.read_text())
        logs = sorted(session_path.parent.glob("attempt-*/processes.jsonl"))
        events = [json.loads(line) for line in logs[-1].read_text().splitlines()]
        initial = next(event for event in events if event["index"] == "calibration")
        inner = manifest["settings"]["inner"]
        overhead = median(
            max(0, event["seconds"] - inner * sum(v["ns"] for v in event["rows"].values()) / 1e9)
            for event in events
            if event["index"] != "calibration"
        )
        per_call = {
            role: {
                cell: median(r["cells"][cell][f"{role}_ns"] for r in session["rounds"])
                / session["repetitions"][cell]
                for cell in CELLS
            }
            for role in ("reference", "candidate")
        }
        for stage, preset in STAGES.items():
            setting = manifest["settings"] if stage == manifest["stage"] else preset
            reps = {
                c: max(1, min(1000000, int(setting["cell_ms"] * 1e6 / initial["rows"][c]["ns"])))
                for c in CELLS
            }
            seconds = initial["seconds"] + setting["rounds"] * (
                2 * overhead
                + setting["inner"]
                * sum(per_call[role][cell] * reps[cell] for role in per_call for cell in CELLS)
                / 1e9
            )
            estimates.append(
                dict(
                    candidate=session["candidate"],
                    reference=session["reference"],
                    seed=session["seed"],
                    repeat=session["repeat"],
                    stage=stage,
                    session_minutes=seconds / 60,
                    all_sessions_minutes=seconds / 60 * len(setting["seeds"]) * setting["repeats"],
                )
            )
    return estimates


def summarize(output):
    manifest = json.loads((output / "raw/manifest.json").read_text())
    if manifest["scoring"] != sha(SCORING_PATH.read_bytes()) or manifest["config"] != sha(
        CONFIG_PATH.read_bytes()
    ):
        raise ValueError("scoring/config changed; replay with the saved code/revision")
    rows = []
    for path in sorted((output / "raw/sessions").glob("*/result.json")):
        session = json.loads(path.read_text())
        for board in ("overall", *MODELS):
            cells = CELLS if board == "overall" else [c for c in CELLS if c.startswith(board + ":")]
            request = dict(
                environment="local",
                identity=manifest["machine"],
                seed=session["seed"],
                candidate_hash=manifest["sources"][session["candidate"]],
                reference_hash=manifest["sources"][session["reference"]],
                cells=dict.fromkeys(cells, 1 / len(cells)),
                timing={k: manifest["settings"][k] for k in ("rounds", "inner", "cell_ms")},
                benchmark_identity=manifest["benchmark"],
                proof_identity=manifest["benchmark"],
            )
            result = dict(
                request_hash=measurement_hash(request),
                provenance=dict(identity=manifest["machine"], machine=manifest["host"]["node"]),
                repetitions={c: session["repetitions"][c] for c in cells},
                rounds=[
                    dict(
                        reference_first=r["reference_first"],
                        cells={c: r["cells"][c] for c in cells},
                    )
                    for r in session["rounds"]
                ],
            )
            plan = dict(
                cells=dict.fromkeys(cells, 1 / len(cells)),
                strata=[
                    dict(
                        environment="local",
                        seed=session["seed"],
                        identity=manifest["machine"],
                        weight=1,
                    )
                ],
                timing=request["timing"],
                benchmark_identity=manifest["benchmark"],
                proof_identity=manifest["benchmark"],
                admission=dict(bar=0.97, guard=1.10, draws=2000, quantile=0.05, min_rounds=5),
            )
            decision = assess(plan, [(request, result)])
            rows.append(
                dict(
                    candidate=session["candidate"],
                    reference=session["reference"],
                    seed=session["seed"],
                    repeat=session["repeat"],
                    board=board,
                    score=decision["score"],
                    gain_lower=decision["gain_lower"],
                    outcome=decision["outcome"],
                    max_cell_ratio=max(decision["strata"][0]["guards"].values()),
                )
            )
    save(output / "summaries/decisions.json", rows)
    shutil.copy2(__file__, output / "summaries/analysis-source.py")
    estimates = runtime_estimates(output)
    save(output / "summaries/runtime-estimates.json", estimates)
    if not rows:
        (output / "REPORT.md").write_text(
            "# Model-family comparison\n\nNo complete sessions yet. "
            "Inspect raw attempts; resume the collector.\n"
        )
        return
    with (output / "summaries/decisions.csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/tensor-family-matplotlib")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import TwoSlopeNorm

    groups = sorted(
        {tuple(pair) for pair in manifest.get("pairs", [])}
        | {(r["candidate"], r["reference"]) for r in rows}
    )
    boards = ("overall", *MODELS)
    values = np.array(
        [
            [
                median(values)
                if (
                    values := [
                        r["score"]
                        for r in rows
                        if (r["candidate"], r["reference"]) == group and r["board"] == b
                    ]
                )
                else math.nan
                for b in boards
            ]
            for group in groups
        ]
    )
    fig, ax = plt.subplots(figsize=(12, max(3.5, len(groups) * 0.65 + 2)))
    image = ax.imshow(
        np.log2(values),
        cmap="RdBu_r",
        norm=TwoSlopeNorm(
            vmin=min(-0.2, float(np.nanmin(np.log2(values)))),
            vcenter=0,
            vmax=max(0.2, float(np.nanmax(np.log2(values)))),
        ),
        aspect="auto",
    )
    for i, group in enumerate(groups):
        for j, board in enumerate(boards):
            matching = [
                r for r in rows if (r["candidate"], r["reference"]) == group and r["board"] == board
            ]
            wins = sum(r["outcome"] == "qualifies" for r in matching)
            decision_label = (
                matching[0]["outcome"] if len(matching) == 1 else f"{wins}/{len(matching)} qualify"
            )
            ax.text(
                j,
                i,
                f"{values[i, j]:.3f}×\n{decision_label}" if matching else "Not measured",
                ha="center",
                va="center",
                fontsize=9,
                bbox=dict(facecolor="white", alpha=0.8, edgecolor="none"),
            )
    ax.set_xticks(range(len(boards)), boards, rotation=20)
    ax.set_yticks(range(len(groups)), [f"{c} / {r}" for c, r in groups])
    ax.set_title(
        f"{manifest['stage'].upper()}: candidate / reference time by model family\n"
        + (
            "Short noisy measurements — admission is diagnostic only"
            if manifest["stage"] == "pilot"
            else "Lower is faster; median of completed session scores"
        )
    )
    fig.colorbar(image, ax=ax, label="log₂ time ratio (colour scale)")
    fig.tight_layout()
    visuals = output / "visualizations"
    visuals.mkdir(exist_ok=True)
    for suffix in ("png", "svg"):
        fig.savefig(visuals / f"family-comparison.{suffix}", dpi=170)
    plt.close(fig)
    text = [
        "# Model-family comparison",
        "",
        f"Stage: **{manifest['stage']}**. Complete sessions: **{len(rows) // 6}**.",
        "",
    ]
    false_wins = [
        r for r in rows if r["candidate"] == r["reference"] and r["outcome"] == "qualifies"
    ]
    if false_wins:
        text += [
            f"**Noise warning: the identical-binary control qualified in {len(false_wins)} "
            "board/session decisions. These measurement settings cannot support admission "
            "or marginal-speedup conclusions. Repeat self-controls with longer calibrated "
            "cells and more rounds before interpreting smaller differences.**",
            "",
        ]
    false_guards = [
        r for r in rows if r["candidate"] == r["reference"] and r["outcome"] == "guard-failed"
    ]
    if false_guards:
        text += [
            f"**Guard warning: identical binaries exceeded the per-cell regression guard in "
            f"{len(false_guards)} board/session decisions. Guard outcomes remain sensitive to "
            "measurement noise at these settings. These decisions overlap across boards and "
            "are not independent failure events.**",
            "",
        ]
    text += [
        "Ratios are candidate/reference; lower is faster. Decisions replay the current scoring "
        "code separately for each board. Overall and family outcomes can differ because guards "
        "and cells differ.",
        "",
        "| Candidate / reference | Board | Median ratio | Qualifying sessions |",
        "|---|---|---:|---:|",
    ]
    for group in groups:
        for board in boards:
            matching = [
                r for r in rows if (r["candidate"], r["reference"]) == group and r["board"] == board
            ]
            if not matching:
                text.append(f"| {' / '.join(group)} | {board} | — | Not measured |")
                continue
            text.append(
                f"| {' / '.join(group)} | {board} | "
                f"{median(r['score'] for r in matching):.3f}× | "
                f"{sum(r['outcome'] == 'qualifies' for r in matching)}/{len(matching)} |"
            )
    text += [
        "",
        "## Projected collection time from these measurements",
        "",
        "Estimates exclude new builds, use observed per-call costs and explicit repetition floors, "
        "and assume other seeds cost similarly. They are planning estimates, not time guarantees.",
        "",
        "| Candidate / reference | Stage | One session, min | All planned seeds/repeats, min |",
        "|---|---|---:|---:|",
    ]
    estimate_groups = sorted({(e["candidate"], e["reference"], e["stage"]) for e in estimates})
    for candidate, reference, stage in estimate_groups:
        selected = [
            e
            for e in estimates
            if (e["candidate"], e["reference"], e["stage"]) == (candidate, reference, stage)
        ]
        text.append(
            f"| {candidate} / {reference} | {stage} | "
            f"{median(e['session_minutes'] for e in selected):.1f} | "
            f"{median(e['all_sessions_minutes'] for e in selected):.1f} |"
        )
    text += ["", "## Incomplete comparisons", ""]
    failures = list((output / "raw/sessions").glob("*/attempt-*/failure.json"))
    if failures:
        for failure in failures:
            detail = json.loads(failure.read_text())["error"]
            text.append(f"- `{failure.parent.parent.name}`: {detail}")
    else:
        expected = (
            len(manifest.get("pairs", []))
            * len(manifest["settings"]["seeds"])
            * manifest["settings"]["repeats"]
        )
        text.append(
            "All planned sessions completed."
            if len(rows) // 6 == expected
            else "No recorded failures; unmeasured comparisons remain queued in the plan."
        )
    text += [
        "",
        "## Limits",
        "",
        "These are trusted, typechecked research controls executed directly with CPU pinning, "
        "not a full sandbox/security audit. No database, reward or chain state is changed. "
        "Thirty cells share each timing session; actual separate-board scheduling may change "
        "cache/thermal behaviour. Admission is replayed against the named reference, never "
        "inferred from two unrelated baseline ratios. The controls deliberately replace optimized "
        "components with their specifications; they are not new miner discoveries. Pilot results "
        "have five rounds, one seed and short cells: use them to validate collection, not set "
        "payouts or claim marginal gains. Repeats on one seed measure setup noise; different seeds "
        "measure workload variation. Qualifying counts are not estimates of a rare false-promotion "
        "rate. Missing/partial sessions are excluded and remain in raw attempts. No cross-session "
        "confidence interval is drawn from this small sample.",
        "",
        "See `summaries/decisions.csv` for per-session admission and guard outcomes, `raw/` for "
        "sources, binaries, proof-build logs, raw process output, calibration and paired "
        "observations. The heatmap is in `visualizations/`. Resume with the same collection "
        "command; completed sessions are skipped.",
    ]
    (output / "REPORT.md").write_text("\n".join(text) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, default="pilot")
    parser.add_argument("--minutes", type=float, default=6)
    parser.add_argument("--cpu", type=int, default=3)
    parser.add_argument("--seeds", type=int, nargs="+", help="override the stage's workload seeds")
    parser.add_argument("--repeats", type=int, help="override sessions per seed and comparison")
    parser.add_argument(
        "--pair",
        action="append",
        help="candidate:reference; default is the six comparison controls",
    )
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--inner", type=int)
    parser.add_argument("--cell-ms", type=float)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--audit", action="store_true", help="build and run differential audits")
    parser.add_argument("--prebuild", action="store_true", help="finish all builds before timing")
    args = parser.parse_args()
    if not math.isfinite(args.minutes) or args.minutes <= 0:
        parser.error("minutes must be finite and positive")
    if args.repeats is not None and args.repeats < 1:
        parser.error("repeats must be positive")
    if args.seeds is not None and (
        len(set(args.seeds)) != len(args.seeds) or any(not 0 <= seed < 2**32 for seed in args.seeds)
    ):
        parser.error("seeds must be unique unsigned 32-bit integers")
    for key in ("rounds", "inner", "cell_ms"):
        value = getattr(args, key)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{key} must be positive and finite")
    if not args.summarize_only:
        collect(args)
    summarize(args.output.resolve())


if __name__ == "__main__":
    main()
