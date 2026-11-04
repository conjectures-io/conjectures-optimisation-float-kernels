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
valid_layer = ORACLE["valid_layer"]
conv_dims = ORACLE["conv_dims"]
pool_dims = ORACLE["pool_dims"]
NAN, INF, ONE, SIGN = 0x7FC00000, 0x7F800000, 0x3F800000, 0x80000000


def words(xs):
    return [round32(Fraction(x)) for x in xs]


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
    actual = reference(
        dict(
            op="conv2d",
            dims=conv_dims(1, 3, 5, 1, 2, 3),
            x=words(range(1, 16)),
            w=words(range(1, 7)),
            b=words([1]),
        )
    )
    assert actual == words([122, 143, 164, 227, 248, 269])


def test_grouped_dilated_convolution_has_hand_calculated_outputs():
    # Two 1-D groups of one channel each; the filter's two taps are two columns apart.
    actual = reference(
        dict(
            op="conv2d",
            dims=conv_dims(2, 1, 5, 2, 1, 2, dil=(1, 2), groups=2),
            x=words([1, 2, 3, 4, 5, 10, 20, 30, 40, 50]),
            w=words([1, 100, 1, -1]),
            b=words([0, Fraction(1, 2)]),
        )
    )
    assert actual == words([301, 402, 503, -19.5, -19.5, -19.5])


def test_padded_strided_convolution_has_hand_calculated_outputs():
    # Stride 2 down, 1 across; one padding row on top and one padding column on the right.
    actual = reference(
        dict(
            op="conv2d",
            dims=conv_dims(1, 3, 3, 1, 2, 2, stride=(2, 1), pad=(1, 0, 0, 1)),
            x=words(range(1, 10)),
            w=words([1, 2, 3, 4]),
            b=words([0]),
        )
    )
    assert actual == words([11, 18, 9, 67, 77, 33])


def test_convolution_padding_is_a_zero_term():
    dims = conv_dims(1, 1, 1, 1, 1, 1, pad=(1, 0, 0, 0))
    # The padded output multiplies +0 by the weight: 0 * inf is NaN.
    assert reference(dict(op="conv2d", dims=dims, x=[ONE], w=[INF], b=[0])) == [NAN, INF]
    # +0 * 1 + -0 is +0, though the bias is -0.
    assert reference(dict(op="conv2d", dims=dims, x=[SIGN], w=[ONE], b=[SIGN])) == [0, SIGN]


def test_depthwise_ignores_requested_output_channels_and_groups():
    request = dict(op="depthwise", x=words([1, 2, 3, 4]), w=words([2, 3]), b=words([0, 1]))
    for cout, groups in [(2, 2), (0, 0), (7, 3)]:
        request["dims"] = conv_dims(2, 1, 2, cout, 1, 1, groups=groups)
        assert valid_layer(request)
        assert reference(request) == words([2, 4, 10, 13])


def test_transposed_convolution_sums_only_contributing_taps():
    dims = conv_dims(1, 1, 3, 1, 1, 2, stride=(1, 3), out_pad=0)
    # Columns 2 and 5 receive no tap: just the bias, here -0, with no +0 term to flip it.
    assert reference(
        dict(op="convTranspose", dims=dims, x=words([1, 2, 3]), w=words([10, 1]), b=[SIGN])
    ) == words([10, 1]) + [SIGN] + words([20, 2]) + [SIGN] + words([30, 3])
    # Absent taps never form 0 * inf.
    assert reference(dict(op="convTranspose", dims=dims, x=[0] * 3, w=[INF] * 2, b=[ONE])) == [
        NAN,
        NAN,
        ONE,
        NAN,
        NAN,
        ONE,
        NAN,
        NAN,
    ]
    # Output padding adds bias-only columns; padding crops from the left.
    cropped = conv_dims(1, 1, 3, 1, 1, 2, stride=(1, 3), pad=(0, 1, 0, 0), out_pad=(0, 2))
    assert reference(
        dict(op="convTranspose", dims=cropped, x=words([1, 2, 3]), w=words([10, 1]), b=[SIGN])
    ) == words([1]) + [SIGN] + words([20, 2]) + [SIGN] + words([30, 3]) + [SIGN, SIGN]


def test_grouped_transposed_convolution_has_hand_calculated_outputs():
    # Weights are cin x (cout / groups) x kh x kw: input channel c feeds outputs 2c and 2c + 1.
    actual = reference(
        dict(
            op="convTranspose",
            dims=conv_dims(2, 1, 2, 4, 1, 2, groups=2, out_pad=0),
            x=words([1, 10, 100, 1000]),
            w=words(range(1, 9)),
            b=words([0] * 4),
        )
    )
    assert actual == words([1, 12, 20, 3, 34, 40, 500, 5600, 6000, 700, 7800, 8000])


def test_pooling_output_size_and_divisor_rules():
    x = words([1, 2, 3, 4, 5])

    def pool(op, pad, ceil, count_include_pad, values=x):
        dims = pool_dims(
            1, 1, 5, 1, 2, stride=(1, 2), pad=pad, ceil=ceil, count_include_pad=count_include_pad
        )
        request = dict(op=op, dims=dims, x=values)
        assert valid_layer(request)
        return reference(request)

    for cip in (False, True):
        assert pool("avgpool", 0, False, cip) == words([1.5, 3.5])
        # Ceil mode keeps the last, partial window.
        assert pool("avgpool", 0, True, cip) == words([1.5, 3.5, 5])
    # A trailing padding column completes the last window; only count_include_pad counts it.
    assert pool("avgpool", (0, 0, 0, 1), False, True) == words([1.5, 3.5, 2.5])
    assert pool("avgpool", (0, 0, 0, 1), False, False) == words([1.5, 3.5, 5])
    # Ceil mode drops a window that would start in the trailing padding.
    assert pool("avgpool", (0, 1, 0, 1), True, False) == words([1, 2.5, 4.5])
    assert pool("avgpool", (0, 1, 0, 1), True, True) == words([0.5, 2.5, 4.5])
    # Padding is not a value for maximum pooling.
    assert pool("maxpool", (0, 1, 0, 1), True, False, words([-1, -2, -3, -4, -5])) == words(
        [-1, -2, -4]
    )
    glob = dict(op="avgpool", dims=pool_dims(1, 2, 3, 2, 3, stride=1), x=words(range(1, 7)))
    assert reference(glob) == words([3.5])


def test_maxpool_folds_the_first_cell_twice():
    one = pool_dims(1, 1, 1, 1, 1)
    assert reference(dict(op="maxpool", dims=one, x=[0x7F800001])) == [0x7FC00001]
    pair = pool_dims(1, 1, 2, 1, 2)
    for window, expected in [([SIGN, 0], 0), ([0, SIGN], 0), ([SIGN, SIGN], SIGN)]:
        assert reference(dict(op="maxpool", dims=pair, x=window)) == [expected]


def test_generalised_shape_validity_rules():
    def ok(op, dims):
        nx, nw, nb = ORACLE["layer_sizes"](op, dims)
        return valid_layer(dict(op=op, dims=dims, x=[0] * nx, w=[0] * nw, b=[0] * nb))

    assert ok("conv2d", conv_dims(4, 3, 3, 6, 2, 2, groups=2))
    assert not ok("conv2d", conv_dims(4, 3, 3, 6, 2, 2, groups=4))  # 6 % 4 != 0
    assert ok("conv2d", conv_dims(1, 3, 3, 1, 2, 2, dil=2, pad=(0, 0, 0, 0)))
    assert not ok("conv2d", conv_dims(1, 2, 2, 1, 2, 2, dil=2))  # Dilated filter is 3 wide.
    # Output padding must be below the stride or the dilation.
    assert ok("convTranspose", conv_dims(1, 2, 2, 1, 2, 2, stride=2, out_pad=1))
    assert ok("convTranspose", conv_dims(1, 2, 2, 1, 2, 2, dil=3, out_pad=2))
    assert not ok("convTranspose", conv_dims(1, 2, 2, 1, 2, 2, stride=2, dil=2, out_pad=2))
    # At least one output must survive the cropping (full size 3 here).
    assert ok("convTranspose", conv_dims(1, 2, 2, 1, 2, 2, pad=(2, 2, 0, 0), out_pad=0))
    assert not ok("convTranspose", conv_dims(1, 2, 2, 1, 2, 2, pad=(2, 2, 1, 0), out_pad=0))
    # Pooling padding must be smaller than the window.
    assert ok("maxpool", pool_dims(1, 3, 3, 2, 2, pad=1))
    assert not ok("maxpool", pool_dims(1, 3, 3, 2, 2, pad=(0, 0, 2, 0)))
    assert not ok("avgpool", pool_dims(1, 2, 2, 4, 1, pad=(1, 0, 0, 0)))  # Taller than 3 rows.


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
    assert reference(dict(op="avgpool", dims=pool_dims(1, 2, 2, 2, 2), x=[0x7F7FFFFF] * 4)) == [
        0x7F800000
    ]


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
