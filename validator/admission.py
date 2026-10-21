"""Statistical admission: whether a submission beat its champion, by a bootstrap over paired rounds.

Every submission is timed against its slot's current champion, the best submission so far. The
headline ratio (fastest round against fastest round, per class) alone does not promote it, because
a copy of the champion that lands a lucky round would pass. Admission asks for evidence instead, as
the deflate competition does: a submission qualifies, and becomes the new champion, when its ratio
is at or below the promotion bar and the 5th percentile of its bootstrapped gain over the champion,
over the gate's rounds each pairing a champion run with a submission run, is above zero.

The statistic is the median of per-round ratios (submission total over champion total). Draws use a
seed derived from the evidence itself, so every validator reaches the same decision.

A full entry scored on a suite (benchmark_suite.py) carries each case's times instead, and is
judged by SUITE_POLICY_VERSION. Its statistic is the median per-round suite score, the weighted
geometric mean over cells (kernel and group) of each cell's mean log ratio. Each bootstrap draw
resamples the cases within every cell as well as the rounds, so a gain must hold across the kinds of
input the suite drew, not only across timing noise. It is also refused, "regressed", when any
cell is slower than the champion by more than REGRESSION_GUARD: a fast path for some inputs may not
pay for itself with a slowdown on others.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import json
import math
import random
from statistics import median

POLICY_VERSION = "paired-median-bootstrap-v1"
DRAWS = 2000
# One-sided: the gain must be positive in at least 95% of draws.
QUANTILE = 0.05
# Fewer paired rounds than this is not evidence.
MIN_ROUNDS = 5
# Suite evidence: its own policy (and draw seeds), so legacy decisions keep theirs.
SUITE_POLICY_VERSION = "suite-cell-bootstrap-v1"
# The most a suite cell may be slower than the champion, as a ratio of its fastest rounds.
REGRESSION_GUARD = 1.05


@dc.dataclass(frozen=True, slots=True)
class SuiteEvidence:
    # A suite verdict's cases: each one's cell, each cell's weight and headline ratio, and per
    # round each case's (incumbent ns, submission ns).
    cells: tuple[str, ...]
    weights: tuple[float, ...]
    cell_ratios: tuple[float, ...]
    case_cells: tuple[int, ...]
    rounds: tuple[tuple[tuple[float, float], ...], ...]


@dc.dataclass(frozen=True, slots=True)
class Evidence:
    # One accepted submission's paired rounds: (incumbent ns, submission ns) per round.
    submission_id: int
    hotkey: str
    ratio: float
    submitted_at: dt.datetime
    rounds: tuple[tuple[float, float], ...]
    suite: SuiteEvidence | None = None


@dc.dataclass(frozen=True, slots=True)
class Decision:
    # Where a submission stands against its champion; `gain_p05` is the 5th-percentile gain.
    submission_id: int
    outcome: str  # "leader" (the champion), "qualifies", "not-significant", "above-bar",
    # "regressed" (a suite cell slower than the guard allows), "no-evidence"
    reference: int | None
    gain_p05: float | None


def _seed(*parts: object) -> int:
    # Deterministic per comparison: the policy and both submissions' evidence.
    blob = json.dumps([POLICY_VERSION, *parts], sort_keys=True, default=str).encode()
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")


def _quantile(xs: list[float], p: float) -> float:
    # Linear interpolation at (n-1)*p, as deflate's admission does.
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _ratios(e: Evidence) -> list[float]:
    # Each round's submission time over the incumbent's; a zero incumbent time is skipped.
    return [sub / inc for inc, sub in e.rounds if inc > 0]


def _resampled_median(rng: random.Random, xs: list[float]) -> float:
    # One bootstrap draw: the median of len(xs) values sampled from xs with replacement.
    return median(rng.choice(xs) for _ in xs)


def gain_vs_incumbent(e: Evidence) -> float:
    # 5th-percentile gain over the incumbent, 1 - median paired ratio, rounds resampled in pairs.
    ratios = _ratios(e)
    rng = random.Random(_seed("incumbent", e.submission_id, e.rounds))
    return _quantile([1.0 - _resampled_median(rng, ratios) for _ in range(DRAWS)], QUANTILE)


def suite_evidence(verdict: dict | str | None) -> SuiteEvidence | None:
    # The suite evidence in a verdict (its JSON or the parsed dict); None for a legacy verdict.
    if isinstance(verdict, str):
        try:
            verdict = json.loads(verdict)
        except json.JSONDecodeError:
            return None
    suite = verdict.get("suite") if isinstance(verdict, dict) else None
    if not suite:
        return None
    classes = verdict.get("classes") or {}
    cells = tuple(sorted(suite["cells"]))
    index = {cell: k for k, cell in enumerate(cells)}
    return SuiteEvidence(
        cells,
        tuple(float(suite["cells"][c]) for c in cells),
        tuple(float(classes.get(c, {}).get("ratio", math.inf)) for c in cells),
        tuple(index[c] for c in suite["cases"]),
        tuple(tuple((float(i), float(s)) for i, s in r) for r in suite["rounds"]),
    )


def _suite_logs(s: SuiteEvidence) -> list[list[float]]:
    # Per complete round, each case's log ratio; a round with a zero time is skipped.
    return [
        [math.log(sub / inc) for inc, sub in r]
        for r in s.rounds
        if r and all(inc > 0 and sub > 0 for inc, sub in r)
    ]


def gain_vs_incumbent_suite(e: Evidence) -> float:
    # 5th-percentile gain, 1 - median round score, with rounds and each cell's cases resampled.
    s = e.suite
    assert s is not None
    logs = _suite_logs(s)
    members: list[list[int]] = [[] for _ in s.cells]
    for case, cell in enumerate(s.case_cells):
        members[cell].append(case)
    rng = random.Random(_seed(SUITE_POLICY_VERSION, "incumbent", e.submission_id, s.rounds))
    gains = []
    for _ in range(DRAWS):
        coef: dict[int, float] = {}
        for cell, cases in enumerate(members):
            if cases:
                share = s.weights[cell] / len(cases)
                for _ in cases:
                    k = rng.choice(cases)
                    coef[k] = coef.get(k, 0.0) + share
        scores = [math.exp(sum(c * row[k] for k, c in coef.items())) for row in logs]
        gains.append(1.0 - median(rng.choice(scores) for _ in scores))
    return _quantile(gains, QUANTILE)


def assess(e: Evidence, bar: float) -> Decision:
    # Whether a submission beat the champion it was timed against: at or below the bar, and the
    # gain significant; one that qualifies is promoted to champion.
    if e.suite is not None:
        return _assess_suite(e, bar)
    if len(_ratios(e)) < MIN_ROUNDS:
        return Decision(e.submission_id, "no-evidence", None, None)
    if e.ratio > bar:
        return Decision(e.submission_id, "above-bar", None, None)
    gain = gain_vs_incumbent(e)
    return Decision(e.submission_id, "qualifies" if gain > 0 else "not-significant", None, gain)


def _assess_suite(e: Evidence, bar: float) -> Decision:
    # assess() for suite evidence: the bar, then the regression guard, then significance.
    assert e.suite is not None
    if len(_suite_logs(e.suite)) < MIN_ROUNDS:
        return Decision(e.submission_id, "no-evidence", None, None)
    if e.ratio > bar:
        return Decision(e.submission_id, "above-bar", None, None)
    if max(e.suite.cell_ratios) > REGRESSION_GUARD:
        return Decision(e.submission_id, "regressed", None, None)
    gain = gain_vs_incumbent_suite(e)
    return Decision(e.submission_id, "qualifies" if gain > 0 else "not-significant", None, gain)
