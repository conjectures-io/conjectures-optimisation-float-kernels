"""Leaderboard -> weight vector: king of the hill, a placeholder until rewards are set."""

from __future__ import annotations

import dataclasses as dc
import os
from collections.abc import Sequence

from chain.types import MetagraphView, WeightPlan
from db.scoring import ScoredSubmission
from service.settings import PROMOTION_BAR


@dc.dataclass(frozen=True, slots=True)
class ScoringConfig:
    # The leader's share of emission; the rest burns.
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


def to_vector(weights: dict[str, float], meta: MetagraphView, *, burn_uid: int = 0) -> WeightPlan:
    # Map per-hotkey weights onto uids; a deregistered hotkey's share and the rest burn.
    if not meta.uids:
        return WeightPlan((), (), False, "no neurons in the metagraph", "")

    by_uid = {uid: 0.0 for uid in meta.uids}
    paid: dict[int, float] = {}
    unpaid = 0.0
    for hotkey, weight in weights.items():
        uid = meta.uid_by_hotkey.get(hotkey)
        if uid is None or uid not in by_uid or uid == burn_uid:
            unpaid += weight
            continue
        by_uid[uid] += weight
        paid[uid] = paid.get(uid, 0.0) + weight

    burn = max(0.0, 1.0 - sum(paid.values()))
    if burn > 0 and burn_uid not in by_uid:
        return WeightPlan((), (), False, f"burn uid {burn_uid} absent from the metagraph", "")
    if burn > 0:
        by_uid[burn_uid] += burn

    uids = tuple(meta.uids)
    vector = tuple(by_uid[uid] for uid in uids)
    miners = ", ".join(f"uid{uid}={w:.3f}" for uid, w in sorted(paid.items()))
    summary = f"miners=[{miners}] burn={burn:.3f}->uid{burn_uid}"
    if unpaid > 0:
        summary += f" (unregistered={unpaid:.3f})"
    return WeightPlan(uids, vector, True, None, summary)
