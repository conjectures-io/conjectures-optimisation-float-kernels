"""Leaderboards -> the competition's weights: the last three champions' authors are paid.

One entry, one paid board: PAYING_SLOTS is the full entry alone (FloatLib's add and mul with
TorchLean's sum and bmm built from them, proved and timed together). A submission that beats the
champion becomes the champion. The current champion and the two before it (demoted ones skipped)
split the winner share of the competition's budget by RANK_SHARES, 60/30/10; whatever they do not
earn goes to the treasury, as while the shipped champion still holds or fewer than three were ever
promoted. One hotkey holding several ranks is paid for each. The single-kernel slots are ranked
practice boards, unpaid. Like the treasury split, the paying slots and the rank shares are code,
not configuration: where emission goes is a reviewed change. The
weights here are fractions of the competition's budget; treasury.py turns them into the
validator's vector. `publication` is what each weight set publishes for the platform to serve.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import os
from collections.abc import Sequence

import admission
from db.scoring import Board, ScoredSubmission
from service.settings import PROMOTION_BAR

# The scoring rule a published snapshot was computed under; bump it when the rule changes.
POLICY_VERSION = "top3-kings-60-30-10-v1"

# The boards that pay and each one's part of the competition's budget, listed here rather than taken
# from db.SLOTS so a board pays only once it is added on purpose.
PAYING_SLOTS: tuple[str, ...] = ("full",)
SLOT_SHARES: dict[str, float] = {slot: 1.0 / len(PAYING_SLOTS) for slot in PAYING_SLOTS}

# Each paid champion's part of its slot's winner share, newest first: the current champion, then
# the one it took over from, then the one before that.
RANK_SHARES: tuple[float, ...] = (0.60, 0.30, 0.10)
assert abs(sum(RANK_SHARES) - 1.0) < 1e-12, "the rank shares must sum to one"


@dc.dataclass(frozen=True, slots=True)
class ScoringConfig:
    # The paid champions' share of their slot's part of the budget; the treasury takes the rest.
    winner_share: float = 1.0
    # The promotion bar a submission must reach to become champion, published with the policy and
    # the same one the leaderboard's `qualifies` uses.
    max_ratio: float = PROMOTION_BAR
    paused_slot_policy: str = "preserve"

    def __post_init__(self):
        if self.paused_slot_policy not in ("preserve", "treasury"):
            raise ValueError("WEIGHT_PAUSED_SLOT_POLICY must be preserve or treasury")

    @classmethod
    def from_env(cls) -> ScoringConfig:
        # Reads WEIGHT_WINNER_SHARE, keeping the default otherwise; the bar is PROMOTION_BAR.
        share = float(os.environ.get("WEIGHT_WINNER_SHARE", str(cls().winner_share)))
        if not 0.0 <= share <= 1.0:
            raise ValueError("WEIGHT_WINNER_SHARE must be between 0 and 1")
        return cls(share, paused_slot_policy=os.getenv("WEIGHT_PAUSED_SLOT_POLICY", "preserve"))


@dc.dataclass(frozen=True, slots=True)
class HotkeyScore:
    # One competitor's line in one slot's audit: its best ratio there and the weight it came to, or
    # one paid champion's line, `rank` 1 for the current champion.
    slot: str
    hotkey: str
    submission_id: int | None
    ratio: float | None
    weight: float
    rank: int | None = None

    def as_snapshot(self) -> dict:
        # The shape db.scoring.record_weight_set stores.
        return {
            "slot": self.slot,
            "hotkey": self.hotkey,
            "submission_id": self.submission_id,
            "ratio": self.ratio,
            "weight": self.weight,
        }


@dc.dataclass(frozen=True, slots=True)
class Scoring:
    # Every competitor's score in every paying slot, for one epoch.
    scores: tuple[HotkeyScore, ...]

    @property
    def weights(self) -> dict[str, float]:
        # Per-hotkey fractions of the competition's budget, summed over the ranks a hotkey holds.
        total: dict[str, float] = {}
        for s in self.scores:
            if s.weight > 0:
                total[s.hotkey] = total.get(s.hotkey, 0.0) + s.weight
        return total

    def snapshots(self) -> list[dict]:
        # The audit rows written beside the vector.
        return [s.as_snapshot() for s in self.scores]

    def summary(self) -> str:
        # One line for the weight_sets row and the log: each slot's paid champions, if any.
        kings = [
            f"{s.slot}#{s.rank}:{s.hotkey[:8]}…"
            + (f"@{s.ratio:.4f}" if s.ratio is not None else "")
            + f"={s.weight:.2f}"
            for s in self.scores
            if s.weight > 0
        ]
        hotkeys = len({s.hotkey for s in self.scores})
        return f"competitors={hotkeys} kings=[{', '.join(kings) or 'none'}]"


def score_board(
    slot: str,
    best_per_hotkey: Sequence[ScoredSubmission],
    config: ScoringConfig,
    kings: Sequence[ScoredSubmission],
    *,
    withhold_leader: bool = False,
) -> list[HotkeyScore]:
    # One board: the last champions' authors, newest first, take RANK_SHARES of the board's winner
    # share (the current one's withheld for the treasury when `withhold_leader`), and every other
    # hotkey's line names its best submission against the champion.
    part = SLOT_SHARES.get(slot, 0.0) * config.winner_share
    scores = [
        HotkeyScore(
            slot,
            king.hotkey,
            king.submission_id,
            king.ratio,
            0.0 if withhold_leader and rank == 1 else part * share,
            rank,
        )
        for rank, (king, share) in enumerate(zip(kings, RANK_SHARES), start=1)
    ]
    paid = {s.hotkey for s in scores}
    scores += [
        HotkeyScore(slot, row.hotkey, row.submission_id, row.ratio, 0.0)
        for row in best_per_hotkey
        if row.hotkey not in paid
    ]
    return scores


def score(boards: Sequence[Board], config: ScoringConfig) -> Scoring:
    # Every paying slot's board, scored on its own and combined.
    scores: list[HotkeyScore] = []
    for board in boards:
        if board.slot in SLOT_SHARES:
            scores += score_board(
                board.slot,
                board.best,
                config,
                board.kings,
                withhold_leader=board.paused and config.paused_slot_policy == "treasury",
            )
    return Scoring(tuple(scores))


def publication(
    result: Scoring,
    boards: Sequence[Board],
    config: ScoringConfig,
    *,
    competition_share: float,
    computed_at: dt.datetime,
) -> dict[str, object]:
    # The scoring one weight set was computed from, as JSON the platform serves unchanged: the
    # policy and, per board, its share, leader, paid champions, lines and admission outcomes, in
    # fractions of the competition's budget that `competition_share` scales to the validator's.
    slots: dict[str, object] = {}
    for board in boards:
        outcome = {d.submission_id: d.outcome for d in board.decisions}
        slots[board.slot] = {
            "paused": board.paused,
            "paused_reward_policy": config.paused_slot_policy,
            "share": SLOT_SHARES.get(board.slot, 0.0),
            "leader": board.leader.submission_id if board.leader else None,
            "kings": [
                s.submission_id
                for s in result.scores
                if s.slot == board.slot and s.rank is not None
            ],
            "items": [
                {
                    "hotkey": s.hotkey,
                    "submission_id": s.submission_id,
                    "ratio": s.ratio,
                    "weight": s.weight,
                    "rank": s.rank,
                    "admission": outcome.get(s.submission_id) if s.submission_id else None,
                }
                for s in result.scores
                if s.slot == board.slot
            ],
        }
    return {
        "competition": "floatlib",
        "computed_at": computed_at.isoformat(),
        "policy": {
            "version": POLICY_VERSION,
            "competition_share": competition_share,
            "slot_shares": SLOT_SHARES,
            "winner_share": config.winner_share,
            "rank_shares": list(RANK_SHARES),
            "promotion_bar": config.max_ratio,
            "admission": {
                "version": admission.POLICY_VERSION,
                "draws": admission.DRAWS,
                "quantile": admission.QUANTILE,
            },
        },
        "slots": slots,
    }
