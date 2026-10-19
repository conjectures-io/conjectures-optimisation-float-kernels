"""Sanity checks for the independent audit oracle, with literal known answers."""

import random
import runpy
from fractions import Fraction
from pathlib import Path

import pytest

ORACLE = runpy.run_path(str(Path(__file__).parents[1] / "scripts/audit-exact-spec.py"))
round32 = ORACLE["round32"]
value = ORACLE["value"]
reference = ORACLE["reference"]


def test_rounding_boundaries():
    assert round32(Fraction(1) + Fraction(1, 2**24)) == 0x3F800000
    assert round32(Fraction(1) + Fraction(3, 2**24)) == 0x3F800002
    assert round32(Fraction(1, 2**150)) == 0
    assert round32(Fraction(3, 2**150)) == 2
    assert round32(-Fraction(1, 2**151)) == 0x80000000
    assert round32(Fraction(2**128 - 2**103)) == 0x7F800000
    assert round32(Fraction(2**128 - 2**103 - 1)) == 0x7F7FFFFF


def test_finite_encodings_round_trip():
    rng = random.Random(42)
    for _ in range(200):
        w = rng.getrandbits(32)
        if w & 0x7F800000 != 0x7F800000:
            assert round32(value(w), bool(w & 0x80000000)) == w


def test_round_once_and_fma_cancellation():
    assert reference(dict(op="sum", x=[0x3F800000, 0x33800000, 0x33800000])) == [0x3F800001]
    # (1 + 2^-23) * (1 - 2^-23) - 1 = -2^-46, not zero.
    assert reference(
        dict(op="scaleShift", dims=[1, 1], x=[0x3F800001], w=[0x3F7FFFFE], b=[0xBF800000])
    ) == [0xA8800000]


def test_sqrt_uses_exact_midpoint_comparison():
    assert ORACLE["sqrt32"](0x40800000) == 0x40000000
    assert ORACLE["sqrt32"](0x40000000) == 0x3FB504F3


def test_normalization_statistics_do_not_overflow_before_division():
    assert (
        reference(dict(op="rmsnorm", dims=[1, 2], x=[0x5F800000] * 2, w=[0x3F800000] * 2))
        == [0x3F800000] * 2
    )
    assert reference(
        dict(op="layernorm", dims=[1, 2], x=[0x7F7FFFFF] * 2, w=[0x3F800000] * 2, b=[0, 0])
    ) == [0, 0]


def test_asymmetric_convolution_has_hand_calculated_outputs():
    def words(xs):
        return [round32(Fraction(x)) for x in xs]

    actual = reference(
        dict(
            op="conv2d",
            dims=[1, 3, 5, 1, 2, 3, 1, 0],
            x=words(range(1, 16)),
            w=words(range(1, 7)),
            b=words([1]),
        )
    )
    assert actual == words([122, 143, 164, 227, 248, 269])


def test_special_values_and_nan_payload_selection():
    maximum = ORACLE["maximum"]
    assert maximum(0x80000000, 0) == 0
    assert maximum(0x80000000, 0x80000000) == 0x80000000
    assert maximum(0xFF800000, 0x7F800000) == 0x7F800000
    assert maximum(0x7FC00007, 0xFF800123) == 0xFFC00123
    # Subtraction flips the right operand before FloatLib selects/quietens a NaN.
    assert ORACLE["subtract"](0x3F800000, 0x7FC00007) == 0xFFC00007
    assert ORACLE["divide"](0x80000000, 0x3F800000) == 0x80000000
    assert ORACLE["divide"](0, 0) == 0x7FC00000
    assert ORACLE["sqrt32"](0x80000000) == 0x80000000
    assert ORACLE["sqrt32"](0xBF800000) == 0x7FC00000


def test_average_pooling_keeps_the_specified_two_rounding_steps():
    # The true average is max finite. The specified rounded SUM overflows first.
    assert reference(dict(op="avgpool", dims=[1, 2, 2, 2], x=[0x7F7FFFFF] * 4)) == [0x7F800000]


def test_shape_audit_detects_wiring_and_parameter_errors():
    audit = runpy.run_path(str(Path(__file__).parents[1] / "scripts/audit-exact-models.py"))
    step = audit["shape_step"]
    layer = dict(
        op="linear",
        dims=[2, 3, 4],
        weights=12,
        bias=4,
        parameters_finite=True,
        valid=True,
        output=8,
        saved_after=5,
    )
    assert step(layer, (6, 5)) == (8, 5)
    for bad, shape in [
        (dict(layer, weights=11), (6, 5)),
        (layer, (7, 5)),
        (dict(layer, saved_after=0), (6, 5)),
    ]:
        with pytest.raises(AssertionError):
            step(bad, shape)
