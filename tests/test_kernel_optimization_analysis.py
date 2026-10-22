"""Verify that paired-order analysis removes order cost without removing genuine gain."""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "kernel_analysis",
    Path(__file__).resolve().parents[1] / "scripts/summarize-kernel-optimizations.py",
)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


@pytest.mark.parametrize("gain", [1.0, 0.8, 1.2])
def test_opposite_order_cancels_position_effect_but_preserves_algorithm_ratio(gain):
    paired = analysis.counterbalance([gain * 1.3, gain / 1.3] * 8)
    assert paired == pytest.approx([gain] * 8)
    assert analysis.block_upper(paired, 1) == pytest.approx(gain)


def test_incomplete_order_pair_is_not_treated_as_balanced_evidence():
    assert analysis.counterbalance([0.8, 1.25, 0.01]) == [1.0]


def test_block_bound_preserves_constant_ratio():
    assert analysis.block_upper([0.9] * 16, 4) == pytest.approx(0.9)
