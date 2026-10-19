#!/usr/bin/env python3
"""Plot descriptive, paired workload-quality comparisons. Points are seed medians, not CIs."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/float-kernels-matplotlib")

ROOT = Path(__file__).resolve().parents[1]
MODELS = ["mlp", "cnn", "mnet", "nmlp", "mixer"]
KINDS = ["normal", "wide", "sparse"]


def relative(record, variant):
    base, delta = record["base"]["sum_squares"], record[variant]["delta_squares"]
    return math.sqrt(delta / base) if base else (0.0 if not delta else None)


def seed_values(records, variant):
    values = defaultdict(list)
    for row in records:
        if row["final"]:
            model, _, kind = row["cell"].split(":")
            value = relative(row, variant)
            if value is not None:
                values[model, kind, row["seed"]].append(value)
    return {key: statistics.median(samples) for key, samples in values.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--variant", choices=["redraw", "negate"], default="redraw")
    parser.add_argument("--root", type=Path, default=ROOT / "data/benchmark-quality")
    args = parser.parse_args()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    runs = [
        [
            json.loads(line)
            for line in (args.root / "raw" / (label + ".jsonl")).read_text().splitlines()
        ]
        for label in [args.baseline, args.candidate]
    ]
    keys = [{(r["seed"], r["cell"], r["sample"], r["layer"]) for r in rows} for rows in runs]
    if keys[0] != keys[1]:
        raise ValueError("Runs must use the same seeds, shapes and sample counts")
    values = [seed_values(run, args.variant) for run in runs]
    output = args.root / "visualizations" / (args.baseline + "-vs-" + args.candidate)
    output.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(
        5, 3, figsize=(12, 13), sharex=True, sharey="row", constrained_layout=True
    )
    for i, model in enumerate(MODELS):
        positive = [v for run in values for (m, _, _), v in run.items() if m == model and v > 0]
        floor = min(positive + [1e-6]) / 10
        for j, kind in enumerate(KINDS):
            ax = axes[i, j]
            seeds = sorted(seed for m, k, seed in values[0] if (m, k) == (model, kind))
            pairs = [(values[0][model, kind, seed], values[1][model, kind, seed]) for seed in seeds]
            for n, pair in enumerate(pairs):
                jitter = (n / max(1, len(pairs) - 1) - 0.5) * 0.18
                points = [max(v, floor) for v in pair]
                ax.plot([jitter, 1 + jitter], points, color="#999999", alpha=0.55, linewidth=0.8)
                for x, value, color in zip([jitter, 1 + jitter], pair, ["#687A89", "#288B65"]):
                    ax.scatter(
                        x,
                        max(value, floor),
                        marker="x" if value == 0 else "o",
                        color=color,
                        s=24,
                        zorder=3,
                    )
            ax.set_yscale("log")
            ax.set_xticks([0, 1], ["Baseline", "Refined"])
            ax.set_xlim(-0.3, 1.3)
            ax.set_title(f"{model} / {kind}")
            ax.grid(axis="y", alpha=0.2)
            if j == 0:
                ax.set_ylabel("Relative output L2 change")
    figure.get_layout_engine().set(rect=(0, 0, 1, 0.94))
    title = figure.suptitle(
        f"Input sensitivity: {args.variant}, fixed weights\n"
        "Each connected pair is one seed's median across batches/samples; × = exactly zero",
        fontsize=12,
        y=0.99,
    )
    title.set_in_layout(False)
    figure.savefig(output / ("sensitivity-" + args.variant + ".png"), dpi=160, bbox_inches="tight")
    plt.close(figure)

    figure, axes = plt.subplots(5, 3, figsize=(14, 12), constrained_layout=True)
    for i, model in enumerate(MODELS):
        for j, kind in enumerate(KINDS):
            ax = axes[i, j]
            for run, color, label in zip(runs, ["#687A89", "#288B65"], ["Baseline", "Refined"]):
                groups = defaultdict(list)
                for r in run:
                    family, _, distribution = r["cell"].split(":")
                    if (family, distribution) == (model, kind):
                        groups[r["layer"]].append(r["base"]["zeros"] / r["base"]["size"])
                indices = sorted(groups)
                ax.plot(
                    indices,
                    [statistics.median(groups[k]) for k in indices],
                    "o-",
                    color=color,
                    label=label,
                    markersize=3,
                )
            ax.set_title(f"{model} / {kind}")
            ax.set_ylim(-0.03, 1.03)
            ax.set_xlabel("Layer index")
            if j == 0:
                ax.set_ylabel("Median zero fraction")
            ax.grid(alpha=0.2)
    axes[0, 0].legend()
    figure.suptitle("Activation sparsity by layer — descriptive medians, not confidence intervals")
    figure.savefig(output / "activation-sparsity.png", dpi=160, bbox_inches="tight")
    plt.close(figure)
    print(output)


if __name__ == "__main__":
    main()
