#!/usr/bin/env python3
"""Show independent sessions, paired rounds and order effects without mixing workload seeds."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from pathlib import Path
from statistics import median

os.environ.setdefault("MPLCONFIGDIR", "/tmp/tensor-family-matplotlib")


def plot(directory):
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

    rows = json.loads((directory / "summaries/decisions.json").read_text())
    if not rows:
        return
    shutil.copy2(__file__, directory / "summaries/details-plot-source.py")
    manifest = json.loads((directory / "raw/manifest.json").read_text())
    boards = ("overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer")
    groups = sorted({(r["candidate"], r["reference"]) for r in rows})
    seeds = sorted({r["seed"] for r in rows})
    colours = dict(zip(seeds, plt.get_cmap("tab10").colors, strict=False))
    output = directory / "visualizations"
    output.mkdir(exist_ok=True)

    def save(fig, name):
        fig.tight_layout()
        for suffix in ("png", "svg"):
            fig.savefig(output / f"{name}.{suffix}", dpi=170, bbox_inches="tight")
        plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(15, max(6, len(groups) * 1.1)), squeeze=False)
    for board, ax in zip(boards, axes.flat, strict=True):
        for i, group in enumerate(groups):
            matches = [
                r for r in rows if (r["candidate"], r["reference"]) == group and r["board"] == board
            ]
            for j, row in enumerate(sorted(matches, key=lambda r: (r["seed"], r["repeat"]))):
                y = i + (j - (len(matches) - 1) / 2) * 0.10
                ax.scatter(
                    row["score"],
                    y,
                    c=[colours[row["seed"]]],
                    marker="o" if row["outcome"] == "qualifies" else "x",
                    s=45,
                )
        ax.axvline(1, color="grey", linestyle="--", linewidth=1)
        ax.axvline(0.97, color="green", linestyle=":", linewidth=1)
        ax.set_xscale("log")
        ax.set_yticks(range(len(groups)), [f"{c} / {r}" for c, r in groups], fontsize=8)
        ax.invert_yaxis()
        ax.set_title(board)
        lo, hi = ax.get_xlim()
        ax.xaxis.set_major_locator(FixedLocator(np.geomspace(lo, hi, 4)))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.3f}×"))
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xlabel("Time ratio (candidate/reference)")
        ax.grid(axis="x", alpha=0.2)
    legend = [
        plt.Line2D([], [], color=colours[s], marker="o", linestyle="", label=f"Seed {s}")
        for s in seeds
    ]
    legend += [
        plt.Line2D([], [], color="black", marker=marker, linestyle="", label=label)
        for marker, label in (("o", "Qualifies"), ("x", "Does not qualify"))
    ]
    fig.legend(
        handles=legend, loc="upper center", ncol=min(4, len(legend)), bbox_to_anchor=(0.5, 1.02)
    )
    fig.suptitle(
        f"{manifest['stage']}: repeatability across sessions (seeds kept separate)", y=1.07
    )
    save(fig, "session-scores")

    sessions = [
        json.loads(p.read_text())
        for p in sorted((directory / "raw/sessions").glob("*/result.json"))
    ]
    fig, axes = plt.subplots(len(groups), 6, figsize=(21, max(3, len(groups) * 2.7)), squeeze=False)
    order_rows = []
    for i, group in enumerate(groups):
        for j, board in enumerate(boards):
            ax = axes[i, j]
            for session in sessions:
                if (session["candidate"], session["reference"]) != group:
                    continue
                scores, guards = [], []
                for round_ in session["rounds"]:
                    cells = [
                        o
                        for c, o in round_["cells"].items()
                        if board == "overall" or c.startswith(board + ":")
                    ]
                    ratios = [o["candidate_ns"] / o["reference_ns"] for o in cells]
                    scores.append(math.exp(sum(math.log(r) for r in ratios) / len(ratios)))
                    guards.append(max(ratios))
                label = f"{session['seed']}, repeat {session['repeat'] + 1}"
                ax.plot(
                    range(1, len(scores) + 1),
                    scores,
                    color=colours[session["seed"]],
                    linestyle="-" if session["repeat"] % 2 == 0 else "--",
                    alpha=0.75,
                    linewidth=1,
                    label=label,
                )
                first = median(
                    [
                        v
                        for r, v in zip(session["rounds"], scores, strict=True)
                        if not r["reference_first"]
                    ]
                )
                second = median(
                    [
                        v
                        for r, v in zip(session["rounds"], scores, strict=True)
                        if r["reference_first"]
                    ]
                )
                order_rows.append(
                    dict(
                        candidate=group[0],
                        reference=group[1],
                        board=board,
                        seed=session["seed"],
                        repeat=session["repeat"],
                        candidate_first=first,
                        reference_first=second,
                        order_gap_percent=100 * (first / second - 1),
                    )
                )
            ax.axhline(1, color="grey", linestyle="--", linewidth=0.8)
            ax.axhline(0.97, color="green", linestyle=":", linewidth=0.8)
            ax.set_title(f"{group[0]} / {group[1]}\n{board}", fontsize=8)
            ax.set_xlabel("Paired round", fontsize=8)
            ax.set_ylabel("Time ratio", fontsize=8)
            ax.grid(alpha=0.15)
    axes[0, -1].legend(fontsize=6)
    fig.suptitle("Round traces on fixed inputs; each line is a separate session", y=1.01)
    save(fig, "round-traces")
    (directory / "summaries/order-effects.json").write_text(json.dumps(order_rows, indent=2) + "\n")

    fig, axes = plt.subplots(1, 2, figsize=(16, max(4, len(groups) * 0.8)))
    for i, group in enumerate(groups):
        for j, board in enumerate(boards):
            matches = [
                r for r in rows if (r["candidate"], r["reference"]) == group and r["board"] == board
            ]
            offsets = np.linspace(-0.25, 0.25, len(matches))
            for offset, row in zip(offsets, matches, strict=True):
                axes[0].scatter(row["max_cell_ratio"], i + offset, color=f"C{j}", s=25)
            orders = [
                r
                for r in order_rows
                if (r["candidate"], r["reference"]) == group and r["board"] == board
            ]
            for offset, row in zip(offsets, orders, strict=True):
                axes[1].scatter(row["order_gap_percent"], i + offset, color=f"C{j}", s=25)
    axes[0].axvline(1.10, color="red", linestyle=":", label="Guard limit")
    axes[0].set_xscale("log")
    lo, hi = axes[0].get_xlim()
    axes[0].xaxis.set_major_locator(FixedLocator(np.geomspace(lo, hi, 4)))
    axes[0].xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.3f}×"))
    axes[0].xaxis.set_minor_locator(NullLocator())
    axes[0].set_xlabel("Largest per-cell median ratio in each board/session")
    axes[0].set_title("Guard bottlenecks")
    axes[1].axvline(0, color="grey", linestyle="--")
    axes[1].set_xlabel("Candidate-first / reference-first median ratio − 1 (%)")
    axes[1].set_title("Execution-order diagnostic (different paired rounds)")
    for ax in axes:
        ax.set_yticks(range(len(groups)), [f"{c} / {r}" for c, r in groups], fontsize=8)
        ax.invert_yaxis()
        ax.grid(axis="x", alpha=0.2)
    handles = [
        plt.Line2D([], [], color=f"C{j}", marker="o", linestyle="", label=b)
        for j, b in enumerate(boards)
    ]
    fig.legend(handles=handles, loc="upper center", ncol=6, bbox_to_anchor=(0.5, 1.04))
    save(fig, "guard-and-order")
    (directory / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Model-family experiment</title>"
        "<style>body{font:16px sans-serif;margin:2em}img{max-width:100%}</style>"
        "<h1>Model-family comparison</h1><p><a href='REPORT.md'>Report and limitations</a></p>"
        "<p>Dots represent sessions, not independent hardware samples. Colours identify workload "
        "seeds. Round traces separate repeated measurements of the same workload. No pooled "
        "confidence intervals are implied. Order differences are diagnostics, "
        "not causal effects.</p>"
        + "".join(
            f"<h2>{name}</h2><img src='visualizations/{name}.png'>"
            for name in ("family-comparison", "session-scores", "round-traces", "guard-and-order")
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    plot(parser.parse_args().directory.resolve())
