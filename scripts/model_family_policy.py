"""Experimental admission policy for offline replay; no worker or database side effects."""

from __future__ import annotations

import copy
import math
from statistics import median

POLICY = "confident-gain-family-guard-confirmation-v1"


def revise(
    original, rounds, weights, board, *, bar=0.97, guard=1.10, full_gain=True, family_guard=True
):
    """Keep the original score/bootstrap draws; change only admission and guard scope."""
    if not rounds or not weights:
        raise ValueError("complete paired evidence is required")
    grouped = board == "overall" and family_guard
    groups = {}
    for cell in weights:
        groups.setdefault(cell.split(":", 1)[0] if grouped else cell, []).append(cell)
    guards = {}
    for name, cells in groups.items():
        total = sum(weights[c] for c in cells)
        guards[name] = median(
            math.exp(
                sum(
                    weights[c]
                    * math.log(r["cells"][c]["candidate_ns"] / r["cells"][c]["reference_ns"])
                    for c in cells
                )
                / total
            )
            for r in rounds
        )
    guard_ok = all(value <= guard for value in guards.values())
    upper = 1 - original["gain_lower"]
    confident = (
        upper <= bar or math.isclose(upper, bar, rel_tol=0, abs_tol=1e-12)
        if full_gain
        else original["gain_lower"] > 0
    )
    outcome = (
        "guard-failed"
        if not guard_ok
        else "above-bar"
        if original["score"] > bar
        else "gain-not-confident"
        if not confident
        else "qualifies"
    )
    return copy.deepcopy(original) | {
        "policy": POLICY,
        "outcome": outcome,
        "gain_required": 1 - bar if full_gain else 0,
        "ratio_upper_bound": upper,
        "guard_scope": "model-family" if grouped else "cell",
        "guard_passed": guard_ok,
        "policy_guards": guards,
    }


def confirm(screening, confirmation, *, screening_seed, confirmation_seed=None):
    """Require both preassigned sessions to pass; do not pool rounds or search for a winner."""
    if screening["outcome"] != "qualifies":
        return "screening-rejected"
    if confirmation is None:
        return "confirmation-required"
    if confirmation_seed == screening_seed:
        raise ValueError("confirmation requires a different workload seed")
    return "confirmed" if confirmation["outcome"] == "qualifies" else "not-confirmed"
