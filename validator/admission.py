"""Statistical admission: who may lead, decided by a bootstrap over the gate's paired rounds.

The headline ratio (fastest round against fastest round, per class) ranks the board; it does not
by itself decide who is paid, because a copy of the leader that lands a lucky round would beat it.
Admission asks for evidence instead, as the deflate competition does:

- a submission qualifies when its ratio is at or below the promotion bar and it is significantly
  faster than the incumbent: over the gate's rounds, each pairing an incumbent run with a
  submission run, the 5th percentile of the bootstrapped gain is above zero;
- accepted submissions are taken in submission order, and a qualifying one takes the lead only
  when it is significantly faster than the current leader, the two runs resampled independently.

The statistic is the median of per-round ratios (submission total over incumbent total). Draws
use a seed derived from the evidence itself, so every validator reaches the same decision.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import json
import random
from collections.abc import Sequence
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
    # Why a submission leads or not; `gain_p05` is the 5th-percentile bootstrapped gain.
    submission_id: int
    outcome: str  # "leader", "not-significant", "above-bar", "no-evidence"
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


def gain_vs_leader(e: Evidence, leader: Evidence) -> float:
    # 5th-percentile gain over the leader, each run's rounds resampled independently.
    mine, theirs = _ratios(e), _ratios(leader)
    rng = random.Random(
        _seed("leader", e.submission_id, e.rounds, leader.submission_id, leader.rounds)
    )
    gains = [
        1.0 - _resampled_median(rng, mine) / _resampled_median(rng, theirs) for _ in range(DRAWS)
    ]
    return _quantile(gains, QUANTILE)


def decide(accepted: Sequence[Evidence], bar: float) -> list[Decision]:
    # Walk accepted submissions in submission order; the last "leader" decision holds the lead.
    decisions: list[Decision] = []
    leader: Evidence | None = None
    for e in sorted(accepted, key=lambda x: (x.submitted_at, x.submission_id)):
        if len(_ratios(e)) < MIN_ROUNDS:
            decisions.append(Decision(e.submission_id, "no-evidence", None, None))
            continue
        if e.ratio > bar:
            decisions.append(Decision(e.submission_id, "above-bar", None, None))
            continue
        vs_incumbent = gain_vs_incumbent(e)
        if vs_incumbent <= 0:
            decisions.append(Decision(e.submission_id, "not-significant", None, vs_incumbent))
            continue
        if leader is None:
            decisions.append(Decision(e.submission_id, "leader", None, vs_incumbent))
            leader = e
            continue
        vs_leader = gain_vs_leader(e, leader)
        if vs_leader > 0:
            decisions.append(Decision(e.submission_id, "leader", leader.submission_id, vs_leader))
            leader = e
        else:
            decisions.append(
                Decision(e.submission_id, "not-significant", leader.submission_id, vs_leader)
            )
    return decisions


def leader(decisions: Sequence[Decision]) -> int | None:
    # The submission holding the lead after every decision, if any.
    led = [d.submission_id for d in decisions if d.outcome == "leader"]
    return led[-1] if led else None
