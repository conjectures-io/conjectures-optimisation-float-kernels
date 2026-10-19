#!/usr/bin/env python3
"""Collect untimed paired-input diagnostics and summarize workload quality by model/data type."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEEDS = [0, 1, 2**31, 2**32 - 1, 42, 20261019, 20261020, 3735928559]


def relative_change(record, variant):
    base = record["base"]["sum_squares"]
    delta = record[variant]["delta_squares"]
    return math.sqrt(delta / base) if base else (0.0 if not delta else None)


def summarize(records):
    groups = defaultdict(list)
    for record in records:
        family, _, kind = record["cell"].split(":")
        groups[family, kind].append(record)
    rows = []
    for (family, kind), group in sorted(groups.items()):
        final = [r for r in group if r["final"]]
        for variant in ["redraw", "negate"]:
            relative = [relative_change(r, variant) for r in final]
            finite = [x for x in relative if x is not None]
            rows.append(
                dict(
                    model=family,
                    kind=kind,
                    perturbation=variant,
                    layer_records=len(group),
                    final_records=len(final),
                    all_zero_layers=sum(r["base"]["zeros"] == r["base"]["size"] for r in group),
                    unchanged_final=sum(r[variant]["changed"] == 0 for r in final),
                    median_changed_fraction=statistics.median(
                        r[variant]["changed"] / r["base"]["size"] for r in final
                    ),
                    median_relative_change=statistics.median(finite) if finite else None,
                    min_relative_change=min(finite) if finite else None,
                    relative_change_below_001=sum(x is not None and x < 1e-3 for x in relative),
                    zero_base_nonzero_delta=sum(x is None for x in relative),
                )
            )
    return rows


def validate_records(records, seeds):
    """Fail on incomplete output, duplicate layers or malformed numerical diagnostics."""
    if not records:
        raise ValueError("No workload records")
    seen, finals, cells = set(), defaultdict(int), set()
    layers_by_sample = defaultdict(list)
    for r in records:
        key = r["seed"], r["cell"], r["sample"], r["layer"]
        if key in seen:
            raise ValueError(f"Duplicate record: {key}")
        seen.add(key)
        layers_by_sample[key[:3]].append(r)
        cells.add((r["seed"], r["cell"]))
        size = r["base"]["size"]
        if size <= 0 or r["base"]["nonfinite"]:
            raise ValueError(f"Invalid base activation: {key}")
        for stats in [r["base"], *[r[v]["activation"] for v in ["redraw", "negate"]]]:
            if any(not math.isfinite(stats[k]) for k in ["sum", "sum_squares", "max_abs"]):
                raise ValueError(f"Invalid numerical diagnostics: {key}")
        for variant in ["redraw", "negate"]:
            stats = r[variant]
            if (
                stats["activation"]["size"] != size
                or stats["activation"]["nonfinite"]
                or not 0 <= stats["changed"] <= size
                or stats["delta_squares"] < 0
            ):
                raise ValueError(f"Invalid paired activation: {key}")
        if r["final"]:
            finals[key[:3]] += 1
    if {r["seed"] for r in records} != set(seeds) or len(cells) != len(seeds) * 30:
        raise ValueError("Missing seed/cell output")
    for seed, cell in cells:
        family, batch, _ = cell.split(":")
        count = 1 if family in {"mlp", "nmlp"} else int(batch[1:])
        for sample in range(count):
            if finals[seed, cell, sample] != 1:
                raise ValueError(f"Missing or repeated final layer: {seed}:{cell}:{sample}")
            layers = layers_by_sample[seed, cell, sample]
            last = next(r["layer"] for r in layers if r["final"])
            if {r["layer"] for r in layers} != set(range(last + 1)):
                raise ValueError(f"Missing intermediate layer: {seed}:{cell}:{sample}")


def quality_issues(records, minimum):
    """A configurable developer regression screen, not a kernel/admission correctness rule."""
    issues = []
    for r in records:
        if not r["final"]:
            continue
        for variant in ["redraw", "negate"]:
            change = relative_change(r, variant)
            if r[variant]["changed"] == 0 or (change is not None and change < minimum):
                issues.append(
                    dict(
                        seed=r["seed"],
                        cell=r["cell"],
                        sample=r["sample"],
                        perturbation=variant,
                        relative_change=change,
                    )
                )
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--binary", type=Path, default=ROOT / "lean/.lake/build/bin/auditExactWorkloads"
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS)
    parser.add_argument(
        "--suite-source",
        type=Path,
        default=ROOT / "lean/bench/ExactSuite.lean",
        help="Source snapshot used to build the selected binary",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "data/benchmark-quality")
    parser.add_argument(
        "--check-quality",
        action="store_true",
        help="Fail on unchanged or very weak final output responses",
    )
    parser.add_argument(
        "--min-relative-change",
        type=float,
        default=1e-3,
        help="Developer screen threshold, not a statistical/admission threshold",
    )
    args = parser.parse_args()
    if not args.label.replace("-", "").replace("_", "").isalnum():
        parser.error("label must contain letters, digits, hyphens or underscores")
    if len(set(args.seeds)) != len(args.seeds) or any(not 0 <= s < 2**32 for s in args.seeds):
        parser.error("seeds must be distinct UInt32 values")
    if not math.isfinite(args.min_relative_change) or args.min_relative_change < 0:
        parser.error("minimum relative change must be finite and nonnegative")
    raw = args.output / "raw" / (args.label + ".jsonl")
    raw.parent.mkdir(parents=True, exist_ok=True)
    if raw.exists():
        parser.error(f"Refusing to overwrite {raw}; use a new label")
    started = time.monotonic()
    binary = args.binary.resolve()
    with raw.open("w") as stream:
        run = subprocess.run(
            [str(binary), *map(str, args.seeds)],
            stdout=stream,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1200,
        )
    if run.returncode or run.stderr.strip():
        raise RuntimeError(f"Workload audit failed ({run.returncode}): {run.stderr}")
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    validate_records(records, args.seeds)
    rows = summarize(records)
    issues = quality_issues(records, args.min_relative_change)
    summary_dir = args.output / "summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    with (summary_dir / (args.label + ".csv")).open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    metadata = dict(
        label=args.label,
        seeds=args.seeds,
        seconds=time.monotonic() - started,
        platform=platform.platform(),
        binary=str(binary),
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        source_sha256={
            str(p.resolve().relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [args.suite_source, ROOT / "lean/audit/WorkloadAudit.lean"]
        },
        records=len(records),
        groups=rows,
        quality_screen=dict(min_relative_change=args.min_relative_change, issues=issues),
    )
    (summary_dir / (args.label + ".json")).write_text(json.dumps(metadata, indent=2) + "\n")
    for row in rows:
        if row["perturbation"] == "redraw":
            print(
                f"{row['model']:6} {row['kind']:6} "
                f"zero layers={row['all_zero_layers']:3} "
                f"unchanged final={row['unchanged_final']:3}/{row['final_records']} "
                f"median relative change={row['median_relative_change']}"
            )
    print(f"Saved {len(records)} layer records in {time.monotonic() - started:.1f}s to {raw}")
    if args.check_quality and issues:
        raise SystemExit(f"Workload quality screen failed for {len(issues)} final perturbations")


if __name__ == "__main__":
    main()
