"""Pay two eligible historical champions per board from coordinator-published policy only."""

from __future__ import annotations

import dataclasses as dc

from config import weights


@dc.dataclass(frozen=True, slots=True)
class HotkeyScore:
    board: str
    hotkey: str
    submission_id: int
    weight: float
    rank: int


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

    def summary(self) -> str:
        # One line for the weight_sets row and the log: each slot's paid champions, if any.
        kings = [
            f"{s.board}#{s.rank}:{s.hotkey[:8]}…" + f"={s.weight:.2f}"
            for s in self.scores
            if s.weight > 0
        ]
        hotkeys = len({s.hotkey for s in self.scores})
        return f"competitors={hotkeys} kings=[{', '.join(kings) or 'none'}]"


def score(publication: dict) -> Scoring:
    if publication.get("version") != "tensor-top2-v1":
        raise ValueError("unknown tensor reward publication")
    policy = publication["rewards"]
    weights(policy["board_weights"], "reward boards")
    shares = policy["rank_shares"]
    if len(shares) != 2:
        raise ValueError("expected two rank shares")
    weights({str(i): v for i, v in enumerate(shares)}, "rank shares")
    if policy["paused_policy"] not in ("preserve", "treasury"):
        raise ValueError("invalid pause policy")
    if set(publication["boards"]) != set(policy["board_weights"]):
        raise ValueError("partial reward publication")
    rows = []
    for board, state in publication["boards"].items():
        if len(state["kings"]) > 2 or type(state["paused"]) is not bool:
            raise ValueError("invalid board reward state")
        seen = set()
        for rank, king in enumerate(state["kings"], 1):
            if king["promotion_id"] in seen:
                raise ValueError("duplicate paid promotion")
            seen.add(king["promotion_id"])
            part = policy["board_weights"][board] * shares[rank - 1]
            if state["paused"] and policy["paused_policy"] == "treasury":
                part = 0.0
            rows.append(HotkeyScore(board, king["hotkey"], king["submission_id"], part, rank))
    return Scoring(tuple(rows))
