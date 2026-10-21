"""Revised experimental policy: confidence, guard scope and fixed confirmation."""

import copy

import pytest

from scripts.model_family_policy import confirm, revise


def evidence(ratios):
    return [
        {
            "cells": {
                cell: {"reference_ns": 1000, "candidate_ns": 1000 * ratio}
                for cell, ratio in ratios.items()
            }
        }
        for _ in range(5)
    ]


@pytest.mark.parametrize(
    "lower,expected", [(0.0183, "gain-not-confident"), (0.03, "qualifies"), (0.1, "qualifies")]
)
def test_confidence_must_cover_entire_required_gain(lower, expected):
    original = {"score": 0.9378, "gain_lower": lower}
    before = copy.deepcopy(original)
    decision = revise(original, evidence({"mlp:b1:normal": 0.9378}), {"mlp:b1:normal": 1}, "mlp")
    assert decision["outcome"] == expected
    assert original == before


def test_overall_guard_allows_local_regression_but_single_family_keeps_cell_guard():
    ratios = {"mlp:b1:normal": 0.5, "resnet:b1:normal": 1.15, "resnet:b4:normal": 1.0}
    rows = evidence(ratios)
    old = {"score": 0.8, "gain_lower": 0.15}
    weights = {cell: 1 / len(ratios) for cell in ratios}
    proposed = revise(old, rows, weights, "overall")
    assert proposed["outcome"] == "qualifies"
    assert proposed["policy_guards"]["resnet"] < 1.1
    assert revise(old, rows, weights, "overall", family_guard=False)["outcome"] == "guard-failed"
    resnet = {cell: value for cell, value in weights.items() if cell.startswith("resnet:")}
    assert revise(old, rows, resnet, "resnet")["outcome"] == "guard-failed"


def test_overall_guard_still_blocks_family_regressions_despite_large_overall_gain():
    ratios = {"mlp:b1:normal": 0.1, "resnet:b1:normal": 1.2}
    decision = revise(
        {"score": 0.35, "gain_lower": 0.6}, evidence(ratios), dict.fromkeys(ratios, 0.5), "overall"
    )
    assert decision["outcome"] == "guard-failed"


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("qualifies", "qualifies", "confirmed"),
        ("qualifies", "gain-not-confident", "not-confirmed"),
        ("above-bar", "qualifies", "screening-rejected"),
        ("qualifies", None, "confirmation-required"),
    ],
)
def test_confirmation_requires_both_preassigned_sessions(first, second, expected):
    result = confirm(
        {"outcome": first},
        {"outcome": second} if second else None,
        screening_seed=42,
        confirmation_seed=99,
    )
    assert result == expected


def test_same_seed_cannot_confirm_screening():
    with pytest.raises(ValueError, match="different workload seed"):
        confirm(
            {"outcome": "qualifies"},
            {"outcome": "qualifies"},
            screening_seed=42,
            confirmation_seed=42,
        )
