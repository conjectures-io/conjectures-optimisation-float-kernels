"""Summarize saved optimization runs, preserving independent seeds and sessions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
from pathlib import Path
from statistics import median

from model_family_policy import confirm, revise

os.environ.setdefault("MPLCONFIGDIR", "/tmp/kernel-optimization-plots")
BOARDS = ("overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer")


def block_upper(scores, length, draws=2000):
    """Sensitivity check retaining adjacent rounds; not a replacement admission rule."""
    blocks = [scores[i : i + length] for i in range(0, len(scores), length)]
    rng = random.Random(440721 + length)
    values = sorted(
        median([v for block in rng.choices(blocks, k=len(blocks)) for v in block])
        for _ in range(draws)
    )
    return values[int(0.95 * (draws - 1))]


def counterbalance(scores):
    """Combine complete adjacent opposite-order rounds; an odd trailing round is unused."""
    return [math.sqrt(a * b) for a, b in zip(scores[::2], scores[1::2])]


def summarize(directories, output):
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    output.mkdir(parents=True, exist_ok=True)
    summaries = output / "summaries"
    visualizations = output / "visualizations"
    summaries.mkdir(exist_ok=True)
    visualizations.mkdir(exist_ok=True)
    rows, attempts, orders = [], [], []
    for directory in directories:
        manifest = json.loads((directory / "raw/manifest.json").read_text())
        decisions = json.loads((directory / "summaries/decisions.json").read_text())
        sessions = {
            (s["candidate"], s["reference"], s["seed"], s["repeat"]): s
            for p in (directory / "raw/sessions").glob("*/result.json")
            for s in [json.loads(p.read_text())]
        }
        revised = {}
        for decision in decisions:
            key = tuple(decision[k] for k in ("candidate", "reference", "seed", "repeat"))
            session = sessions[key]
            cells = [
                c
                for c in session["rounds"][0]["cells"]
                if decision["board"] == "overall" or c.startswith(decision["board"] + ":")
            ]
            weights = dict.fromkeys(cells, 1 / len(cells))
            row = revise(decision, session["rounds"], weights, decision["board"])
            row["dataset"] = directory.name
            row["original_outcome"] = decision["outcome"]
            round_scores = [
                math.exp(
                    sum(
                        math.log(r["cells"][c]["candidate_ns"] / r["cells"][c]["reference_ns"])
                        for c in cells
                    )
                    / len(cells)
                )
                for r in session["rounds"]
            ]
            row["block2_ratio_upper"] = block_upper(round_scores, 2)
            row["block4_ratio_upper"] = block_upper(round_scores, 4)
            row["block_sensitivity_passes"] = (
                max(row["block2_ratio_upper"], row["block4_ratio_upper"]) <= 0.97
            )
            # Adjacent rounds have opposite order. Their geometric mean cancels a
            # stable multiplicative second-position effect. Keep this explicitly
            # separate from current admission; never select whichever rule passes.
            paired_scores = counterbalance(round_scores)
            row["counterbalanced_rounds"] = 2 * len(paired_scores)
            row["counterbalanced_score"] = median(paired_scores)
            row["counterbalanced_ratio_upper"] = block_upper(paired_scores, 1)
            row["counterbalanced_qualifies"] = (
                row["guard_passed"]
                and row["counterbalanced_score"] <= 0.97
                and row["counterbalanced_ratio_upper"] <= 0.97
            )
            rows.append(row)
            revised[(*key, decision["board"])] = row
            by_order = []
            for first in (True, False):
                scores = [
                    math.exp(
                        sum(
                            math.log(r["cells"][c]["candidate_ns"] / r["cells"][c]["reference_ns"])
                            for c in cells
                        )
                        / len(cells)
                    )
                    for r in session["rounds"]
                    if r["reference_first"] == first
                ]
                by_order.append(median(scores))
            orders.append(
                {
                    k: row[k]
                    for k in ("dataset", "candidate", "reference", "seed", "repeat", "board")
                }
                | {
                    "reference_first": by_order[0],
                    "candidate_first": by_order[1],
                    "relative_gap": by_order[1] / by_order[0] - 1,
                }
            )
        seeds = manifest["settings"]["seeds"]
        for candidate, reference in manifest["pairs"]:
            for repeat in range(manifest["settings"]["repeats"]):
                for board in BOARDS:
                    first = revised.get((candidate, reference, seeds[0], repeat, board))
                    second = (
                        revised.get((candidate, reference, seeds[1], repeat, board))
                        if len(seeds) > 1
                        else None
                    )
                    if first is None:
                        continue
                    attempts.append(
                        dict(
                            dataset=directory.name,
                            candidate=candidate,
                            reference=reference,
                            repeat=repeat,
                            board=board,
                            outcome=confirm(
                                first,
                                second,
                                screening_seed=seeds[0],
                                confirmation_seed=seeds[1] if len(seeds) > 1 else None,
                            ),
                        )
                    )
                    for label, field in (
                        ("original", "original_outcome"),
                        ("counterbalanced", "counterbalanced_qualifies"),
                    ):

                        def adapt(row, field=field, label=label):
                            if row is None:
                                return None
                            outcome = (
                                row[field]
                                if label == "original"
                                else ("qualifies" if row[field] else "not-qualified")
                            )
                            return row | {"outcome": outcome}

                        attempts[-1][label + "_outcome"] = confirm(
                            adapt(first),
                            adapt(second),
                            screening_seed=seeds[0],
                            confirmation_seed=seeds[1] if len(seeds) > 1 else None,
                        )
    for name, data in (("decisions", rows), ("confirmation", attempts), ("order-effects", orders)):
        (summaries / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
        if data:
            flat = [
                {k: v for k, v in row.items() if not isinstance(v, (dict, list))} for row in data
            ]
            with (summaries / f"{name}.csv").open("w") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
                writer.writeheader()
                writer.writerows(flat)
    text = [
        "# Kernel optimization experiments",
        "",
        "Ratios are candidate/reference: lower is faster. Every point/session retains its "
        "own workload seed. No pooling of rounds across seeds; no best-repeat selection.",
        "",
        "Screening is exploratory. Confirmation requires both preassigned workload seeds "
        "to pass the full-gain confidence rule and the guard. Overall uses model-family "
        "guards; individual family boards use cell guards. These are research decisions, "
        "not changes to live admission.",
        "",
    ]
    for directory in directories:
        selected = [r for r in rows if r["dataset"] == directory.name]
        pairs = sorted({(r["candidate"], r["reference"]) for r in selected})
        text += [
            f"## {directory.name}",
            "",
            "| Candidate / reference | " + " | ".join(BOARDS) + " |",
            "|---|" + "---|" * len(BOARDS),
        ]
        for candidate, reference in pairs:
            values = []
            for board in BOARDS:
                matches = [
                    r
                    for r in selected
                    if (r["candidate"], r["reference"], r["board"]) == (candidate, reference, board)
                ]
                passes = [
                    a
                    for a in attempts
                    if (a["dataset"], a["candidate"], a["reference"], a["board"])
                    == (directory.name, candidate, reference, board)
                ]
                count = sum(a["outcome"] == "confirmed" for a in passes)
                values.append(
                    f"{median(r['score'] for r in matches):.3f}×; {count}/{len(passes)}"
                    if matches
                    else "—"
                )
            text.append(f"| {candidate} / {reference} | " + " | ".join(values) + " |")
        text += [
            "",
            "Table values: median of session point estimates; confirmed/attempts. "
            "The median is descriptive, not a pooled admission statistic.",
            "",
        ]
        fig, axes = plt.subplots(2, 3, figsize=(17, max(7, len(pairs) * 1.2)))
        seeds = sorted({r["seed"] for r in selected})
        colours = {seed: plt.get_cmap("tab10")(i) for i, seed in enumerate(seeds)}
        for board, ax in zip(BOARDS, axes.flat, strict=True):
            for i, (candidate, reference) in enumerate(pairs):
                matches = [
                    r
                    for r in selected
                    if (r["candidate"], r["reference"], r["board"]) == (candidate, reference, board)
                ]
                for j, row in enumerate(sorted(matches, key=lambda r: (r["seed"], r["repeat"]))):
                    y = i + (j - (len(matches) - 1) / 2) * 0.12
                    colour = colours[row["seed"]]
                    upper = row["ratio_upper_bound"]
                    ax.plot([row["score"], upper], [y, y], color=colour, alpha=0.5, zorder=1)
                    ax.plot([upper], [y], "|", color=colour, markersize=8, zorder=2)
                    marker = {
                        "qualifies": "o",
                        "above-bar": "x",
                        "guard-failed": "v",
                        "gain-not-confident": "s",
                    }[row["outcome"]]
                    ax.scatter(row["score"], y, color=colour, marker=marker, s=45, zorder=3)
            ax.axvline(1, color="grey", linestyle="--")
            ax.axvline(0.97, color="seagreen", linestyle=":")
            ax.set_yticks(range(len(pairs)), [f"{c} / {r}" for c, r in pairs], fontsize=8)
            ax.invert_yaxis()
            ax.set_title(board)
            ax.set_xlabel("Time ratio (candidate/reference)")
            ax.grid(axis="x", alpha=0.2)
        handles = [
            Line2D([], [], color=c, marker="o", linestyle="none", label=f"Seed {seed}")
            for seed, c in colours.items()
        ]
        handles += [
            Line2D([], [], color="black", marker=m, linestyle="none", label=label)
            for m, label in (
                ("o", "Passes screening"),
                ("x", "Above bar"),
                ("s", "Confidence fails"),
                ("v", "Guard fails"),
            )
        ]
        fig.legend(
            handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.96), ncol=4, frameon=False
        )
        fig.suptitle(
            f"{directory.name}: paired sessions; segment ends at one-sided 95% upper bound", y=0.995
        )
        fig.tight_layout(rect=(0, 0, 1, 0.86))
        for suffix in ("png", "svg"):
            fig.savefig(visualizations / f"{directory.name}.{suffix}", dpi=160, bbox_inches="tight")
        plt.close(fig)
        if directory.name == "validation":
            fig, axes = plt.subplots(2, 2, figsize=(15, 9))
            for row_index, board in enumerate(("resnet", "mobilenet")):
                relevant = [r for r in selected if r["board"] == board]
                low = min(min(r["score"], r["counterbalanced_score"]) for r in relevant)
                high = max(
                    max(r["ratio_upper_bound"], r["counterbalanced_ratio_upper"]) for r in relevant
                )
                for column, balanced in enumerate((False, True)):
                    ax = axes[row_index, column]
                    for i, pair in enumerate(pairs):
                        matches = sorted(
                            [r for r in relevant if (r["candidate"], r["reference"]) == pair],
                            key=lambda r: (r["seed"], r["repeat"]),
                        )
                        for j, r in enumerate(matches):
                            score = r["counterbalanced_score"] if balanced else r["score"]
                            upper = (
                                r["counterbalanced_ratio_upper"]
                                if balanced
                                else r["ratio_upper_bound"]
                            )
                            passes = (
                                r["counterbalanced_qualifies"]
                                if balanced
                                else r["outcome"] == "qualifies"
                            )
                            y = i + (j - (len(matches) - 1) / 2) * 0.12
                            colour = colours[r["seed"]]
                            ax.plot([score, upper], [y, y], color=colour, alpha=0.5, zorder=1)
                            ax.plot([upper], [y], "|", color=colour, markersize=8, zorder=2)
                            ax.scatter(
                                score, y, color=colour, marker="o" if passes else "x", zorder=3
                            )
                    ax.set_yticks(
                        range(len(pairs)), [f"{c} / {ref}" for c, ref in pairs], fontsize=8
                    )
                    ax.invert_yaxis()
                    ax.set_xlim(min(low * 0.98, 0.95), max(high * 1.02, 1.02))
                    ax.axvline(1, color="grey", linestyle="--")
                    ax.axvline(0.97, color="seagreen", linestyle=":")
                    ax.set_title(
                        board
                        + (": paired opposite-order rounds" if balanced else ": individual rounds")
                    )
                    ax.set_xlabel("Time ratio; endpoint = one-sided 95% upper bound")
                    ax.grid(axis="x", alpha=0.2)
            fig.suptitle("Same raw measurements: strict admission using two statistical units")
            fig.legend(
                handles=handles[: len(seeds)]
                + [
                    Line2D([], [], color="black", marker="o", linestyle="none", label="Passes"),
                    Line2D(
                        [], [], color="black", marker="x", linestyle="none", label="Does not pass"
                    ),
                ],
                loc="upper center",
                ncol=len(seeds) + 2,
                bbox_to_anchor=(0.5, 0.96),
                frameon=False,
            )
            fig.tight_layout(rect=(0, 0, 1, 0.91))
            for suffix in ("png", "svg"):
                fig.savefig(
                    visualizations / f"{directory.name}-order-balanced.{suffix}",
                    dpi=160,
                    bbox_inches="tight",
                )
            plt.close(fig)
        values = [
            [
                median(
                    r["score"]
                    for r in selected
                    if (r["candidate"], r["reference"], r["board"]) == (c, ref, board)
                )
                for board in BOARDS
            ]
            for c, ref in pairs
        ]
        fig, ax = plt.subplots(figsize=(12, max(3, 0.85 * len(pairs))))
        image = ax.imshow(
            [[math.log2(v) for v in line] for line in values],
            cmap="coolwarm",
            vmin=-0.5,
            vmax=0.5,
            aspect="auto",
        )
        for i, (candidate, reference) in enumerate(pairs):
            for j, board in enumerate(BOARDS):
                matches = [
                    a
                    for a in attempts
                    if (a["dataset"], a["candidate"], a["reference"], a["board"])
                    == (directory.name, candidate, reference, board)
                ]
                count = sum(a["outcome"] == "confirmed" for a in matches)
                text_label = (
                    f"{count}/{len(matches)} confirmed" if len(seeds) > 1 else "screen only"
                )
                ax.text(
                    j, i, f"{values[i][j]:.3f}×\n{text_label}", ha="center", va="center", fontsize=9
                )
        ax.set_xticks(range(len(BOARDS)), BOARDS)
        ax.set_yticks(range(len(pairs)), [f"{c} / {ref}" for c, ref in pairs])
        ax.set_title(
            f"{directory.name}: median session ratios (descriptive); "
            "blue = less time, red = more time"
        )
        fig.colorbar(image, ax=ax, label="log₂ time ratio")
        fig.tight_layout()
        for suffix in ("png", "svg"):
            fig.savefig(
                visualizations / f"{directory.name}-overview.{suffix}", dpi=170, bbox_inches="tight"
            )
        plt.close(fig)
    text += [
        "## Measurement limits",
        "",
        "Timing is sequential native execution, not the production sandbox; CPU affinity "
        "is recorded in each raw manifest and is not enforced on macOS. "
        "The inner best-of statistic can hide variability. Adjacent rounds can "
        "be correlated; the ordinary bootstrap assumes independent rounds. Selection "
        "over many candidates/families increases the chance of a lucky result. Use fresh "
        "seeds, repeated sessions and identical-binary controls before trusting marginal "
        "wins. See order-effects.csv for alternating-order diagnostics.",
        "",
    ]
    (output / "REPORT.md").write_text("\n".join(text))
    (output / "index.html").write_text(
        "<h1>Kernel optimization experiments</h1>"
        + "".join(
            f'<h2>{d.name}</h2><img width="100%" src="visualizations/{d.name}-overview.png">'
            f'<a href="visualizations/{d.name}.svg"><img width="100%" '
            f'src="visualizations/{d.name}.png"></a>'
            for d in directories
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summarize(args.directories, args.output)
