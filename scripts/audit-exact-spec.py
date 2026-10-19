#!/usr/bin/env python3
"""Independent, developer-only audit of ContractExact; no miner code, DB or chain.

Uses rational arithmetic and a search over binary32 encodings, rather than FloatLib's
dyadic/limb algorithms. Run after setup: .venv/bin/python scripts/audit-exact-spec.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import time
from collections import Counter
from fractions import Fraction as Q
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGN = 0x80000000
INF = 0x7F800000
ONE = 0x3F800000
NAN = 0x7FC00000
MAX = INF - 1


def isnan(w):
    return w & ~SIGN > INF


def value(w):
    """Finite word as an exact rational, decoded directly from IEEE fields."""
    e, f = (w >> 23) & 255, w & 0x7FFFFF
    assert e != 255, f"nonfinite value {w:08x}"
    m, power = (f, -149) if e == 0 else ((1 << 23) + f, e - 150)
    result = Q(m << power) if power >= 0 else Q(m, 1 << -power)
    return -result if w & SIGN else result


def round32(q, negative_zero=False):
    """Round a rational once, ties to even, including gradual under/overflow."""
    if not q:
        return SIGN if negative_zero else 0
    sign = SIGN if q < 0 else 0
    q = abs(q)
    # Overflow midpoint: halfway between max finite and the next power of two.
    if q >= Q((1 << 128) - (1 << 103)):
        return sign | INF
    lo, hi = 0, MAX
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if value(mid) <= q:
            lo = mid
        else:
            hi = mid - 1
    if lo == MAX:
        return sign | lo
    midpoint = (value(lo) + value(lo + 1)) / 2
    return sign | (lo + int(q > midpoint or (q == midpoint and lo % 2 == 1)))


def dot(a, b):
    if len(a) != len(b):
        return 0
    words = [w for pair in zip(a, b) for w in pair]
    nans = [w for w in words if isnan(w)]
    if nans:
        signaling = [w for w in nans if not w & 0x400000]
        return (signaling or nans)[0] | 0x400000
    infinities = set()
    total = Q(0)
    all_negative_zero = bool(a)
    for x, y in zip(a, b):
        if x & ~SIGN == INF or y & ~SIGN == INF:
            if x & ~SIGN == 0 or y & ~SIGN == 0:
                return NAN
            infinities.add(bool((x ^ y) & SIGN))
        else:
            product = value(x) * value(y)
            total += product
            all_negative_zero &= product == 0 and bool((x ^ y) & SIGN)
    if len(infinities) == 2:
        return NAN
    if infinities:
        return INF | (SIGN if True in infinities else 0)
    return round32(total, all_negative_zero)


def add(a, b):
    return dot([a, b], [ONE, ONE])


def mul(a, b):
    return dot([a], [b])


def subtract(a, b):
    # FloatLib defines subtraction as addition after negating the right operand,
    # including its NaN sign bit. Payload/sign behavior is part of the pinned spec.
    return add(a, b ^ SIGN)


def divide(a, b):
    selected = choose_nan(a, b)
    if selected is not None:
        return selected
    aa, bb = a & ~SIGN, b & ~SIGN
    sign = (a ^ b) & SIGN
    if (aa == INF and bb == INF) or (aa == 0 and bb == 0):
        return NAN
    if aa == INF or bb == 0:
        return sign | INF
    if aa == 0 or bb == INF:
        return sign
    return round32(value(a) / value(b), bool((a ^ b) & SIGN))


def choose_nan(*words):
    nans = [w for w in words if isnan(w)]
    signaling = [w for w in nans if not w & 0x400000]
    return ((signaling or nans)[0] | 0x400000) if nans else None


def maximum(a, b):
    selected = choose_nan(a, b)
    if selected is not None:
        return selected
    if a & ~SIGN == 0 and b & ~SIGN == 0:
        return (a & b) & SIGN

    # Monotone integer ordering of all non-NaN encodings, including infinities.
    def key(w):
        return ~w if w & SIGN else w | SIGN

    return a if key(a) >= key(b) else b


def sqrt_rational(q):
    """Correctly rounded sqrt by comparing rational squares, without host sqrt."""
    assert q >= 0
    if q >= Q((1 << 128) - (1 << 103)) ** 2:
        return INF
    lo, hi = 0, MAX
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if value(mid) ** 2 <= q:
            lo = mid
        else:
            hi = mid - 1
    if lo == MAX:
        return MAX
    midpoint = (value(lo) + value(lo + 1)) / 2
    return lo + int(q > midpoint**2 or (q == midpoint**2 and lo % 2 == 1))


def sqrt32(w):
    if isnan(w):
        return w | 0x400000
    if w & ~SIGN == 0 or w == INF:
        return w
    if w & SIGN:
        return NAN
    return sqrt_rational(value(w))


def activate(w, relu):
    return 0 if relu and w & SIGN and not isnan(w) else w


def reference(r):
    op = r["op"]
    d, x, w, b = (r.get(k, []) for k in ("dims", "x", "w", "b"))
    relu = r.get("relu", False)
    if op == "dot":
        return [dot(x, w)]
    if op == "sum":
        return [dot(x, [ONE] * len(x))]
    if op == "save":
        return x
    if op == "sqrt":
        return [sqrt_rational(Q(d[0], d[1]))]
    if op.startswith("checked:"):
        raw = dict(r, op=op.removeprefix("checked:"))
        return [0] + reference(raw) if valid_layer(raw) else [1]
    if op == "checkedConv":
        ci, h, wi, co, kh, kw, stride, pad = d
        valid = (
            min(ci, h, wi, co, kh, kw, stride) > 0
            and h + 2 * pad >= kh
            and wi + 2 * pad >= kw
            and len(x) == ci * h * wi
            and len(w) == co * ci * kh * kw
            and len(b) == co
        )
        return [0] + reference(dict(r, op="conv2d")) if valid else [1]
    if op == "checkedNorm":
        rows, n = d
        valid = (
            rows > 0
            and n > 0
            and len(x) == rows * n
            and len(w) == n
            and all(a & ~SIGN < INF for a in x + w + [r["eps"]])
            and value(r["eps"]) > 0
        )
        return [0] + reference(dict(r, op="rmsnorm")) if valid else [1]
    if op == "checkedModel":
        valid = len(x) == 4 and len(w) == 4 and len(b) == 2
        return [0] + reference(dict(r, op="model")) if valid else [1]
    if op == "linear":
        rows, inner, outputs = d
        return [
            activate(
                dot(
                    x[i * inner : (i + 1) * inner] + [ONE], w[j * inner : (j + 1) * inner] + [b[j]]
                ),
                relu,
            )
            for i in range(rows)
            for j in range(outputs)
        ]
    if op == "gemm":
        batches, rows, inner, cols = d
        return [
            dot(
                [x[(batch * rows + i) * inner + t] for t in range(inner)],
                [w[(batch * inner + t) * cols + j] for t in range(inner)],
            )
            for batch in range(batches)
            for i in range(rows)
            for j in range(cols)
        ]
    if op in ("conv2d", "depthwise"):
        cin, h, width, cout, kh, kw, stride, pad = d
        ho, wo = (h + 2 * pad - kh) // stride + 1, (width + 2 * pad - kw) // stride + 1
        out = []
        for channel in range(cin if op == "depthwise" else cout):
            for oy in range(ho):
                for ox in range(wo):
                    inputs, weights = [], []
                    channels = [channel] if op == "depthwise" else range(cin)
                    for ic in channels:
                        for dy in range(kh):
                            for dx in range(kw):
                                iy, ix = oy * stride + dy - pad, ox * stride + dx - pad
                                inputs.append(
                                    x[(ic * h + iy) * width + ix]
                                    if 0 <= iy < h and 0 <= ix < width
                                    else 0
                                )
                                wi = channel if op == "depthwise" else channel * cin + ic
                                weights.append(w[(wi * kh + dy) * kw + dx])
                    out.append(activate(dot(inputs + [ONE], weights + [b[channel]]), relu))
        return out
    if op in ("avgpool", "maxpool"):
        channels, h, width, k = d
        out = []
        for channel in range(channels):
            for oy in range(h // k):
                for ox in range(width // k):
                    window = [
                        x[(channel * h + oy * k + dy) * width + ox * k + dx]
                        for dy in range(k)
                        for dx in range(k)
                    ]
                    if op == "avgpool":
                        out.append(divide(dot(window, [ONE] * len(window)), round32(Q(k * k))))
                    else:
                        acc = window[0]
                        for word in window:
                            acc = maximum(acc, word)
                        out.append(acc)
        return out
    if op == "addRelu":
        return [activate(add(a, bb), relu) for a, bb in zip(x, w)]
    if op == "scaleShift":
        channels, hw = d
        return [activate(dot([a, ONE], [w[i // hw], b[i // hw]]), relu) for i, a in enumerate(x)]
    if op in ("rmsnorm", "layernorm"):
        rows, n = d
        eps = r.get("eps", 0x3727C5AC)
        if not valid_layer(dict(r, eps=eps)):
            result = []
            for i in range(rows):
                row = x[i * n : (i + 1) * n]
                if op == "layernorm":
                    mean = divide(dot(row, [ONE] * n), round32(Q(n)))
                    row = [subtract(a, mean) for a in row]
                scale = divide(ONE, sqrt32(add(divide(dot(row, row), round32(Q(n))), eps)))
                result.extend(
                    add(mul(mul(a, scale), w[j]), b[j])
                    if op == "layernorm"
                    else mul(mul(a, scale), w[j])
                    for j, a in enumerate(row)
                )
            return result
        result = []
        for i in range(rows):
            row = x[i * n : (i + 1) * n]
            exact = [value(a) for a in row]
            mean = sum(exact, Q(0)) / n if op == "layernorm" else Q(0)
            centered = [a - mean for a in exact]
            variance = sum((a * a for a in centered), Q(0)) / n
            denominator = value(sqrt_rational(variance + value(r.get("eps", 0x3727C5AC))))
            for j, a in enumerate(centered):
                y = a * value(w[j]) / denominator
                result.append(round32(y + value(b[j]) if op == "layernorm" else y))
        return result
    if op == "model":
        result = []
        for i in range(2):
            row = x[2 * i : 2 * i + 2]
            dense = reference(dict(op="linear", dims=[1, 2, 2], x=row, w=w, b=b))
            result += [activate(add(a, saved), True) for a, saved in zip(dense, row)]
        return result
    raise ValueError(op)


def valid_layer(r):
    """Independent public-boundary rules; never execute a malformed raw request."""
    op = r["op"]
    d, x, w, b = (r.get(k, []) for k in ("dims", "x", "w", "b"))
    if op == "linear":
        rows, inner, outputs = d
        return min(d) > 0 and (len(x), len(w), len(b)) == (rows * inner, outputs * inner, outputs)
    if op == "gemm":
        batches, rows, inner, cols = d
        return min(d) > 0 and (len(x), len(w)) == (batches * rows * inner, batches * inner * cols)
    if op in ("conv2d", "depthwise"):
        ci, h, wi, co, kh, kw, stride, pad = d
        channels = ci if op == "depthwise" else co
        filters = ci if op == "depthwise" else co * ci
        return (
            min(ci, h, wi, co, kh, kw, stride) > 0
            and kh <= h + 2 * pad
            and kw <= wi + 2 * pad
            and (len(x), len(w), len(b)) == (ci * h * wi, filters * kh * kw, channels)
        )
    if op in ("avgpool", "maxpool"):
        c, h, wi, k = d
        return min(d) > 0 and k <= min(h, wi) and len(x) == c * h * wi
    if op == "scaleShift":
        c, hw = d
        return min(d) > 0 and (len(x), len(w), len(b)) == (c * hw, c, c)
    if op == "addRelu":
        return bool(x) and len(x) == len(w)
    if op in ("rmsnorm", "layernorm"):
        rows, n = d
        eps = r.get("eps", 0x3727C5AC)
        return (
            min(d) > 0
            and len(x) == rows * n
            and len(w) == n
            and (op != "layernorm" or len(b) == n)
            and all(a & ~SIGN < INF for a in x + w + [eps] + (b if op == "layernorm" else []))
            and value(eps) > 0
        )
    raise ValueError(op)


def cases(seed, count):
    rng = random.Random(seed)
    out = []

    def put(op, **kw):
        out.append(dict(dict(op=op, dims=[], x=[], w=[], b=[], relu=False, eps=0x3727C5AC), **kw))

    def small(n):
        return [round32(Q(rng.randrange(-100, 101), 8)) for _ in range(n)]

    def finite():
        return (rng.randrange(255) << 23) | rng.getrandbits(23) | rng.choice([0, SIGN])

    edges = [
        0,
        SIGN,
        1,
        SIGN | 1,
        0x7FFFFF,
        0x800000,
        ONE,
        ONE | SIGN,
        0x33800000,
        0x34000000,
        MAX,
        MAX | SIGN,
    ]
    for a in edges:
        for b in edges:
            put("dot", x=[a], w=[b])
    specials = [0, SIGN, ONE, ONE | SIGN, INF, INF | SIGN, NAN | 7, 0x7F800123]
    for a in specials:
        for b in specials:
            put("dot", x=[a], w=[b])
    for a, b in [
        ([], []),
        ([ONE], []),
        ([SIGN, SIGN], [ONE, ONE]),
        ([ONE, ONE | SIGN, 1], [ONE] * 3),
        ([NAN | 7, 0x7F800123], [ONE, ONE]),
        ([INF, INF | SIGN], [ONE, ONE]),
    ]:
        put("dot", x=a, w=b)
    put("sum", x=[ONE, 0x33800000, 0x33800000])
    put("sum", x=[])
    put("checkedConv", dims=[1, 2, 2, 1, 5, 5, 1, 0])
    put("checkedConv", dims=[1, 2, 2, 1, 1, 1, 0, 0])
    put("checkedConv", dims=[1, 2, 2, 1, 1, 1, 1, 0], x=[ONE] * 4, w=[], b=[0])
    put("checkedConv", dims=[1, 2, 2, 1, 1, 1, 1, 0], x=[ONE] * 4, w=[ONE], b=[0])
    for eps, x, w in [
        (0, [ONE], [ONE]),
        (NAN, [ONE], [ONE]),
        (1, [NAN], [ONE]),
        (1, [ONE], []),
        (1, [ONE], [ONE]),
    ]:
        put("checkedNorm", dims=[1, 1], eps=eps, x=x, w=w)
    for x in [[ONE] * 3, [ONE] * 4]:
        put("checkedModel", x=x, w=[ONE] * 4, b=[0, 0])
    put("rmsnorm", dims=[1, 2], x=[0x5F800000] * 2, w=[ONE] * 2)
    put("layernorm", dims=[1, 2], x=[MAX] * 2, w=[ONE] * 2, b=[0, 0])
    for q in [
        Q(0),
        Q(1, 2**300),
        Q(3, 2**300),
        Q(1, 2**512),
        Q(2**512),
        Q((2**128 - 2**103) ** 2),
        Q((2**128 - 2**103) ** 2 - 1),
    ]:
        put("sqrt", dims=[q.numerator, q.denominator])
    # Exact squared midpoints, and perturbations on each side, across all binades.
    for _ in range(count):
        lower = rng.randrange(MAX)
        midpoint = (value(lower) + value(lower + 1)) / 2
        for q in (midpoint**2, midpoint**2 * Q(1000001, 1000000), midpoint**2 * Q(999999, 1000000)):
            put("sqrt", dims=[q.numerator, q.denominator])
    put("addRelu", relu=True, x=specials, w=[0] * len(specials))
    # FMA must not round the product before cancellation with the addend.
    put("scaleShift", dims=[1, 1], x=[0x3F800001], w=[0x3F7FFFFE], b=[SIGN | ONE])
    for _ in range(count):
        n = rng.randrange(0, 17)
        put("dot", x=[finite() for _ in range(n)], w=[finite() for _ in range(n)])
        put("sum", x=[finite() for _ in range(n)])
        rows, inner, cols = rng.randrange(1, 4), rng.randrange(1, 5), rng.randrange(1, 4)
        put(
            "linear",
            dims=[rows, inner, cols],
            relu=rng.choice([True, False]),
            x=small(rows * inner),
            w=small(cols * inner),
            b=small(cols),
        )
        put(
            "gemm",
            dims=[2, rows, inner, cols],
            x=small(2 * rows * inner),
            w=small(2 * inner * cols),
        )
        cin, h, width, cout = 2, 3, 5, 3
        kh, kw, stride, pad = rng.choice([(2, 3, 1, 0), (3, 2, 2, 1), (1, 1, 1, 0)])
        dims = [cin, h, width, cout, kh, kw, stride, pad]
        for op in ("conv2d", "depthwise"):
            filters = cout * cin if op == "conv2d" else cin
            put(
                op,
                dims=dims,
                relu=rng.choice([True, False]),
                x=small(cin * h * width),
                w=small(filters * kh * kw),
                b=small(cout if op == "conv2d" else cin),
            )
        for op in ("avgpool", "maxpool"):
            put(op, dims=[2, 4, 5, 2], x=small(40))
        put("addRelu", relu=rng.choice([True, False]), x=small(7), w=small(7))
        put(
            "scaleShift",
            dims=[3, 4],
            relu=rng.choice([True, False]),
            x=small(12),
            w=small(3),
            b=small(3),
        )
        for op in ("rmsnorm", "layernorm"):
            put(op, dims=[rows, inner], x=small(rows * inner), w=small(inner), b=small(inner))
            put(
                op,
                dims=[1, 3],
                eps=rng.choice([1, 0x3727C5AC, MAX]),
                x=[finite() for _ in range(3)],
                w=small(3),
                b=small(3),
            )
        put("model", x=small(4), w=small(4), b=small(2))
    return out


def edge_cases(seed):
    """Structured adversarial cases, independent of ordinary random small inputs."""
    rng = random.Random(seed)
    out = []

    def put(op, label, **kw):
        request = dict(op=op, label=label, dims=[], x=[], w=[], b=[], relu=False, eps=0x3727C5AC)
        request.update(kw)
        out.append(request)

    words = [
        0,
        SIGN,
        1,
        SIGN | 1,
        0x7FFFFF,
        0x800000,
        ONE,
        ONE | SIGN,
        0x3F800001,
        MAX,
        MAX | SIGN,
        INF,
        INF | SIGN,
        NAN | 7,
        SIGN | NAN | 9,
        0x7F800123,
        0xFF800456,
    ]
    for a in words:
        for b in words:
            put("addRelu", "scalar-add", x=[a], w=[b])
            put("addRelu", "scalar-relu", x=[a], w=[b], relu=True)
            for c in words:
                put("scaleShift", "fma-specials", dims=[1, 1], x=[a], w=[b], b=[c])
            for tail in ([0, SIGN], [INF, INF | SIGN], [NAN | 7, 0x7F800123]):
                window = [a, b] + tail
                for op in ("avgpool", "maxpool"):
                    put(op, "pool-specials", dims=[1, 2, 2, 2], x=window)
    for _ in range(300):
        a, b = [rng.choices(words, k=8) for _ in range(2)]
        put("dot", "reduction-specials", x=a, w=b)
        put("sum", "sum-specials", x=a)
        order = rng.sample(range(8), 8)
        put("dot", "reordered-specials", x=[a[i] for i in order], w=[b[i] for i in order])
    for exponent in range(255):
        lower = max(0, (exponent << 23) - 1)
        for neighbor in [lower, min(MAX - 1, lower + 1)]:
            mid = (value(neighbor) + value(neighbor + 1)) / 2
            for q in [mid**2, mid**2 + Q(1, 2**1100), max(Q(0), mid**2 - Q(1, 2**1100))]:
                put("sqrt", "sqrt-binade-midpoint", dims=[q.numerator, q.denominator])
    for _ in range(180):
        ci, h, wi, co = rng.randrange(1, 4), rng.randrange(1, 5), rng.randrange(1, 6), 2
        kh, kw, stride, pad = rng.choice([(1, 1, 1, 0), (2, 3, 2, 2), (3, 2, 3, 3)])
        for op in ("conv2d", "depthwise"):
            put(
                op,
                "conv-layout-extremes",
                dims=[ci, h, wi, co, kh, kw, stride, pad],
                x=rng.choices(words, k=ci * h * wi),
                w=rng.choices(words, k=(co * ci if op == "conv2d" else ci) * kh * kw),
                b=rng.choices(words, k=co if op == "conv2d" else ci),
                relu=bool(rng.getrandbits(1)),
            )
        put(
            "gemm",
            "batched-rectangular-extremes",
            dims=[3, 2, 3, 4],
            x=rng.choices(words, k=18),
            w=rng.choices(words, k=36),
        )
        put(
            "linear",
            "linear-extremes",
            dims=[2, 3, 4],
            x=rng.choices(words, k=6),
            w=rng.choices(words, k=12),
            b=rng.choices(words, k=4),
        )
    rows = [
        [0, SIGN, 0],
        [MAX] * 3,
        [MAX, MAX | SIGN, 1],
        [1, SIGN | 1, 0],
        [ONE, ONE, 0x3F800001],
        [ONE, ONE | SIGN, 0],
        [0x800000] * 3,
    ]
    for row in rows:
        for eps in [1, 0x800000, 0x3727C5AC, ONE, MAX, 0, SIGN, ONE | SIGN, INF, NAN, 0x7F800123]:
            for gain in [ONE, MAX, 0, SIGN, 1]:
                for op in ("rmsnorm", "layernorm"):
                    put(
                        op,
                        "normalization-extremes-fallback",
                        dims=[2, 3],
                        eps=eps,
                        x=row + list(reversed(row)),
                        w=[gain] * 3,
                        b=[1, MAX, SIGN],
                    )
    for word in words:
        for op in ("rmsnorm", "layernorm"):
            put(
                op,
                "normalization-specials",
                dims=[1, 3],
                x=[ONE, word, ONE | SIGN],
                w=[ONE] * 3,
                b=[0] * 3,
            )
    bases = [
        dict(op="linear", dims=[2, 3, 2], x=[ONE] * 6, w=[ONE] * 6, b=[0] * 2),
        dict(op="gemm", dims=[1, 2, 3, 2], x=[ONE] * 6, w=[ONE] * 6),
        dict(op="conv2d", dims=[1, 2, 3, 2, 2, 3, 2, 1], x=[ONE] * 6, w=[ONE] * 12, b=[0] * 2),
        dict(op="depthwise", dims=[2, 2, 3, 2, 2, 3, 2, 1], x=[ONE] * 12, w=[ONE] * 12, b=[0] * 2),
        dict(op="maxpool", dims=[1, 3, 5, 2], x=[ONE] * 15),
        dict(op="avgpool", dims=[1, 3, 5, 2], x=[ONE] * 15),
        dict(op="scaleShift", dims=[2, 3], x=[ONE] * 6, w=[ONE] * 2, b=[0] * 2),
        dict(op="rmsnorm", dims=[2, 3], x=[ONE] * 6, w=[ONE] * 3),
        dict(op="layernorm", dims=[2, 3], x=[ONE] * 6, w=[ONE] * 3, b=[0] * 3),
        dict(op="addRelu", x=[ONE] * 3, w=[ONE] * 3),
    ]
    for base in bases:
        mutations = [base]
        for key in ["x", "w", "b"]:
            if key in base:
                mutations += [
                    dict(base, **{key: base[key][:-1]}),
                    dict(base, **{key: base[key] + [0]}),
                ]
        for index in range(len(base.get("dims", []))):
            if base["op"] == "gemm" and index == 0:
                continue  # The model layer interface has exactly one GEMM batch.
            for val in [0, 2**64 + 1]:
                dims = list(base["dims"])
                dims[index] = val
                mutations.append(dict(base, dims=dims))
        if base["op"] in ("rmsnorm", "layernorm"):
            for eps in [0, SIGN, ONE | SIGN, INF, NAN, 0x7F800123]:
                mutations.append(dict(base, eps=eps))
            for key in ["x", "w"] + (["b"] if base["op"] == "layernorm" else []):
                for bad in [INF, NAN, 0x7F800123]:
                    arr = list(base[key])
                    arr[0] = bad
                    mutations.append(dict(base, **{key: arr}))
        for mutated in mutations:
            if any(v > 2**32 for v in mutated.get("dims", [])) and valid_layer(mutated):
                continue  # Do not request a valid but unbounded allocation from the raw spec.
            req = dict(mutated)
            op = req.pop("op")
            put("checked:" + op, "checked-boundary", **req)
    return out


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--cases-per-kernel", type=int, default=20)
    parser.add_argument(
        "--native", action="store_true", help="Use lake build exactSpecProbe output"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "data/spec-audit/results.json")
    parser.add_argument(
        "--edge-cases",
        action="store_true",
        help="Include exceptional-value and malformed-shape cases",
    )
    args = parser.parse_args()
    requests = cases(args.seed, args.cases_per_kernel)
    if args.edge_cases:
        requests += edge_cases(args.seed)
    expected = [reference(r) for r in requests]
    # These expose design boundaries, rather than treating the contract as an oracle.
    # Keep separate from the independent finite-reference comparisons above.
    defaults = dict(dims=[], x=[], w=[], b=[], relu=False, eps=0x3727C5AC)
    diagnostics = [
        dict(defaults, op="shape", dims=[1, 2, 2, 1, 5, 5, 1, 0]),
        dict(defaults, op="shape", dims=[1, 3, 5, 1, 1, 1, 0, 0]),
        dict(defaults, op="rmsnorm", dims=[1, 2], x=[0x5F800000] * 2, w=[ONE] * 2),
        dict(defaults, op="layernorm", dims=[1, 2], x=[MAX] * 2, w=[ONE] * 2, b=[0, 0]),
    ]
    suite_request = dict(defaults, op="traceSuite", eps=args.seed)
    suite_requests = [suite_request]
    if args.edge_cases:
        suite_requests.append(dict(defaults, op="traceBoundarySuite", eps=args.seed))
    lake = shutil.which("lake") or str(Path.home() / ".elan/bin/lake")
    command = (
        [str(ROOT / "lean/.lake/build/bin/exactSpecProbe")]
        if args.native
        else [lake, "env", "lean", "--run", "audit/SpecProbe.lean"]
    )
    run = subprocess.run(
        command,
        cwd=ROOT / "lean",
        input="".join(json.dumps(r) + "\n" for r in requests + diagnostics + suite_requests),
        text=True,
        capture_output=True,
        timeout=600,
    )
    if run.returncode or run.stderr.strip():
        raise RuntimeError(f"Lean probe failed:\n{run.stdout}\n{run.stderr}")
    actual = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(actual) == len(requests) + len(diagnostics) + len(suite_requests), len(actual)
    suites = actual[-len(suite_requests) :]
    for suite, count in zip(suites, [15, 30]):
        assert isinstance(suite, list) and len(suite) == count, suite
    models = [model for suite in suites for model in suite]
    model_failures = []
    for model in models:
        activation, saved = model["input"], []
        for index, record in enumerate(model["trace"]):
            r = record["request"]
            # Check wiring against independently propagated activations, not Lean's last output.
            assert r["w" if r["op"] == "gemm" else "x"] == activation
            if r["op"] == "save":
                saved = activation
            if r["op"] == "addRelu":
                assert r["w"] == saved
            activation = reference(r)
            if activation != record["actual"]:
                model_failures.append(
                    dict(
                        model=model["model"],
                        kind=model["kind"],
                        layer=index,
                        expected=activation,
                        **record,
                    )
                )
    failures = [
        dict(request=r, expected=e, actual=a)
        for r, e, a in zip(requests, expected, actual)
        if e != a
    ]
    report = dict(
        seed=args.seed,
        native=args.native,
        cases=len(requests),
        by_operation=dict(Counter(r["op"] for r in requests)),
        by_category=dict(Counter(r.get("label", "ordinary") for r in requests)),
        failures=failures,
        models_checked=len(models),
        model_failures=model_failures,
        diagnostics=[
            dict(request=r, actual=a) for r, a in zip(diagnostics, actual[len(requests) :])
        ],
        seconds=round(time.monotonic() - started, 2),
        sha256={
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                ROOT / "lean/contract/ContractExact.lean",
                ROOT / "lean/contract/ContractExactChecked.lean",
                ROOT / "lean/contract/ExactToolkit/StableNorm.lean",
                ROOT / "lean/contract/ExactToolkit/SqrtProof.lean",
                ROOT / "lean/contract/ExactToolkit/TensorProofs.lean",
                ROOT / "lean/contract/ExactToolkit/ModelProofs.lean",
                ROOT / "lean/bench/ExactSuite.lean",
                ROOT / "lean/audit/SpecProbe.lean",
                Path(__file__).resolve(),
            ]
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report["comparisons"] = [
        dict(request=r, expected=e, actual=a) for r, e, a in zip(requests, expected, actual)
    ]
    report["model_traces"] = models
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("comparisons", "model_traces")}, indent=2
        )
    )
    return bool(failures or model_failures)


if __name__ == "__main__":
    raise SystemExit(main())
