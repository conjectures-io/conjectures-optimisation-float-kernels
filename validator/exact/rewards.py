"""Pay two eligible historical champions per board from coordinator-published policy only."""

from __future__ import annotations

import copy

import reward

from .config import digest, weights


def score(publication: dict) -> reward.Scoring:
    if publication.get("version") != "exact-top2-v1":
        raise ValueError("unknown exact reward publication")
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
            rows.append(
                reward.HotkeyScore(board, king["hotkey"], king["submission_id"], None, part, rank)
            )
    return reward.Scoring(tuple(rows))


def snapshot(publication, result, competition_share, computed_at):
    return {
        "competition": "exact",
        "computed_at": computed_at.isoformat(),
        "policy": {
            "version": publication["version"],
            "competition_share": competition_share,
            **copy.deepcopy(publication["rewards"]),
        },
        "boards": copy.deepcopy(publication["boards"]),
        "items": [s.as_snapshot() | {"rank": s.rank} for s in result.scores],
        "submission_state_sha256": digest(publication),
    }
