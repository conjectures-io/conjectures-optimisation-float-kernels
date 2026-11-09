"""Pure-Python checks of the oracle's correctly rounded activations and recurrent layers.

No Lean: the activations are compared with host float64 `math.tanh` / `1/(1+exp(-x))` and with
hand-derived roundings; the LSTM/GRU layouts with a float64 re-implementation of PyTorch's
equations, and exactly on gate-saturating cases where every rounding is known.
"""

import math
import random
import runpy
import struct
from fractions import Fraction
from pathlib import Path

ORACLE = runpy.run_path(str(Path(__file__).parents[1] / "scripts/audit-exact-spec.py"))
round32 = ORACLE["round32"]
value = ORACLE["value"]
reference = ORACLE["reference"]
valid_layer = ORACLE["valid_layer"]
sigmoid32 = ORACLE["sigmoid32"]
tanh32 = ORACLE["tanh32"]
dot = ORACLE["dot"]
NAN, INF, ONE, SIGN, HALF, MAX = (
    0x7FC00000,
    0x7F800000,
    0x3F800000,
    0x80000000,
    0x3F000000,
    0x7F7FFFFF,
)
HUGE = 0x7149F2CA  # 1e30: saturates any gate.


def word(x):
    return round32(Fraction(x))


def f32(x):
    """A float64 rounded to binary32 by the host (ties to even), as a word."""
    return struct.unpack("<I", struct.pack("<f", x))[0]


def ordinal(w):
    """Monotone integer order of non-NaN words, with -0 and +0 adjacent."""
    return -(w & ~SIGN) if w & SIGN else w


def host_check(fn32, fn64, w):
    """Exact agreement with the host's float64 result rounded to binary32, unless that result
    lies within 1e-4 ulp of a binary32 midpoint (where float64 error could decide); then 1 ulp."""
    x = float(value(w))
    y = fn64(x)
    got = fn32(w)
    nearest = f32(y)
    lo = (
        nearest
        if struct.unpack("<f", struct.pack("<I", nearest & ~SIGN))[0] <= abs(y)
        else nearest - 1
    )
    a, b = (struct.unpack("<f", struct.pack("<I", v))[0] for v in (lo & ~SIGN, (lo & ~SIGN) + 1))
    if abs((abs(y) - a) / (b - a) - 0.5) > 1e-4:
        assert got == nearest, (hex(w), x, hex(got), hex(nearest))
    else:
        assert abs(ordinal(got) - ordinal(nearest)) <= 1, (hex(w), hex(got), hex(nearest))


def host_sigmoid(x):
    return 1 / (1 + math.exp(-x)) if x > -700 else 0.0


def random_words(rng, n, lo, hi):
    out = []
    for _ in range(n):
        if rng.random() < 0.5:
            x = rng.uniform(lo, hi)
        else:  # Log-uniform magnitudes, down to subnormal arguments.
            x = math.copysign(2 ** rng.uniform(-149, math.log2(max(-lo, hi))), rng.random() - 0.5)
            x = min(max(x, lo), hi)
        out.append(f32(x))
    return out


def test_activation_specials():
    for fn in (sigmoid32, tanh32):
        assert fn(0x7F800001) == 0x7FC00001  # Signalling NaN quieted, payload kept.
        assert fn(0xFF800123) == 0xFFC00123  # Sign kept.
        assert fn(NAN | 7) == NAN | 7
    assert sigmoid32(INF) == ONE
    assert sigmoid32(INF | SIGN) == 0
    assert sigmoid32(0) == sigmoid32(SIGN) == HALF
    assert tanh32(INF) == ONE
    assert tanh32(INF | SIGN) == ONE | SIGN
    assert tanh32(0) == 0
    assert tanh32(SIGN) == SIGN


def test_activation_known_roundings():
    assert sigmoid32(ONE) == 0x3F3B26A8
    assert tanh32(ONE) == 0x3F42F7D6
    # σ(x) = 1/2 + x/4 - x^3/48 + …: at x = 2^-23, just below the midpoint 1/2 + 2^-25.
    assert sigmoid32(0x34000000) == HALF
    # At x = -2^-24, just above the midpoint 1/2 - 2^-26 (the ulp below 1/2 is 2^-25).
    assert sigmoid32(0xB3800000) == HALF
    # tanh x = x - x^3/3 + …: 2^-12 loses a third of its lower ulp (rounds back to 2^-12),
    # 2^-11 loses 4/3 of an ulp (rounds to the next word down).
    assert tanh32(0x39800000) == 0x39800000
    assert tanh32(0x3A000000) == 0x39FFFFFF
    assert tanh32(0x30800000) == 0x30800000  # 2^-30
    for w in (1, 0x7FFFFF, 0x800000):  # Subnormal and least normal arguments are fixed points.
        assert tanh32(w) == w and tanh32(w | SIGN) == w | SIGN
    # tanh rounds to 1 only above 13·ln 2 ≈ 9.0109; tanh 9 is the word below 1.
    assert tanh32(word(9)) == 0x3F7FFFFF
    assert tanh32(word(Fraction(9011, 1000))) == ONE
    assert tanh32(word(1e10)) == ONE and tanh32(MAX | SIGN) == ONE | SIGN
    # sigmoid underflows gradually: e^-100 ≈ 27 · 2^-149; +0 below -150·ln 2 ≈ -103.97.
    assert sigmoid32(word(-100)) == 0x1B
    assert sigmoid32(word(-103.9)) == 1
    assert sigmoid32(word(-104)) == 0
    assert sigmoid32(MAX | SIGN) == 0
    assert sigmoid32(word(17.3)) == 0x3F7FFFFF
    assert sigmoid32(word(17.4)) == ONE


def test_activations_match_host_float64():
    rng = random.Random(7)
    for w in random_words(rng, 3000, -25.0, 25.0):
        host_check(tanh32, math.tanh, w)
        host_check(sigmoid32, host_sigmoid, w)
    for w in random_words(rng, 1000, -110.0, -80.0):  # Subnormal and zero sigmoids.
        host_check(sigmoid32, host_sigmoid, w)


def test_tanh_is_odd_and_both_are_monotone():
    rng = random.Random(11)
    ws = random_words(rng, 1500, -30.0, 30.0) + [0, SIGN, 1, SIGN | 1, MAX, MAX | SIGN]
    for w in ws:
        assert tanh32(w ^ SIGN) == tanh32(w) ^ SIGN
    ordered = sorted(ws, key=ordinal)
    for fn in (sigmoid32, tanh32):
        outs = [ordinal(fn(w)) for w in ordered]
        assert outs == sorted(outs)
    # Bounds: |tanh| ≤ 1 and 0 ≤ σ ≤ 1 everywhere.
    assert all(value(tanh32(w)) ** 2 <= 1 and 0 <= value(sigmoid32(w)) <= 1 for w in ws)


def test_silu_uses_the_correctly_rounded_sigmoid():
    xs = [ONE, word(-3), word(20), 0x34000000, INF, INF | SIGN, SIGN, 0x7F800001]
    expected = [ORACLE["mul"](x, sigmoid32(x)) for x in xs]
    assert reference(dict(op="silu", x=xs)) == expected
    assert expected[0] == 0x3F3B26A8
    assert expected[4:] == [INF, NAN, SIGN, 0x7FC00001]  # -inf · +0 is NaN.


def sig64(x):
    return 1 / (1 + math.exp(-x)) if x > -700 else 0.0


def lstm64(steps, inner, hidden, x, wih, whh, bih, bhh):
    h, c, out = [0.0] * hidden, [0.0] * hidden, []
    for t in range(steps):
        xt = x[t * inner : (t + 1) * inner]

        def pre(k, h=h, xt=xt):
            return (
                sum(wih[k * inner + j] * xt[j] for j in range(inner))
                + sum(whh[k * hidden + j] * h[j] for j in range(hidden))
                + bih[k]
                + bhh[k]
            )

        g = [pre(k) for k in range(4 * hidden)]
        i, f = [sig64(v) for v in g[:hidden]], [sig64(v) for v in g[hidden : 2 * hidden]]
        gg, o = (
            [math.tanh(v) for v in g[2 * hidden : 3 * hidden]],
            [sig64(v) for v in g[3 * hidden :]],
        )
        c = [f[u] * c[u] + i[u] * gg[u] for u in range(hidden)]
        h = [o[u] * math.tanh(c[u]) for u in range(hidden)]
        out += h
    return out


def gru64(steps, inner, hidden, x, wih, whh, bih, bhh):
    h, out = [0.0] * hidden, []
    for t in range(steps):
        xt = x[t * inner : (t + 1) * inner]

        def wx(k, xt=xt):
            return sum(wih[k * inner + j] * xt[j] for j in range(inner)) + bih[k]

        def wh(k, h=h):
            return sum(whh[k * hidden + j] * h[j] for j in range(hidden)) + bhh[k]

        r = [sig64(wx(u) + wh(u)) for u in range(hidden)]
        z = [sig64(wx(hidden + u) + wh(hidden + u)) for u in range(hidden)]
        n = [math.tanh(wx(2 * hidden + u) + r[u] * wh(2 * hidden + u)) for u in range(hidden)]
        h = [(1 - z[u]) * n[u] + z[u] * h[u] for u in range(hidden)]
        out += h
    return out


def test_recurrent_layers_match_float64_pytorch_equations():
    rng = random.Random(3)
    for trial in range(60):
        steps, inner, hidden = rng.randrange(1, 5), rng.randrange(1, 4), rng.randrange(1, 4)
        for op, gates, model in (("lstm", 4, lstm64), ("gru", 3, gru64)):
            n_ih, n_hh, n_b = gates * hidden * inner, gates * hidden * hidden, gates * hidden
            scale = rng.choice([1, 4])
            ws = [word(Fraction(rng.randrange(-100, 101), 32) * scale) for _ in range(n_ih + n_hh)]
            bs = [word(Fraction(rng.randrange(-100, 101), 32)) for _ in range(2 * n_b)]
            xs = [word(Fraction(rng.randrange(-100, 101), 16)) for _ in range(steps * inner)]
            request = dict(op=op, dims=[steps, inner, hidden], x=xs, w=ws, b=bs)
            assert valid_layer(request)
            got = [float(value(v)) for v in reference(request)]
            f = [float(value(v)) for v in ws + bs + xs]
            want = model(
                steps,
                inner,
                hidden,
                f[n_ih + n_hh + 2 * n_b :],
                f[:n_ih],
                f[n_ih : n_ih + n_hh],
                f[n_ih + n_hh : n_ih + n_hh + n_b],
                f[n_ih + n_hh + n_b : n_ih + n_hh + 2 * n_b],
            )
            assert len(got) == steps * hidden
            for a, b in zip(got, want):
                assert abs(a - b) <= 1e-5, (op, trial, got, want)


def gate_biases(hidden, gates, levels):
    """`bih` with every unit of gate block `k` at `levels[k]`; `bhh` zero."""
    return [levels[k] for k in range(gates) for _ in range(hidden)] + [0] * (gates * hidden)


def test_lstm_gate_order_with_saturated_gates():
    # i = 1, f = 0, o = 1 by saturation; g = tanh(bias) exactly, so c = g and h = tanh(g).
    g_bias = word(0.5)
    b = gate_biases(1, 4, [HUGE, HUGE | SIGN, g_bias, HUGE])
    out = reference(dict(op="lstm", dims=[2, 1, 1], x=[ONE, ONE], w=[0] * 8, b=b))
    c = tanh32(g_bias)
    assert out == [tanh32(c)] * 2
    # Forget gate open, input gate shut: the cell keeps its +0 start, h stays +0.
    b = gate_biases(1, 4, [HUGE | SIGN, HUGE, g_bias, HUGE])
    assert reference(dict(op="lstm", dims=[3, 1, 1], x=[ONE] * 3, w=[0] * 8, b=b)) == [0] * 3
    # Both open: the cell accumulates g each step, one exact rounding per step.
    b = gate_biases(1, 4, [HUGE, HUGE, g_bias, HUGE])
    out = reference(dict(op="lstm", dims=[3, 1, 1], x=[ONE] * 3, w=[0] * 8, b=b))
    cells = [dot([ONE, ONE], [c, tanh32(g_bias)]) for c in (0,)]
    cells.append(dot([ONE, ONE], [cells[-1], tanh32(g_bias)]))
    cells.append(dot([ONE, ONE], [cells[-1], tanh32(g_bias)]))
    assert out == [tanh32(v) for v in cells]


def test_gru_gate_order_with_saturated_gates():
    # z = 1 keeps h at its +0 start.
    b = gate_biases(1, 3, [0, HUGE, word(0.25)])
    assert reference(dict(op="gru", dims=[2, 1, 1], x=[ONE] * 2, w=[0] * 6, b=b)) == [0, 0]
    # z = 0 and r = 1: h' = n = tanh(w_in·x + b_in + (w_hn·h + b_hn)), with b_hn = 1/8.
    w = [0, 0, word(2), 0, 0, word(-0.5)]  # w_in = 2; w_hn = -1/2.
    bias = [HUGE, HUGE | SIGN, word(0.25), 0, 0, word(0.125)]
    out = reference(dict(op="gru", dims=[2, 1, 1], x=[ONE, word(-1)], w=w, b=bias))
    hn0 = word(0.125)  # -1/2 · 0 + 1/8
    h1 = tanh32(dot([ONE, ONE, hn0], [word(2), word(0.25), ONE]))
    hn1 = dot([h1, ONE], [word(-0.5), word(0.125)])
    h2 = tanh32(dot([word(-1), ONE, hn1], [word(2), word(0.25), ONE]))
    assert out == [h1, h2]
    # r = 0 drops the recurrent candidate term entirely.
    bias[0] = HUGE | SIGN
    out = reference(dict(op="gru", dims=[1, 1, 1], x=[ONE], w=w, b=bias))
    assert out == [tanh32(word(2.25))]


def test_recurrent_signed_zeros():
    # Every pre-activation is -0 · 1 + h·w + -0 + -0 with h = +0: all-negative-zero only when
    # the recurrent product is -0 too (w_hh = -1).
    for op, gates in (("lstm", 4), ("gru", 3)):
        req = dict(op=op, dims=[1, 1, 1], x=[SIGN], w=[ONE] * gates + [ONE | SIGN] * gates)
        req["b"] = [SIGN] * (2 * gates)
        assert reference(req) == [0]  # c' = 1/2·(+0) + 1/2·tanh(-0) = +0; GRU: n - z·n + z·h.
    # Zero parameters keep h = +0.
    assert (
        reference(dict(op="lstm", dims=[2, 2, 2], x=[ONE] * 4, w=[0] * 32, b=[0] * 16)) == [0] * 4
    )
    assert reference(dict(op="gru", dims=[2, 2, 2], x=[ONE] * 4, w=[0] * 24, b=[0] * 12)) == [0] * 4


def test_recurrent_nan_payloads_propagate():
    # A signalling NaN input reaches every gate; the first one in the dot is quieted.
    out = reference(dict(op="lstm", dims=[1, 1, 1], x=[0x7F800005], w=[ONE] * 8, b=[0] * 8))
    assert out == [0x7FC00005]
    out = reference(dict(op="gru", dims=[1, 1, 1], x=[0x7F800005], w=[ONE] * 6, b=[0] * 6))
    assert out == [0x7FC00005]


def test_last_step_follows_lean_extract():
    x = [word(i + 1) for i in range(7)]
    assert reference(dict(op="lastStep", dims=[3, 2], x=x[:6])) == x[4:6]
    assert reference(dict(op="lastStep", dims=[0, 3], x=x)) == []  # (0 - 1)·3 = 0 in Nat.
    assert reference(dict(op="lastStep", dims=[3, 2], x=x[:5])) == x[4:5]  # Clamped.
    assert reference(dict(op="lastStep", dims=[5, 2], x=x[:3])) == []
    assert reference(dict(op="lastStep", dims=[1, 7], x=x)) == x


def test_checked_recurrent_validity():
    lstm = dict(op="lstm", dims=[2, 3, 2], x=[ONE] * 6, w=[ONE] * 40, b=[0] * 16)
    gru = dict(op="gru", dims=[2, 3, 2], x=[ONE] * 6, w=[ONE] * 30, b=[0] * 12)
    assert valid_layer(lstm) and valid_layer(gru)
    assert not valid_layer(dict(lstm, w=gru["w"], b=gru["b"]))
    assert not valid_layer(dict(lstm, dims=[3, 2, 3]))
    for base in (lstm, gru):
        for key in ("x", "w", "b"):
            assert not valid_layer(dict(base, **{key: base[key][:-1]}))
            assert not valid_layer(dict(base, **{key: base[key] + [0]}))
        for i in range(3):
            dims = list(base["dims"])
            dims[i] = 0
            assert not valid_layer(dict(base, dims=dims))
        checked = dict(base, op="checked:" + base["op"])
        assert reference(checked) == [0] + reference(base)
        assert reference(dict(checked, x=[ONE] * 5)) == [1]
    assert reference(dict(op="checked:lastStep", dims=[3, 2], x=[ONE] * 5)) == [1]
    assert reference(dict(op="checked:lastStep", dims=[0, 2], x=[])) == [1]
    assert reference(dict(op="checked:lastStep", dims=[2, 1], x=[ONE, 0])) == [0, 0]


def test_case_generators_cover_the_recurrent_family():
    ops = {r["op"] for r in ORACLE["cases"](5, 2) + ORACLE["edge_cases"](5)}
    assert {"sigmoid", "tanh", "silu"} <= ops
    for op in ("lstm", "gru", "lastStep"):
        assert {op, "checked:" + op} <= ops


def test_near_midpoint_inputs_agree_with_fixed_high_precision():
    """The inputs whose results lie nearest a rounding midpoint (where a float64 estimate can
    misround), against 150-digit evaluations of other formulas: tanh a = (e^(2a) - 1)/(e^(2a) + 1)
    and σ(x) = e^x / (1 + e^x)."""
    from decimal import Decimal, localcontext

    rng = random.Random(19)
    hard = ORACLE["hard_activation_words"]
    with localcontext() as context:
        context.prec = 150
        for w in hard(rng, "tanh", 2.0**-14, 9.2, 40):
            e = context.exp(Decimal(float(2 * value(w))))
            assert tanh32(w) == round32(Fraction((e - 1) / (e + 1))), hex(w)
            assert tanh32(w | SIGN) == tanh32(w) | SIGN
        for w in hard(rng, "sigmoid", -104, 18, 40):
            e = context.exp(Decimal(float(value(w))))
            assert sigmoid32(w) == round32(Fraction(e / (1 + e))), hex(w)
