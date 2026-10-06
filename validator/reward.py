"""Leaderboard -> the competition's weights: king of the hill, a placeholder until rewards are set.

The weights here are fractions of the competition's budget; treasury.py turns them into the
validator's vector. `publication` is what each weight set publishes for the platform to serve.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import os
from collections.abc import Sequence

import admission
from db.scoring import ScoredSubmission
from service.settings import PROMOTION_BAR

# The scoring rule a published snapshot was computed under; bump it when the rule changes.
POLICY_VERSION = "king-of-the-hill-v1"


@dc.dataclass(frozen=True, slots=True)
class ScoringConfig:
    # The leader's share of the competition's budget; the treasury takes the rest.
    winner_share: float = 1.0
    # Only a ratio at or below this pays: the promotion bar, the same one the leaderboard's
    # `qualifies` uses, so a noisy near-copy of the incumbent earns nothing.
    max_ratio: float = PROMOTION_BAR

    @classmethod
    def from_env(cls) -> ScoringConfig:
        # Reads WEIGHT_WINNER_SHARE, keeping the default otherwise; the bar is PROMOTION_BAR.
        share = float(os.environ.get("WEIGHT_WINNER_SHARE", str(cls().winner_share)))
        if not 0.0 <= share <= 1.0:
            raise ValueError("WEIGHT_WINNER_SHARE must be between 0 and 1")
        return cls(share)


@dc.dataclass(frozen=True, slots=True)
class HotkeyScore:
    # One competitor's line in the audit: its best ratio and the weight it came to.
    hotkey: str
    submission_id: int | None
    ratio: float | None
    weight: float

    def as_snapshot(self) -> dict:
        # The shape db.scoring.record_weight_set stores.
        return {
            "hotkey": self.hotkey,
            "submission_id": self.submission_id,
            "ratio": self.ratio,
            "weight": self.weight,
        }


@dc.dataclass(frozen=True, slots=True)
class Scoring:
    # Every competitor's score for one epoch.
    scores: tuple[HotkeyScore, ...]

    @property
    def weights(self) -> dict[str, float]:
        # Per-hotkey emission shares, only the paid ones.
        return {s.hotkey: s.weight for s in self.scores if s.weight > 0}

    def snapshots(self) -> list[dict]:
        # The audit rows written beside the vector.
        return [s.as_snapshot() for s in self.scores]

    def summary(self) -> str:
        # One line for the weight_sets row and the log.
        paid = [s for s in self.scores if s.weight > 0]
        leader = f"{paid[0].hotkey[:8]}… ratio={paid[0].ratio:.4f}" if paid else "none"
        return f"competitors={len(self.scores)} leader={leader}"


def score(
    best_per_hotkey: Sequence[ScoredSubmission],
    config: ScoringConfig,
    leader: ScoredSubmission | None,
) -> Scoring:
    # The admitted leader's hotkey takes the winner share (see admission.py): the fastest headline
    # ratio alone does not pay, so a lucky copy cannot unseat the submission it copied. Its line
    # names the leading submission; every other hotkey's names its best, fastest first.
    scores = []
    for row in best_per_hotkey:
        paid = (
            leader is not None and row.hotkey == leader.hotkey and leader.ratio <= config.max_ratio
        )
        shown = leader if paid and leader is not None else row
        scores.append(
            HotkeyScore(
                shown.hotkey, shown.submission_id, shown.ratio, config.winner_share if paid else 0.0
            )
        )
    scores.sort(key=lambda s: s.weight == 0)
    return Scoring(tuple(scores))


def publication(
    result: Scoring,
    decisions: Sequence[admission.Decision],
    config: ScoringConfig,
    *,
    competition_share: float,
    computed_at: dt.datetime,
) -> dict[str, object]:
    # The scoring one weight set was computed from, as JSON the platform serves unchanged: the
    # policy, every competitor's line, and each submission's admission outcome. Weights are
    # fractions of the competition's budget; `competition_share` turns them into the validator's.
    outcome = {d.submission_id: d for d in decisions}
    return {
        "competition": "floatlib",
        "slot": "add",
        "computed_at": computed_at.isoformat(),
        "policy": {
            "version": POLICY_VERSION,
            "competition_share": competition_share,
            "winner_share": config.winner_share,
            "promotion_bar": config.max_ratio,
            "admission": {
                "version": admission.POLICY_VERSION,
                "draws": admission.DRAWS,
                "quantile": admission.QUANTILE,
            },
        },
        "leader": admission.leader(decisions),
        "items": [
            {
                "hotkey": s.hotkey,
                "submission_id": s.submission_id,
                "ratio": s.ratio,
                "weight": s.weight,
                "admission": (
                    outcome[s.submission_id].outcome if s.submission_id in outcome else None
                ),
            }
            for s in result.scores
        ],
    }
