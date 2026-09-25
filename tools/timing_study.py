"""Timing study - alternates full gate runs of two submissions for a fixed time, then summarises."""

from __future__ import annotations

import fcntl
import json
import os
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402 - the gate lives at the repository root, one level up

_MINUTES = float(os.environ.get("STUDY_MINUTES", "60"))
SUBMISSIONS = ["incumbent", "fastlog"]
# A kernel at or below this ratio would be promoted under the proposed 3% rule.
PROMOTION_BAR = 0.97


def summarise(runs: list[dict]) -> dict:
    # Ratio statistics over accepted runs, the share that would clear the bar, and per-class ranges.
    ok = [r for r in runs if r["passed"]]
    ratios = [r["ratio"] for r in ok]
    if not ratios:
        return {"runs": len(runs), "accepted": 0}
    classes = {c: [r["classes"][c]["ratio"] for r in ok] for c in ok[0]["classes"]}
    return {
        "runs": len(runs),
        "accepted": len(ok),
        "ratio_mean": statistics.mean(ratios),
        "ratio_stdev": statistics.stdev(ratios) if len(ratios) > 1 else 0.0,
        "ratio_min": min(ratios),
        "ratio_max": max(ratios),
        "at_or_below_bar": sum(x <= PROMOTION_BAR for x in ratios),
        "class_ratio_range": {c: [min(v), max(v)] for c, v in classes.items()},
        "seconds_mean": statistics.mean(r["seconds"] for r in runs),
    }


def markdown(summary: dict, minutes: float) -> str:
    # The study's result as one table per submission.
    lines = [f"Timing study, {minutes:.0f} minutes, bar {PROMOTION_BAR}", ""]
    lines += [
        "| submission | runs | accepted | mean | stdev | min | max | at or below bar | s/run |"
    ]
    lines += ["|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, s in summary.items():
        if not s.get("accepted"):
            lines.append(f"| {name} | {s['runs']} | 0 | | | | | | |")
            continue
        stats = (s["ratio_mean"], s["ratio_stdev"], s["ratio_min"], s["ratio_max"])
        cells = " | ".join(f"{x:.4f}" for x in stats)
        lines.append(
            f"| {name} | {s['runs']} | {s['accepted']} | {cells} "
            f"| {s['at_or_below_bar']} | {s['seconds_mean']:.0f} |"
        )
    return "\n".join(lines)


def main() -> int:
    # Alternates the submissions until the deadline, saving every verdict as it lands.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.add(HERE / "logs" / f"timing-study-{stamp}.log", level="DEBUG")
    out = HERE / "results" / f"timing-study-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    runs: dict[str, list[dict]] = {n: [] for n in SUBMISSIONS}
    started = time.monotonic()
    with open(HERE / "gate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        digest = gate.pin()
        i = 0
        while time.monotonic() - started < _MINUTES * 60:
            name = SUBMISSIONS[i % len(SUBMISSIONS)]
            v = asdict(gate.gate(HERE / "submissions" / name, digest))
            v["load1"] = os.getloadavg()[0]
            runs[name].append(v)
            logger.info(
                f"[study] #{i + 1:3} {name:10} passed={v['passed']} ratio={v['ratio']} "
                f"seed={v['seed']} load={v['load1']:.2f} {v['seconds']:.0f}s"
            )
            out.write_text(json.dumps({"bar": PROMOTION_BAR, "runs": runs}, indent=2))
            i += 1
    minutes = (time.monotonic() - started) / 60
    summary = {n: summarise(r) for n, r in runs.items()}
    out.write_text(
        json.dumps(
            {"bar": PROMOTION_BAR, "minutes": minutes, "summary": summary, "runs": runs}, indent=2
        )
    )
    table = markdown(summary, minutes)
    out.with_suffix(".md").write_text(table + "\n")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
