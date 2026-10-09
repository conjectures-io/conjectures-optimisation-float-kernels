"""Statistical admission: whether a submission beat its champion, by a bootstrap over paired rounds.

Every submission is timed against its slot's current champion, the best submission so far. The
headline ratio (fastest round against fastest round, per class) alone does not promote it, because
a copy of the champion that lands a lucky round would pass. Admission asks for evidence instead, as
the deflate competition does: a submission qualifies, and becomes the new champion, when its ratio
is at or below the promotion bar and the 5th percentile of its bootstrapped gain over the champion,
over the gate's rounds each pairing a champion run with a submission run, is above zero.

The statistic is the median of per-round ratios (submission total over champion total). Draws use a
seed derived from the evidence itself, so every validator reaches the same decision.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import json
import random
from statistics import median

POLICY_VERSION = "paired-median-bootstrap-v1"
DRAWS = 2000
# One-sided: the gain must be positive in at least 95% of draws.
QUANTILE = 0.05
# Fewer paired rounds than this is not evidence.
MIN_ROUNDS = 5


@dc.dataclass(frozen=True, slots=True)
class Evidence:
    # One accepted submission's paired rounds: (incumbent ns, submission ns) per round.
    submission_id: int
    hotkey: str
    ratio: float
    submitted_at: dt.datetime
    rounds: tuple[tuple[float, float], ...]


@dc.dataclass(frozen=True, slots=True)
class Decision:
    # Where a submission stands against its champion; `gain_p05` is the 5th-percentile gain.
    submission_id: int
    outcome: (
        str  # "leader" (the champion), "qualifies", "not-significant", "above-bar", "no-evidence"
    )
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


def assess(e: Evidence, bar: float) -> Decision:
    # Whether a submission beat the champion it was timed against: at or below the bar, and the
    # gain significant; one that qualifies is promoted to champion.
    if len(_ratios(e)) < MIN_ROUNDS:
        return Decision(e.submission_id, "no-evidence", None, None)
    if e.ratio > bar:
        return Decision(e.submission_id, "above-bar", None, None)
    gain = gain_vs_incumbent(e)
    return Decision(e.submission_id, "qualifies" if gain > 0 else "not-significant", None, gain)
