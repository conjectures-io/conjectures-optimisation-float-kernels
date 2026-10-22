#!/usr/bin/env python3
"""Compare admission policies on archived measurements. Never builds or executes a benchmark."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
import types
from pathlib import Path
from statistics import median

from model_family_policy import POLICY, confirm, revise

ROOT = Path(__file__).resolve().parents[1]
BOARDS = ("overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer")
RULES = ("original", "confident-gain", "family-guard")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def archived_scoring(directory, manifest):
    """Load the hash-checked recorded code in its own package, regardless of current branch."""
    sources = directory / "raw/code/validator"
    if not (sources / "config.py").exists():
        sources = sources / "exact"
    package = "_family_archive_" + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    module = types.ModuleType(package)
    module.__path__ = [str(sources)]
    sys.modules[package] = module
    for name in ("config", "scoring"):
        path = sources / f"{name}.py"
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest[name]:
            raise ValueError(f"archived {name} does not match manifest")
        spec = importlib.util.spec_from_file_location(f"{package}.{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        tree = ast.parse(content, filename=str(path))
        # Recorded refactor modules used a top-level config import. Isolate it locally.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "config" and node.level == 0:
                node.level = 1
        exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


def replay(directory):
    manifest = json.loads((directory / "raw/manifest.json").read_text())
    scorer = archived_scoring(directory, manifest)
    previous = {
        (r["candidate"], r["reference"], r["seed"], r["repeat"], r["board"]): r
        for r in json.loads((directory / "summaries/decisions.json").read_text())
    }
    legacy = not hasattr(sys.modules[scorer.__package__ + ".config"], "MeasurementRequest")
    rows = []
    for path in sorted((directory / "raw/sessions").glob("*/result.json")):
        session = json.loads(path.read_text())
        for board in BOARDS:
            cells = [
                c
                for c in session["rounds"][0]["cells"]
                if board == "overall" or c.startswith(board + ":")
            ]
            request = dict(
                environment="local",
                identity=manifest["machine"],
                seed=session["seed"],
                candidate_hash=manifest["sources"][session["candidate"]],
                reference_hash=manifest["sources"][session["reference"]],
                cells=cells,
                timing={k: manifest["settings"][k] for k in ("rounds", "inner", "cell_ms")},
                benchmark_identity=manifest["benchmark"],
            )
            # Reconstruct exactly the ordering used in the original collector's projection.
            batches = {m: (1, 8) if m in ("mlp", "normalized_mlp") else (1, 4) for m in BOARDS[1:]}
            cells = [
                f"{m}:b{b}:{kind}"
                for m in BOARDS[1:]
                for b in batches[m]
                for kind in ("normal", "wide", "sparse")
                if board == "overall" or m == board
            ]
            request["cells"] = dict.fromkeys(cells, 1 / len(cells)) if legacy else cells
            if legacy:
                request["proof_identity"] = manifest["benchmark"]
            result = dict(
                request_hash=scorer.measurement_hash(request),
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
                admission=dict(bar=0.97, guard=1.1, draws=2000, quantile=0.05, min_rounds=5),
            )
            if legacy:
                plan["proof_identity"] = manifest["benchmark"]
            old = scorer.assess(plan, [(request, result)])
            key = (
                session["candidate"],
                session["reference"],
                session["seed"],
                session["repeat"],
                board,
            )
            recorded = previous[key]
            if old["outcome"] != recorded["outcome"] or any(
                not math.isclose(old[k], recorded[k], rel_tol=0, abs_tol=1e-12)
                for k in ("score", "gain_lower")
            ):
                raise ValueError(f"original decision did not reproduce: {key}")
            strict = revise(old, result["rounds"], plan["cells"], board, family_guard=False)
            proposed = revise(old, result["rounds"], plan["cells"], board)
            base = dict(
                dataset=directory.name,
                candidate=key[0],
                reference=key[1],
                seed=key[2],
                repeat=key[3],
                board=board,
                score=old["score"],
                gain_lower=old["gain_lower"],
                ratio_upper_bound=1 - old["gain_lower"],
            )
            for rule, decision in zip(RULES, (old, strict, proposed), strict=True):
                guards = decision.get("policy_guards", old["strata"][0]["guards"])
                rows.append(
                    base
                    | dict(
                        rule=rule, outcome=decision["outcome"], max_guard_ratio=max(guards.values())
                    )
                )
    return manifest, rows


def confirmation_pairs(manifests, rows):
    out = []
    for name, manifest in manifests.items():
        seeds = manifest["settings"]["seeds"]
        if len(seeds) > 2:
            raise ValueError("declare a screening/confirmation schedule before replaying >2 seeds")
        first, second = seeds[0], seeds[1] if len(seeds) == 2 else None
        selected = [r for r in rows if r["dataset"] == name and r["rule"] == "family-guard"]
        lookup = {
            (r["candidate"], r["reference"], r["repeat"], r["board"], r["seed"]): r
            for r in selected
        }
        for row in selected:
            if row["seed"] != first:
                continue
            key = (row["candidate"], row["reference"], row["repeat"], row["board"])
            other = lookup.get((*key, second))
            state = confirm(row, other, screening_seed=first, confirmation_seed=second)
            out.append(
                dict(
                    dataset=name,
                    candidate=key[0],
                    reference=key[1],
                    repeat=key[2],
                    board=key[3],
                    screening_seed=first,
                    confirmation_seed=second,
                    screening_outcome=row["outcome"],
                    confirmation_outcome=other["outcome"] if other else None,
                    outcome=state,
                    screening_score=row["score"],
                    confirmation_score=other["score"] if other else None,
                )
            )
    return out


def write_csv(path, rows):
    if rows:
        with path.open("w") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def screening_plots(output, rows, groups, names):
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

    markers = {
        "qualifies": "o",
        "guard-failed": "v",
        "above-bar": "x",
        "not-significant": "s",
        "gain-not-confident": "s",
    }
    labels = {
        "o": "Passes screening",
        "v": "Guard failed",
        "x": "Score above 0.97",
        "s": "Confidence insufficient",
    }
    selected_rows = [r for r in rows if r["dataset"] in names]
    seeds = sorted({r["seed"] for r in selected_rows})
    colours = dict(zip(seeds, plt.get_cmap("tab10").colors, strict=False))
    limits = {}
    for board in BOARDS:
        scores = [r["score"] for r in selected_rows if r["board"] == board]
        scores += [
            r["ratio_upper_bound"]
            for r in selected_rows
            if r["board"] == board and r["score"] <= 0.97
        ]
        limits[board] = (min(min(scores), 0.97) / 1.03, max(max(scores), 1) * 1.03)
    for rule in ("original", "family-guard"):
        fig, axes = plt.subplots(2, 3, figsize=(18, 10), squeeze=False)
        for ax, board in zip(axes.flat, BOARDS, strict=True):
            for i, group in enumerate(groups):
                matches = sorted(
                    [
                        r
                        for r in selected_rows
                        if r["rule"] == rule
                        and r["board"] == board
                        and (r["candidate"], r["reference"]) == group
                    ],
                    key=lambda r: (r["seed"], r["repeat"]),
                )
                for j, row in enumerate(matches):
                    y = i + (j - (len(matches) - 1) / 2) * 0.1
                    colour = colours[row["seed"]]
                    if row["score"] <= 0.97:
                        ax.hlines(
                            y,
                            row["score"],
                            row["ratio_upper_bound"],
                            color=colour,
                            alpha=0.65,
                            linewidth=1,
                            zorder=1,
                        )
                        ax.vlines(
                            row["ratio_upper_bound"],
                            y - 0.025,
                            y + 0.025,
                            color=colour,
                            linewidth=1,
                            zorder=2,
                        )
                    ax.scatter(
                        row["score"],
                        y,
                        marker=markers[row["outcome"]],
                        color=colour,
                        s=38,
                        zorder=3,
                    )
            ax.axvline(1, color="grey", linestyle="--", linewidth=1)
            ax.axvline(0.97, color="green", linestyle=":", linewidth=1)
            ax.set_xscale("log")
            ax.set_xlim(*limits[board])
            ax.xaxis.set_major_locator(FixedLocator(np.geomspace(*limits[board], 4)))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.3f}×"))
            ax.xaxis.set_minor_locator(NullLocator())
            ax.set_yticks(range(len(groups)), [f"{a} / {b}" for a, b in groups], fontsize=8)
            ax.invert_yaxis()
            ax.set_xlabel("Candidate/reference time ratio")
            ax.set_title(board)
            ax.grid(axis="x", alpha=0.2)
        legend = [
            plt.Line2D([], [], color=colours[s], marker="o", linestyle="", label=f"Seed {s}")
            for s in seeds
        ]
        legend += [
            plt.Line2D([], [], color="black", marker=m, linestyle="", label=label)
            for m, label in labels.items()
        ]
        fig.legend(handles=legend, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.05))
        fig.suptitle(
            f"{rule}: same saved session scores, policy-specific decisions\n"
            "Segments end at 95% upper ratio bound; revised passes still need confirmation",
            y=1.12,
        )
        fig.tight_layout()
        for suffix in ("png", "svg"):
            fig.savefig(
                output / "visualizations" / f"session-scores-{rule}.{suffix}",
                dpi=170,
                bbox_inches="tight",
            )
        plt.close(fig)


def report(output, manifests, rows, confirmed):
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/family-policy-replay-mpl")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import TwoSlopeNorm

    summary = [
        "# Revised model-family admission — saved-data replay",
        "",
        "No new benchmark processes ran. Archived scoring/config hashes were verified, "
        "and every original score, confidence bound and outcome reproduced before changing rules.",
        "",
        "## Rules",
        "",
        "1. The confidence bound must establish at least 3% gain (upper time-ratio bound ≤0.97).",
        "2. Overall uses a 10% guard on each model family's median round g"
        "eomean; other boards keep per-cell guards.",
        "3. Both preassigned sessions must pass: first manifest seed scree"
        "ns, second seed confirms, within each repeat and dataset.",
        "",
        "Scores and bootstrap draws are unchanged. We change decisions, no"
        "t the measured performance.",
        "",
        "## Ablation on individual sessions (cumulative rule changes)",
        "",
        "| Rule | Identical-code qualifying decisions | Large-gain control qualifying decisions |",
        "|---|---:|---:|",
    ]
    counts = []
    for rule in RULES:
        selected = [r for r in rows if r["rule"] == rule]
        copies = [r for r in selected if r["candidate"] == r["reference"]]
        positive = [
            r
            for r in selected
            if r["candidate"] == "incumbent"
            and r["reference"] == "slowdepthwise"
            and r["board"] in ("overall", "mobilenet")
        ]
        a = sum(r["outcome"] == "qualifies" for r in copies)
        b = sum(r["outcome"] == "qualifies" for r in positive)
        summary.append(f"| {rule} | {a}/{len(copies)} | {b}/{len(positive)} |")
        counts.append(
            dict(
                rule=rule,
                copies_qualify=a,
                copies_total=len(copies),
                positive_qualify=b,
                positive_total=len(positive),
            )
        )
    copies = [r for r in confirmed if r["candidate"] == r["reference"]]
    positive = [
        r
        for r in confirmed
        if r["candidate"] == "incumbent"
        and r["reference"] == "slowdepthwise"
        and r["board"] in ("overall", "mobilenet")
    ]
    summary += [
        "",
        "## Confirmation replay (different denominator)",
        "",
        "- Identical-code confirmed attempts: "
        f"**{sum(r['outcome'] == 'confirmed' for r in copies)}/{len(copies)}**.",
        "- Large-gain confirmed attempts: "
        f"**{sum(r['outcome'] == 'confirmed' for r in positive)}/{len(positive)}**.",
        "",
        "An attempt contains two sessions; this count is not directly comp"
        "arable to individual-session counts above.",
        "No retries-until-pass or best-session selection were used. A fail"
        "ed screening ends that attempt; "
        "the other recorded session is not promoted to screening instead.",
        "",
        "## Main comparison after confirmation",
        "",
        "| Candidate / reference | Board | Median saved ratio | Confirmed attempts |",
        "|---|---|---:|---:|",
    ]
    main = [
        r
        for r in confirmed
        if manifests[r["dataset"]]["stage"] == "short" and len(manifests[r["dataset"]]["pairs"]) > 1
    ]
    groups = sorted({(r["candidate"], r["reference"]) for r in main})
    values = []
    for group in groups:
        group_values = []
        for board in BOARDS:
            selected = [
                r for r in main if (r["candidate"], r["reference"]) == group and r["board"] == board
            ]
            original = [
                r["score"]
                for r in rows
                if r["rule"] == "family-guard"
                and (r["candidate"], r["reference"]) == group
                and r["board"] == board
                and r["dataset"] in {p["dataset"] for p in selected}
            ]
            value = median(original)
            wins = sum(r["outcome"] == "confirmed" for r in selected)
            group_values.append(value)
            summary.append(
                f"| {' / '.join(group)} | {board} | {value:.3f}× | {wins}/{len(selected)} |"
            )
        values.append(group_values)
    if groups:
        values = np.array(values)
        fig, ax = plt.subplots(figsize=(13, 6))
        mesh = ax.imshow(
            np.log2(values),
            cmap="RdBu_r",
            aspect="auto",
            norm=TwoSlopeNorm(
                vmin=min(-0.2, np.log2(values).min()),
                vcenter=0,
                vmax=max(0.2, np.log2(values).max()),
            ),
        )
        for i, group in enumerate(groups):
            for j, board in enumerate(BOARDS):
                selected = [
                    r
                    for r in main
                    if (r["candidate"], r["reference"]) == group and r["board"] == board
                ]
                wins = sum(r["outcome"] == "confirmed" for r in selected)
                ax.text(
                    j,
                    i,
                    f"{values[i, j]:.3f}×\n{wins}/{len(selected)} confirmed",
                    ha="center",
                    va="center",
                    fontsize=9,
                    bbox=dict(facecolor="white", alpha=0.8, edgecolor="none"),
                )
        ax.set_xticks(range(6), BOARDS, rotation=20)
        ax.set_yticks(range(len(groups)), [f"{a} / {b}" for a, b in groups])
        ax.set_title(
            "Saved time ratios; revised policy + preassigned confirmation\nOne "
            "attempt = two different-seed sessions; timing scores unchanged"
        )
        fig.colorbar(mesh, ax=ax, label="log₂ time ratio")
        fig.tight_layout()
        for suffix in ("png", "svg"):
            fig.savefig(
                output / "visualizations" / f"confirmed-families.{suffix}",
                dpi=170,
                bbox_inches="tight",
            )
        plt.close(fig)
        screening_plots(output, rows, groups, {r["dataset"] for r in main})
    summary += [
        "",
        "## Limits and policy tradeoff",
        "",
        "This is a retrospective policy experiment, not a change to coordi"
        "nator/worker promotion logic. "
        "The current checkout has another schema/layout; archived observat"
        "ions were evaluated with their own frozen code, not reinterpreted"
        " through a different measurement protocol.",
        "",
        "The rules were chosen after observing these data. Blocking the kn"
        "own false wins is an in-sample result, not proof of a low false-p"
        "romotion rate. Fresh held-out controls remain necessary. "
        "Different seeds/sessions do not guarantee statistical independenc"
        "e or repair systematic timing bias.",
        "",
        "A family-level overall guard allows an individual cell to regress"
        " by more than 10% when its weighted family score stays within 10%"
        ". "
        "That tradeoff is intentional. Other boards retain the existing pe"
        "r-cell guard; the guard remains a point estimate, not a confidenc"
        "e-aware regression test.",
        "",
        "This keeps each reference fixed. It does not simulate an evolving"
        " champion history or payouts. "
        "Separate confirmation is demonstrated with existing sessions; liv"
        "e confirmation scheduling still needs integration before deployme"
        "nt.",
        "",
        "See summaries/individual-decisions.csv for old/new outcomes and c"
        "onfidence bounds, and summaries/confirmed-attempts.csv for confir"
        "mation evidence. "
        "Raw benchmark directories were not modified.",
        "",
        "Board views of the same sessions are correlated. The counts above are not independent "
        "trials or a false-promotion-rate estimate.",
    ]
    (output / "REPORT.md").write_text("\n".join(summary) + "\n")
    save(output / "summaries/policy-counts.json", counts)
    (output / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Admission replay</title"
        "><style>body{font:16px sans-serif;margin:2em}img{max-width:100%}<"
        "/style><h1>Revised admission replay</h1><p><a href='REPORT.md'>Re"
        "port and limits</a></p><img src='visualizations/confirmed-familie"
        "s.png' alt='Confirmed family outcomes'>"
        "<h2>Original screening</h2><img src='visualizations/session-scores-original.png'>"
        "<h2>Revised screening</h2><img src='visualizations/session-scores-family-guard.png'>"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("choose a new output directory to preserve previous results")
    manifests, rows = {}, []
    for directory in args.inputs:
        directory = directory.resolve()
        if directory.name in manifests:
            parser.error("input dataset names must be unique")
        manifest, decisions = replay(directory)
        manifests[directory.name] = manifest
        rows.extend(decisions)
    confirmed = confirmation_pairs(manifests, rows)
    (output / "summaries").mkdir(parents=True)
    (output / "visualizations").mkdir()
    save(
        output / "manifest.json",
        dict(
            policy=POLICY,
            rules=dict(
                bar=0.97,
                guard=1.10,
                confidence_quantile=0.05,
                confirmation="first manifest seed then second seed, same repeat and dataset",
            ),
            inputs={
                str(p.resolve()): hashlib.sha256((p / "raw/manifest.json").read_bytes()).hexdigest()
                for p in args.inputs
            },
            archived_manifests=manifests,
            raw_session_hashes={
                str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                for directory in args.inputs
                for path in sorted((directory / "raw/sessions").glob("*/result.json"))
            },
        ),
    )
    save(output / "summaries/individual-decisions.json", rows)
    save(output / "summaries/confirmed-attempts.json", confirmed)
    write_csv(output / "summaries/individual-decisions.csv", rows)
    write_csv(output / "summaries/confirmed-attempts.csv", confirmed)
    for path in (Path(__file__), Path(__file__).with_name("model_family_policy.py")):
        shutil.copy2(path, output / "summaries" / path.name)
    report(output, manifests, rows, confirmed)
    print(
        f"Reproduced {len(rows) // len(RULES)} original decisions; saved policy replay to {output}"
    )


if __name__ == "__main__":
    main()
