#!/usr/bin/env python3
"""Replay real scoring/admission and plot the raw exact-runtime timing pilot."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/float-kernels-matplotlib")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "validator")]

from loguru import logger  # noqa: E402

from prototypes.single_seed_scoring import assess, measurement_hash  # noqa: E402


def ci(values, rng):
    """Round-bootstrap CI of the session median, conditional on one fixed input seed."""
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10000))
    bounds = []
    for probability in (0.025, 0.975):
        index = probability * (len(medians) - 1)
        lo, hi = math.floor(index), math.ceil(index)
        bounds.append(medians[lo] + (index - lo) * (medians[hi] - medians[lo]))
    return bounds


def analyse(directory):
    logger.remove()
    # The pilot explicitly evaluates the default guard, independent of the caller's shell.
    manifest = json.loads((directory / "raw/manifest.json").read_text())
    # Refuse to silently replay changed scoring code after a branch switch.
    import hashlib

    for name in ("validator/scoring.py", "validator/config.py"):
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != manifest["sources"][name]:
            raise RuntimeError(f"{name} changed since collection; use the recorded revision")
    sessions = json.loads((directory / "raw/sessions.json").read_text())
    events = [json.loads(s) for s in (directory / "raw/processes.jsonl").read_text().splitlines()]
    rng = random.Random(20261020)
    summaries = []
    cell_rows = []
    for s in sessions:
        if not s["complete"]:
            continue
        rows = [r for r in events if r["session"] == s["name"] and r["round"] is not None]
        paired = defaultdict(dict)
        for row in rows:
            paired[row["round"]][row["phase"]] = row
        if len(paired) != s["rounds"] or any(len(p) != 2 for p in paired.values()):
            raise RuntimeError(f"incomplete pairs in {s['name']}")
        inputs = {role: [] for role in ("reference", "candidate")}
        scores, orders = [], []
        for r, pair in sorted(paired.items()):
            ref, sub = pair["reference"], pair["candidate"]
            if len(ref["cells"]) != 30 or ref["cells"].keys() != sub["cells"].keys():
                raise RuntimeError("missing cells")
            if (
                ref["reps"] != sub["reps"]
                or ref["seed"] != sub["seed"]
                or ref["seed"] != s["seed"]
                or ref["inner"] != sub["inner"]
                or ref["inner"] != s["inner"]
                or ref["binary"] != s["reference"]
                or sub["binary"] != s["candidate"]
                or {ref["position"], sub["position"]} != {0, 1}
            ):
                raise RuntimeError("incompatible paired measurement settings")
            logs = []
            for cell in ref["cells"]:
                i, c = ref["cells"][cell], sub["cells"][cell]
                if i["checksum"] != c["checksum"]:
                    raise RuntimeError("checksum mismatch")
                ratio = c["ns"] / i["ns"]
                logs.append(math.log(ratio))
                cell_rows.append(
                    dict(
                        session=s["name"],
                        seed=s["seed"],
                        round=r,
                        cell=cell,
                        ratio=ratio,
                        reference_ns=i["ns"],
                        candidate_ns=c["ns"],
                        repetitions=ref["reps"][cell],
                        reference_first=ref["position"] == 0,
                    )
                )
            scores.append(math.exp(statistics.mean(logs)))
            orders.append(ref["position"] == 0)
            for role in inputs:
                inputs[role].append(
                    {c: (v["ns"], v["checksum"]) for c, v in pair[role]["cells"].items()}
                )
        cell_names = sorted(inputs["reference"][0])
        request = {
            "environment": "pilot",
            "identity": "a" * 64,
            "seed": s["seed"],
            "candidate_hash": "b" * 64,
            "reference_hash": "c" * 64,
            "cells": cell_names,
            "benchmark_identity": "d" * 64,
            "timing": {
                "rounds": s["rounds"],
                "inner": s["inner"],
                "cell_ms": s["target_ms"],
                "run_seconds": 300,
                "job_seconds": 900,
            },
        }
        plan = {
            "strata": [
                {"environment": "pilot", "identity": "a" * 64, "seed": s["seed"], "weight": 1}
            ],
            "cells": dict.fromkeys(cell_names, 1 / len(cell_names)),
            "timing": request["timing"],
            "benchmark_identity": "d" * 64,
            "admission": {
                "min_rounds": 5,
                "draws": 2000,
                "bar": 0.97,
                "guard": 1.1,
                "quantile": 0.05,
            },
        }
        result = {
            "request_hash": measurement_hash(request),
            "provenance": {"identity": "a" * 64, "machine": "pilot"},
            "repetitions": next(iter(paired.values()))["reference"]["reps"],
            "rounds": [
                {
                    "reference_first": orders[i],
                    "cells": {
                        c: {
                            "reference_ns": inputs["reference"][i][c][0],
                            "candidate_ns": inputs["candidate"][i][c][0],
                            "reference_checksum": inputs["reference"][i][c][1],
                            "candidate_checksum": inputs["candidate"][i][c][1],
                        }
                        for c in cell_names
                    },
                }
                for i in range(len(scores))
            ],
        }
        decision = assess(plan, [(request, result)])
        if not math.isclose(decision["score"], statistics.median(scores), rel_tol=1e-12):
            raise RuntimeError("independent score aggregation disagrees with scorer")
        guard = {
            "ok": decision["guard_passed"],
            "groups": decision["strata"][0]["guards"],
            "limit": 1.1,
        }
        error = None if decision["guard_passed"] else "regression guard failed"
        order_medians = {
            role: statistics.median([x for x, first in zip(scores, orders) if first == want])
            for role, want in (("reference_first", True), ("candidate_first", False))
        }
        summaries.append(
            dict(
                **s,
                score=decision["score"],
                ci95=ci(scores, rng),
                round_scores=scores,
                reference_first=orders,
                admission=decision,
                gain_p05=decision["gain_lower"],
                guard=guard,
                gate_error=error,
                would_promote=error is None and decision["outcome"] == "qualifies",
                order_medians=order_medians,
                order_gap_percent=100
                * (order_medians["candidate_first"] / order_medians["reference_first"] - 1),
                wall_per_pair_s=statistics.mean(
                    [p["reference"]["seconds"] + p["candidate"]["seconds"] for p in paired.values()]
                ),
            )
        )
    out = directory / "summaries"
    out.mkdir(exist_ok=True)
    if not summaries:
        raise RuntimeError("No complete sessions to analyse; collect with a larger time budget")
    source = Path(__file__).read_bytes()
    (out / "analysis-source.py").write_bytes(source)
    (out / "analysis-provenance.json").write_text(
        json.dumps(
            {
                "analysis_sha256": hashlib.sha256(source).hexdigest(),
                "ci_draws": 10000,
                "ci_seed": 20261020,
                "ci_probabilities": [0.025, 0.975],
                "promotion_bar": 0.97,
                "guard_limit": 1.10,
                "submission_id": 1,
                "admission_draws": 2000,
                "admission_quantile": 0.05,
            },
            indent=2,
        )
        + "\n"
    )
    (out / "sessions.json").write_text(json.dumps(summaries, indent=2) + "\n")
    with (out / "cells.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(cell_rows[0]))
        writer.writeheader()
        writer.writerows(cell_rows)
    with (out / "sessions.csv").open("w") as f:
        fields = [
            "name",
            "seed",
            "rounds",
            "target_ms",
            "inner",
            "score",
            "ci_low",
            "ci_high",
            "guard_worst",
            "would_promote",
            "order_gap_percent",
            "seconds",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for s in summaries:
            writer.writerow(
                {k: s[k] for k in fields if k in s}
                | dict(
                    ci_low=s["ci95"][0],
                    ci_high=s["ci95"][1],
                    guard_worst=max(s["guard"]["groups"].values()),
                )
            )
    return manifest, sessions, summaries, cell_rows, events


def plots(directory, sessions, cells, events):
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter

    output = directory / "visualizations"
    output.mkdir(exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titleweight": "bold",
            "figure.facecolor": "#FAFBFC",
            "axes.facecolor": "white",
            "savefig.facecolor": "#FAFBFC",
            "axes.edgecolor": "#CBD2D9",
            "grid.color": "#E5E9ED",
        }
    )
    colors = {42: "#237A8A", 20261020: "#C47A30"}
    self_runs = [s for s in sessions if s["reference"] == s["candidate"]]
    controls = [s for s in sessions if s["reference"] != s["candidate"]]

    def finish(fig, name):
        fig.savefig(output / f"{name}.png", dpi=170, bbox_inches="tight")
        fig.savefig(output / f"{name}.svg", bbox_inches="tight")
        plt.close(fig)

    def lines(ax):
        ax.axvline(1, color="#53616F", lw=1.2, ls="--", label="Equal speed")
        ax.axvline(0.97, color="#5C9B6F", lw=1.3, ls=":", label="Promotion bar")
        ax.grid(axis="x", alpha=0.7)

    fig, axes = plt.subplots(
        1, 2, figsize=(13, 5), constrained_layout=True, gridspec_kw={"width_ratios": [1.4, 1]}
    )
    for ax, group, title in zip(
        axes,
        [self_runs, controls],
        ["Identical code: noise controls", "Different proved implementations"],
    ):
        for y, s in enumerate(group):
            color = colors[s["seed"]] if s in self_runs else "#6C66A4"
            lo, hi = s["ci95"]
            ax.errorbar(
                s["score"],
                y,
                xerr=[[s["score"] - lo], [hi - s["score"]]],
                fmt="none",
                ecolor=color,
                lw=2,
                capsize=5,
                zorder=2,
            )
            ax.scatter(
                s["score"],
                y,
                marker="D",
                s=55,
                color=color,
                edgecolor="white",
                linewidth=0.7,
                zorder=4,
            )
            ax.annotate(
                f" {s['score']:.4f}×",
                (hi, y),
                xytext=(5, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
            )
        ax.set_yticks(range(len(group)), [s["name"] for s in group])
        ax.invert_yaxis()
        ax.set_ylim(len(group) - 0.5, -0.5)
        ax.set_title(title)
        ax.set_xlabel("Candidate / reference time · lower is faster")
        lines(ax)
    axes[1].margins(x=0.25)
    axes[0].legend(loc="lower left", fontsize=8)
    fig.suptitle(
        "Session scores and 95% round-bootstrap intervals\n"
        "Each interval uses repeated rounds of one fixed workload; seeds are not pooled",
        fontsize=13,
    )
    finish(fig, "01-session-scores")

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), sharey=True, constrained_layout=True)
    for ax, seed in zip(axes, (42, 20261020)):
        if not any(s["seed"] == seed for s in self_runs):
            ax.set_title(f"Seed {seed}: not collected")
            ax.set_axis_off()
            continue
        for index, s in enumerate(x for x in self_runs if x["seed"] == seed):
            ax.plot(
                np.arange(1, len(s["round_scores"]) + 1),
                s["round_scores"],
                marker="o",
                ms=4,
                lw=1.3,
                alpha=0.85,
                color=["#237A8A", "#C47A30", "#7967A3"][index],
                label=s["name"],
            )
        ax.axhline(1, color="#53616F", ls="--", lw=1)
        ax.axhline(0.97, color="#5C9B6F", ls=":", lw=1.3)
        ax.set_title(f"Seed {seed} · identical inputs across sessions")
        ax.set_xlabel("Paired round")
        ax.grid(alpha=0.6)
        ax.legend(fontsize=8)
    axes[0].set_ylabel("Round geometric-mean time ratio")
    fig.suptitle("Self-comparison round traces", fontsize=14)
    finish(fig, "02-self-round-traces")

    fig, ax = plt.subplots(figsize=(13, 5), constrained_layout=True)
    for i, s in enumerate(self_runs):
        for first, offset, color in ((True, -0.16, "#237A8A"), (False, 0.16, "#C47A30")):
            values = [
                v for v, order in zip(s["round_scores"], s["reference_first"]) if order == first
            ]
            jitter = np.linspace(-0.045, 0.045, len(values))
            ax.scatter(
                i + offset + jitter,
                values,
                s=25,
                color=color,
                alpha=0.65,
                label=("Reference first" if first else "Candidate first") if i == 0 else None,
            )
            ax.plot(
                [i + offset - 0.09, i + offset + 0.09],
                [statistics.median(values)] * 2,
                color=color,
                lw=3,
                zorder=4,
            )
    ax.axhline(1, color="#53616F", ls="--", lw=1)
    ax.axhline(0.97, color="#5C9B6F", ls=":", lw=1.3)
    ax.set_xticks(range(len(self_runs)), [s["name"] for s in self_runs], rotation=15)
    ax.set_ylabel("Round score · points are actual paired rounds")
    ax.set_title(
        "Execution order: dots are rounds, bars are medians\n"
        "Descriptive association; few rounds do not establish an order effect"
    )
    ax.legend(ncols=2)
    ax.grid(axis="y", alpha=0.6)
    finish(fig, "03-execution-order")

    names = sorted({c["cell"] for c in cells})
    matrix = np.array(
        [
            [
                statistics.median(
                    [c["ratio"] for c in cells if c["session"] == s["name"] and c["cell"] == name]
                )
                for name in names
            ]
            for s in self_runs
        ]
    )
    extent = max(0.03, float(np.max(np.abs(matrix - 1))))
    fig, ax = plt.subplots(figsize=(15, 5), constrained_layout=True)
    im = ax.imshow(
        100 * (matrix - 1),
        aspect="auto",
        cmap="RdBu_r",
        norm=TwoSlopeNorm(vmin=-100 * extent, vcenter=0, vmax=100 * extent),
    )
    ax.set_xticks(range(len(names)), names, rotation=65, ha="right", fontsize=8)
    ax.set_yticks(range(len(self_runs)), [s["name"] for s in self_runs])
    for edge in (5.5, 11.5, 17.5, 23.5):
        ax.axvline(edge, color="white", lw=2)
    fig.colorbar(im, ax=ax, label="Median cell ratio − 1 (%) · red = candidate slower")
    ax.set_title(
        "Where self-comparison noise occurs\n"
        "Each cell summarizes repeated measurements of exactly the same input"
    )
    finish(fig, "04-cell-noise")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    for ax, group, title in zip(axes, [self_runs, controls], ["Self comparisons", "Controls"]):
        for y, s in enumerate(group):
            values = list(s["guard"]["groups"].values())
            ax.scatter(values, [y] * len(values), color="#AEBBC5", s=20, alpha=0.8)
            ax.scatter(
                max(values),
                y,
                color="#B8554C" if max(values) > 1.1 else "#237A8A",
                marker="D",
                s=55,
                zorder=4,
            )
        ax.axvline(1.1, color="#B8554C", ls=":", label="Guard limit 1.10")
        ax.axvline(1, color="#53616F", ls="--")
        ax.set_yticks(range(len(group)), [s["name"] for s in group])
        ax.set_ylim(len(group) - 0.5, -0.5)
        ax.set_title(title)
        ax.set_xlabel("Gate group ratio: separate minima across rounds")
        ax.grid(axis="x", alpha=0.6)
    axes[1].set_xscale("log")
    axes[1].xaxis.set_major_locator(FixedLocator([0.125, 0.25, 0.5, 1, 2, 4, 8]))
    axes[1].xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    axes[1].xaxis.set_minor_formatter(NullFormatter())
    axes[0].legend(fontsize=8)
    fig.suptitle(
        "Regression guard · 15 model × distribution groups per session\n"
        "Diamonds mark the worst group; dots are groups, not repeated measurements",
        fontsize=13,
    )
    finish(fig, "05-regression-guard")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    for index, s in enumerate(self_runs):
        per_cell = [
            [
                c["reference_ns"] / 1e6
                for c in cells
                if c["session"] == s["name"] and c["cell"] == name
            ]
            for name in names
        ]
        axes[0].plot(
            range(len(names)),
            [statistics.median(v) for v in per_cell],
            "o-",
            ms=3,
            lw=1,
            alpha=0.75,
            label=s["name"],
        )
    axes[0].axhline(100, color="#53616F", ls=":", lw=1)
    axes[0].set_xticks(range(len(names)), names, rotation=75, ha="right", fontsize=7)
    axes[0].set_ylabel("Reference cell duration (ms), best of inner runs")
    axes[0].set_title("Calibration achieved per cell")
    axes[0].legend(fontsize=7)
    for i, s in enumerate(sessions):
        ev = [e for e in events if e["session"] == s["name"] and e["round"] is not None]
        timed = sum(sum(c["ns"] for c in e["cells"].values()) * e["inner"] / 1e9 for e in ev)
        # Best × inner is a lower bound, not an exact measure of inference total.
        axes[1].barh(i, s["seconds"], color="#CFD8DF")
        axes[1].barh(i, timed, color="#237A8A")
    axes[1].set_yticks(range(len(sessions)), [s["name"] for s in sessions])
    axes[1].invert_yaxis()
    axes[1].set_xlabel("Elapsed seconds")
    axes[1].set_title("Session wall time (grey)\nReported best times × inner (teal, lower bound)")
    axes[1].grid(axis="x", alpha=0.5)
    finish(fig, "06-calibration-and-runtime")

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), sharey=True, constrained_layout=True)
    self_names = {s["name"] for s in self_runs}
    for ax, seed in zip(axes, (42, 20261020)):
        selected = [
            e
            for e in events
            if e["session"] in self_names and e["seed"] == seed and e["round"] is not None
        ]
        if not selected:
            ax.set_title(f"Seed {seed}: not collected")
            ax.set_axis_off()
            continue
        base = {
            c: statistics.median([e["cells"][c]["ns"] / e["reps"][c] for e in selected])
            for c in names
        }
        factors = [
            math.exp(
                statistics.mean(
                    math.log(e["cells"][c]["ns"] / e["reps"][c] / base[c]) for c in names
                )
            )
            for e in selected
        ]
        for name in dict.fromkeys(e["session"] for e in selected):
            indices = [i for i, e in enumerate(selected) if e["session"] == name]
            ax.plot(
                [selected[i]["id"] for i in indices],
                [factors[i] for i in indices],
                color="#CBD2D9",
                lw=1,
                zorder=1,
            )
        for role, color in (("reference", "#237A8A"), ("candidate", "#C47A30")):
            indices = [i for i, e in enumerate(selected) if e["phase"] == role]
            ax.scatter(
                [selected[i]["id"] for i in indices],
                [factors[i] for i in indices],
                color=color,
                s=22,
                label=role.capitalize(),
                zorder=3,
            )
        ax.axhline(1, color="#53616F", ls="--", lw=1)
        ax.set_title(f"Seed {seed}")
        ax.set_xlabel("Process index in collection order")
        ax.grid(alpha=0.5)
        ax.legend(fontsize=8)
    axes[0].set_ylabel("Normalized inference duration · higher = slower")
    fig.suptitle(
        "Absolute timing drift despite identical code\n"
        "Each cell normalized by its same-seed median time per repetition, then geometric mean",
        fontsize=12,
    )
    finish(fig, "07-absolute-timing-drift")


def report(directory, manifest, planned, sessions):
    selfs = [s for s in sessions if s["reference"] == s["candidate"]]
    lines = [
        "# Exact-runtime timing pilot",
        "",
        f"Commit: `{manifest['commit']}`. CPU {manifest['cpu']} on "
        f"`{manifest['host']['node']}`. "
        f"Total pilot including preparation: {manifest['total_seconds'] / 60:.1f} min.",
        "",
        "## Results",
        "",
        "| Session | Seed | Rounds | Score | 95% interval | Worst guard | Admission | Guard |",
        "|---|---:|---:|---:|---|---:|---|---|",
    ]
    for s in sessions:
        lo, hi = s["ci95"]
        lines.append(
            f"| {s['name']} | {s['seed']} | {s['rounds']} | {s['score']:.4f} | "
            f"{lo:.4f}–{hi:.4f} | {max(s['guard']['groups'].values()):.3f} | "
            f"{s['admission']['outcome']} | {'pass' if s['guard']['ok'] else 'FAIL'} |"
        )
    lines += [
        "",
        f"Self comparisons: {sum(s['would_promote'] for s in selfs)}/{len(selfs)} "
        "would promote; "
        f"{sum(not s['guard']['ok'] for s in selfs)}/{len(selfs)} fail the guard.",
        "",
        "The controls test detection of a large real performance difference. They do not "
        "establish sensitivity to genuine improvements near the 3% threshold.",
    ]
    short = [s for s in selfs if s["rounds"] == 9]
    defaults = [s for s in selfs if s["rounds"] == 25]
    lines += ["", "## Findings and next step", ""]
    if short:
        lines += [
            f"- In the nine-round controls, {sum(s['would_promote'] for s in short)}/"
            f"{len(short)} would promote identical code. This is an observed failure, "
            "not an estimate of the long-run false-promotion rate."
        ]
    for s in defaults:
        lines += [
            f"- Default 25-round self comparison: {s['score']:.4f}×, "
            f"{'would promote' if s['would_promote'] else 'would not promote'}, "
            f"guard {'passed' if s['guard']['ok'] else 'failed'}. "
            f"Paired scoring took {s['seconds'] / 60:.2f} minutes, excluding build and "
            "proof checks. This does not certify an end-to-end gate time budget."
        ]
    if selfs:
        lines += [
            f"- Self-comparison session medians span {min(s['score'] for s in selfs):.4f}× "
            f"to {max(s['score'] for s in selfs):.4f}×. The round and drift plots show why "
            "a single score is insufficient here."
        ]
    lines += [
        "- Do not tune production thresholds from this run. Repeat the default settings on "
        "a quiet validator-class host, with both starting orders and more independent sessions. "
        "Then investigate warm calibration and more tightly interleaved timing if noise remains.",
        "",
        "## Method and interpretation",
        "",
        "- All 30 current cells, with identical inputs and repetition counts within a pair.",
        "- Raw cell observations are the benchmark's best-of-inner durations, not individual "
        "inner timings. Every process's original stdout/stderr is retained unchanged.",
        "- Starting order alternates; repeat sessions reverse their starting parity.",
        "- Sessions ending in `a` and `b` repeat the same seed and settings. The default "
        "session uses 25 rounds; other self sessions use nine. Both use 100 ms and inner=3.",
        "- Controls compare the current champion with the proved slowdepthwise fixture in "
        "both directions. The reverse is a genuine algorithm comparison against a weaker "
        "reference, not a newly discovered improvement over the current champion. Controls "
        "use seven rounds, 25 ms and inner=2 to keep the pilot bounded.",
        "- Scores and guards are replayed through scoring.assess; promotion through "
        "the existing admission implementation. Numeric aggregation is cross-checked.",
        "- Intervals bootstrap paired round scores within one fixed workload. They do not "
        "measure variation across seeds, sessions or machines, and assume exchangeable "
        "rounds; serial drift can make them optimistic.",
        "- Plotted intervals are two-sided 95% intervals. Admission uses a one-sided 95% "
        "gain bound with its own 2,000 deterministic resamples; the two are different tests. "
        "Admission replay uses submission ID 1, recorded in the analysis code.",
        "- Guard values use median paired ratios for each cell, as the scorer "
        "does. They are not the medians shown in the cell-noise heatmap.",
        "- Trusted binaries run directly, pinned to one logical CPU. Both fixtures "
        "typecheck VerifyExact. Full sandbox/kernel/security checks and build time are "
        "outside this timing experiment. CPU governor and system services were not changed.",
        "- A handful of controls cannot establish a false-promotion rate or justify changing "
        "thresholds. This laptop/local-host pilot must be repeated on the validator machine.",
        "",
        "## Files",
        "",
        "- `raw/`: frozen binaries and sources, build logs, manifest, plan, process JSONL, "
        "and verbatim stdout/stderr. JSONL contains every cell time/checksum, repetitions, "
        "seed, order, wall duration and host load.",
        "- `summaries/`: session JSON/CSV and one CSV row per paired cell measurement.",
        "- `visualizations/`: numbered PNG and SVG plots.",
    ]
    incomplete = [s["name"] for s in planned if not s["complete"]]
    requested = json.loads((directory / "raw/plan.json").read_text())
    skipped = [row[0] for row in requested if row[0] not in {s["name"] for s in planned}]
    if skipped:
        lines += ["", "Sessions not started within the budget: " + ", ".join(skipped)]
    if "microsoft" in manifest["host"]["release"].lower():
        lines += [
            "",
            "This host runs WSL2. Logical-CPU pinning does not isolate the physical core "
            "from Windows activity or its SMT sibling; host scheduling and frequency effects "
            "are not fully observable in the recorded Linux load averages.",
        ]
    if incomplete:
        lines += ["", "Partial sessions excluded from inference: " + ", ".join(incomplete)]
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n")
    images = sorted((directory / "visualizations").glob("*.png"))
    (directory / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Exact timing pilot</title>'
        "<style>body{max-width:1400px;margin:32px auto;font:16px sans-serif;background:#fafbfc}"
        "img{width:100%;margin:20px 0}a{color:#237a8a}</style>"
        '<h1>Exact-runtime timing pilot</h1><p><a href="REPORT.md">Report and methodology</a></p>'
        + "".join(
            f'<a href="visualizations/{p.stem}.svg"><img src="visualizations/{p.name}" '
            f'alt="{p.stem}"></a>'
            for p in images
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    manifest, planned, sessions, cells, events = analyse(args.directory)
    plots(args.directory, sessions, cells, events)
    report(args.directory, manifest, planned, sessions)
    print(f"Analysed {len(sessions)} complete sessions; plots in {args.directory}/visualizations")


if __name__ == "__main__":
    main()
