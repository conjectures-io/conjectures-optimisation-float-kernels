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
from decimal import Decimal, localcontext
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


def exp32(w):
    """Correctly rounded binary32 exp, from Python's correctly rounded decimal `exp`.

    The decimal result is within half a unit of its last digit, so the true value lies within one
    unit of it; precision doubles until both ends of that interval round to the same word. The
    true value is never a rounding boundary (e^x is irrational for rational x != 0).
    """
    if isnan(w):
        return w | 0x400000
    if w & ~SIGN == INF:
        return 0 if w & SIGN else INF
    x = value(w)
    if x == 0:
        return ONE
    if x > 89:  # e^89 is beyond the overflow midpoint
        return INF
    if x < -104:  # e^-104 is below half the least subnormal, 2^-150
        return 0
    precision = 40
    while True:
        with localcontext() as context:
            context.prec = precision
            result = context.exp(Decimal(float(x)))  # binary32 converts exactly
        unit = Q(1, 10 ** (precision - 1 - result.adjusted()))
        lo, hi = round32(Q(result) - unit), round32(Q(result) + unit)
        if lo == hi:
            return lo
        precision *= 2


def sigmoid32(w):
    """`1 / (1 + exp(-x))`, the spec's steps in its order; `-x` flips the sign bit."""
    return divide(ONE, add(ONE, exp32(w ^ SIGN)))


def softmax(rows, n, x):
    def at(i):
        return x[i] if i < len(x) else 0

    out = []
    for r in range(rows):
        top = at(r * n)
        for t in range(n):
            top = maximum(top, at(r * n + t))
        e = [exp32(subtract(at(r * n + t), top)) for t in range(n)]
        total = dot(e, [ONE] * n)
        out += [divide(v, total) for v in e]
    return out


def scores(heads, tokens, d, causal, q, k):
    c = heads * d

    def at(a, i):
        return a[i] if i < len(a) else 0

    out = []
    for h in range(heads):
        for i in range(tokens):
            for j in range(tokens):
                if causal and j > i:
                    out.append(INF | SIGN)
                else:
                    out.append(
                        dot(
                            [at(q, i * c + h * d + t) for t in range(d)],
                            [at(k, j * c + h * d + t) for t in range(d)],
                        )
                    )
    return out


def mix(heads, tokens, d, p, v):
    c = heads * d

    def at(a, i):
        return a[i] if i < len(a) else 0

    return [
        dot(
            [at(p, (h * tokens + i) * tokens + j) for j in range(tokens)],
            [at(v, j * c + h * d + t) for j in range(tokens)],
        )
        for i in range(tokens)
        for h in range(heads)
        for t in range(d)
    ]


def attention(dims, x, w, b):
    """Projections by `linear`, then scores, softmax, mix and the output projection."""
    tokens, heads, d, causal = dims
    c = heads * d
    ws = [w[i * c * c : (i + 1) * c * c] for i in range(3)] + [w[3 * c * c :]]
    bs = [b[i * c : (i + 1) * c] for i in range(3)] + [b[3 * c :]]

    def project(inp, i):
        return reference(dict(op="linear", dims=[tokens, c, c], x=inp, w=ws[i], b=bs[i]))

    q, k, v = (project(x, i) for i in range(3))
    p = softmax(heads * tokens, tokens, scores(heads, tokens, d, causal == 1, q, k))
    return project(mix(heads, tokens, d, p, v), 3)


def activate(w, relu):
    return 0 if relu and w & SIGN and not isnan(w) else w


# Shapes travel to the Lean probe as flat `dims` lists, booleans as 0/1.
CONV_FIELDS = "cin h w cout kh kw strideH strideW padT padL padB padR dilH dilW groups".split()
CONVT_FIELDS = CONV_FIELDS + ["outPadH", "outPadW"]
POOL_FIELDS = "c h w kh kw strideH strideW padT padL padB padR ceilMode countIncludePad".split()


def _pair(v):
    return (v, v) if isinstance(v, int) else tuple(v)


def _sides(p):
    """Padding as (top, left, bottom, right) from an int, (vertical, horizontal) or 4 sides."""
    if isinstance(p, int):
        return (p,) * 4
    return (p[0], p[1], p[0], p[1]) if len(p) == 2 else tuple(p)


def conv_dims(cin, h, w, cout, kh, kw, *, stride=1, pad=0, dil=1, groups=1, out_pad=None):
    """Probe `dims` of a convolution, or of a transposed one when `out_pad` is given."""
    dims = [cin, h, w, cout, kh, kw, *_pair(stride), *_sides(pad), *_pair(dil), groups]
    return dims if out_pad is None else dims + list(_pair(out_pad))


def pool_dims(c, h, w, kh, kw, *, stride=None, pad=0, ceil=False, count_include_pad=False):
    """Probe `dims` of a pooling layer; the stride defaults to the window."""
    stride = (kh, kw) if stride is None else _pair(stride)
    return [c, h, w, kh, kw, *stride, *_sides(pad), int(ceil), int(count_include_pad)]


class Shape:
    """Named view of a flat `dims` list."""

    def __init__(self, fields, dims):
        assert len(dims) == len(fields), (fields, dims)
        self.__dict__.update(zip(fields, dims))


def conv_shape(op, dims):
    s = Shape(CONVT_FIELDS if op == "convTranspose" else CONV_FIELDS, dims)
    if op == "depthwise":
        # One group per input channel, one filter each; the request's cout/groups are unused.
        s.cout = s.groups = s.cin
    return s


def output_extent(n, k, dil, stride, lo, hi):
    """Convolution output length: placements of the dilated filter inside the padded axis."""
    return (n + lo + hi - (dil * (k - 1) + 1)) // stride + 1


def conv2d(s, x, w, b, relu):
    """Each output gathers its window from the zero-padded input (padding contributes +0 terms)
    over its group's channels, then the bias, rounded once."""
    ho = output_extent(s.h, s.kh, s.dilH, s.strideH, s.padT, s.padB)
    wo = output_extent(s.w, s.kw, s.dilW, s.strideW, s.padL, s.padR)
    per_group_in, per_group_out = s.cin // s.groups, s.cout // s.groups

    def padded(channel, row, col):
        inside = 0 <= row < s.h and 0 <= col < s.w
        return x[(channel * s.h + row) * s.w + col] if inside else 0

    out = []
    for o in range(s.cout):
        base = o // per_group_out * per_group_in
        for oy in range(ho):
            for ox in range(wo):
                pairs = [
                    (
                        padded(
                            base + c,
                            oy * s.strideH - s.padT + ky * s.dilH,
                            ox * s.strideW - s.padL + kx * s.dilW,
                        ),
                        w[((o * per_group_in + c) * s.kh + ky) * s.kw + kx],
                    )
                    for c in range(per_group_in)
                    for ky in range(s.kh)
                    for kx in range(s.kw)
                ]
                pairs.append((ONE, b[o]))
                inputs = [a for a, _ in pairs]
                weights = [c for _, c in pairs]
                out.append(activate(dot(inputs, weights), relu))
    return out


def conv_transpose(s, x, w, b, relu):
    """Scatter formulation: every input cell sends `x · w` through every filter tap to the
    output position it lands on, after cropping the padding. Each output then sums exactly the
    contributions it received, ordered by (input channel, tap row, tap column), bias last."""
    full_h = s.strideH * (s.h - 1) + s.dilH * (s.kh - 1) + 1 + s.outPadH
    full_w = s.strideW * (s.w - 1) + s.dilW * (s.kw - 1) + 1 + s.outPadW
    ho, wo = full_h - s.padT - s.padB, full_w - s.padL - s.padR
    per_group_in, per_group_out = s.cin // s.groups, s.cout // s.groups
    received = {}
    for c in range(s.cin):
        group = c // per_group_in
        for iy in range(s.h):
            for ix in range(s.w):
                source = x[(c * s.h + iy) * s.w + ix]
                for j in range(per_group_out):
                    o = group * per_group_out + j
                    for ky in range(s.kh):
                        oy = iy * s.strideH + ky * s.dilH - s.padT
                        for kx in range(s.kw):
                            ox = ix * s.strideW + kx * s.dilW - s.padL
                            if 0 <= oy < ho and 0 <= ox < wo:
                                weight = w[((c * per_group_out + j) * s.kh + ky) * s.kw + kx]
                                received.setdefault((o, oy, ox), []).append(
                                    ((c, ky, kx), source, weight)
                                )
    out = []
    for o in range(s.cout):
        for oy in range(ho):
            for ox in range(wo):
                terms = sorted(received.get((o, oy, ox), []), key=lambda term: term[0])
                assert len({key for key, _, _ in terms}) == len(terms)
                inputs = [source for _, source, _ in terms] + [ONE]
                weights = [weight for _, _, weight in terms] + [b[o]]
                out.append(activate(dot(inputs, weights), relu))
    return out


def window_starts(n, k, stride, lo, hi, ceil):
    """Start of each pooling window in padded coordinates, enumerated one by one.

    Floor mode: windows that lie wholly inside the padded axis. Ceil mode: keep adding windows
    until one has reached the end of the padded axis, but never one starting in the trailing
    padding (or beyond the image)."""
    total = lo + n + hi
    starts = []
    while True:
        start = len(starts) * stride
        if ceil:
            if starts and starts[-1] + k >= total:
                break
            if start >= lo + n:
                break
        elif start + k > total:
            break
        starts.append(start)
    return starts


def pooling(op, s, x):
    """Explicit window coordinates: each window's image cells (row-major); padding is no cell."""
    rows = window_starts(s.h, s.kh, s.strideH, s.padT, s.padB, s.ceilMode)
    cols = window_starts(s.w, s.kw, s.strideW, s.padL, s.padR, s.ceilMode)

    def image_range(start, k, lo, n):
        return range(max(start - lo, 0), min(start - lo + k, n))

    def padded_count(start, k, lo, n, hi):
        return min(start + k, lo + n + hi) - start

    out = []
    for channel in range(s.c):
        for top in rows:
            for left in cols:
                cells = [
                    x[(channel * s.h + r) * s.w + q]
                    for r in image_range(top, s.kh, s.padT, s.h)
                    for q in image_range(left, s.kw, s.padL, s.w)
                ]
                assert cells, "every valid window holds an image cell"
                if op == "maxpool":
                    acc = cells[0]
                    for word in cells:  # The first cell is folded in again, as specified.
                        acc = maximum(acc, word)
                    out.append(acc)
                else:
                    divisor = (
                        padded_count(top, s.kh, s.padT, s.h, s.padB)
                        * padded_count(left, s.kw, s.padL, s.w, s.padR)
                        if s.countIncludePad
                        else len(cells)
                    )
                    out.append(divide(dot(cells, [ONE] * len(cells)), round32(Q(divisor))))
    return out


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
    if op == "exp":
        return [exp32(a) for a in x]
    if op == "silu":
        return [mul(a, sigmoid32(a)) for a in x]
    if op == "softmax":
        return softmax(d[0], d[1], x)
    if op == "scores":
        return scores(d[0], d[1], d[2], d[3] == 1, x, w)
    if op == "mix":
        return mix(d[0], d[1], d[2], x, w)
    if op == "attention":
        return attention(d, x, w, b)
    if op == "sqrt":
        return [sqrt_rational(Q(d[0], d[1]))]
    if op.startswith("checked:"):
        raw = dict(r, op=op.removeprefix("checked:"))
        return [0] + reference(raw) if valid_layer(raw) else [1]
    if op == "checkedConv":
        raw = dict(r, op="conv2d")
        return [0] + reference(raw) if valid_layer(raw) else [1]
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
        return conv2d(conv_shape(op, d), x, w, b, relu)
    if op == "convTranspose":
        return conv_transpose(conv_shape(op, d), x, w, b, relu)
    if op in ("avgpool", "maxpool"):
        return pooling(op, Shape(POOL_FIELDS, d), x)
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


def shape_ok(op, d):
    """Geometry rules of the public boundary for convolution and pooling layers."""
    if op in ("avgpool", "maxpool"):
        s = Shape(POOL_FIELDS, d)
        return (
            min(s.c, s.h, s.w, s.kh, s.kw, s.strideH, s.strideW) > 0
            # The window fits the padded image, and no window can lie wholly in padding.
            and s.kh <= s.padT + s.h + s.padB
            and s.kw <= s.padL + s.w + s.padR
            and max(s.padT, s.padB) < s.kh
            and max(s.padL, s.padR) < s.kw
        )
    s = conv_shape(op, d)
    if not (
        min(s.cin, s.cout, s.h, s.w, s.kh, s.kw, s.strideH, s.strideW, s.dilH, s.dilW, s.groups) > 0
        and s.cin % s.groups == 0
        and s.cout % s.groups == 0
    ):
        return False
    span_h, span_w = s.dilH * (s.kh - 1) + 1, s.dilW * (s.kw - 1) + 1
    if op != "convTranspose":
        return span_h <= s.padT + s.h + s.padB and span_w <= s.padL + s.w + s.padR
    return (
        # At least one output row and column survives the cropping.
        s.strideH * (s.h - 1) + s.outPadH + span_h > s.padT + s.padB
        and s.strideW * (s.w - 1) + s.outPadW + span_w > s.padL + s.padR
        # ONNX/PyTorch: output padding below the stride or the dilation.
        and s.outPadH < max(s.strideH, s.dilH)
        and s.outPadW < max(s.strideW, s.dilW)
    )


def layer_sizes(op, d):
    """Expected (input, weight, bias) lengths of a convolution or pooling layer."""
    if op in ("avgpool", "maxpool"):
        s = Shape(POOL_FIELDS, d)
        return s.c * s.h * s.w, 0, 0
    s = conv_shape(op, d)
    taps = s.kh * s.kw
    if op == "convTranspose":
        # Weights are cin × (cout / groups) × kh × kw.
        filters = s.cin * (s.cout // s.groups if s.groups else 0)
    else:
        # Weights are cout × (cin / groups) × kh × kw.
        filters = s.cout * (s.cin // s.groups if s.groups else 0)
    return s.cin * s.h * s.w, filters * taps, s.cout


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
    if op in ("conv2d", "depthwise", "convTranspose", "avgpool", "maxpool"):
        return shape_ok(op, d) and (len(x), len(w), len(b)) == layer_sizes(op, d)
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
    if op == "attention":
        tokens, heads, d_ = d[:3]
        c = heads * d_
        return min(d[:3]) > 0 and (len(x), len(w), len(b)) == (tokens * c, 4 * c * c, 4 * c)
    if op == "silu":
        return bool(x)
    raise ValueError(op)


def random_geometry(rng, op):
    """A random valid small shape for a convolution or pooling layer, drawing every feature:
    groups, dilation, per-axis strides, four-sided padding, output padding, 1-D extents, ceil
    mode and count_include_pad."""
    while True:
        if op in ("avgpool", "maxpool"):
            h, w = rng.choice([1, 2, 3, 4, 5, 6]), rng.choice([1, 2, 3, 4, 5, 7])
            if rng.random() < 0.15:
                kh, kw, stride = h, w, 1  # Global pooling.
            else:
                kh, kw = rng.randrange(1, min(h, 3) + 2), rng.randrange(1, min(w, 3) + 2)
                stride = (rng.randrange(1, 4), rng.randrange(1, 4))
            pad = tuple(rng.randrange(0, k) for k in (kh, kw, kh, kw))
            dims = pool_dims(
                rng.randrange(1, 3),
                h,
                w,
                kh,
                kw,
                stride=stride,
                pad=pad if rng.random() < 0.7 else 0,
                ceil=rng.random() < 0.5,
                count_include_pad=rng.random() < 0.5,
            )
        else:
            groups = rng.choice([1, 1, 2, 3])
            dims = conv_dims(
                groups * rng.randrange(1, 3),
                rng.randrange(1, 6),
                rng.randrange(1, 6),
                groups * rng.randrange(1, 3),
                rng.randrange(1, 4),
                rng.randrange(1, 4),
                stride=(rng.randrange(1, 4), rng.randrange(1, 4)),
                pad=tuple(rng.choice([0, 0, 1, 2, 3]) for _ in range(4)),
                dil=(rng.choice([1, 1, 2, 3]), rng.choice([1, 1, 2])),
                groups=groups,
                out_pad=(
                    (rng.randrange(0, 3), rng.randrange(0, 3)) if op == "convTranspose" else None
                ),
            )
            if op == "depthwise" and rng.random() < 0.3:
                dims[3], dims[14] = rng.randrange(0, 5), rng.randrange(0, 5)  # Both unused.
        if shape_ok(op, dims):
            return dims


def cases(seed, count):
    rng = random.Random(seed)
    out = []

    def put(op, **kw):
        out.append(dict(dict(op=op, dims=[], x=[], w=[], b=[], relu=False, eps=0x3727C5AC), **kw))

    def small(n):
        return [round32(Q(rng.randrange(-100, 101), 8)) for _ in range(n)]

    def finite():
        return (rng.randrange(255) << 23) | rng.getrandbits(23) | rng.choice([0, SIGN])

    def moderate(n):
        return [
            (rng.randrange(112, 136) << 23) | rng.getrandbits(23) | rng.choice([0, SIGN])
            for _ in range(n)
        ]

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
    put("checkedConv", dims=conv_dims(1, 2, 2, 1, 5, 5))
    put("checkedConv", dims=conv_dims(1, 2, 2, 1, 1, 1, stride=0))
    put("checkedConv", dims=conv_dims(1, 2, 2, 1, 1, 1), x=[ONE] * 4, w=[], b=[0])
    put("checkedConv", dims=conv_dims(1, 2, 2, 1, 1, 1), x=[ONE] * 4, w=[ONE], b=[0])
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
        for op in ("conv2d", "depthwise", "convTranspose", "avgpool", "maxpool"):
            dims = random_geometry(rng, op)
            nx, nw, nb = layer_sizes(op, dims)
            # Moderate weights make the exact sums need genuine rounding.
            weights = rng.choice([small, moderate])
            put(
                op,
                dims=dims,
                relu=rng.choice([True, False]),
                x=small(nx),
                w=weights(nw),
                b=small(nb),
            )
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
        put("exp", x=[finite() for _ in range(8)] + small(8))
        put(
            "exp",
            x=[round32(Q(rng.randrange(-104 * 2**20, 89 * 2**20), 2**20)) for _ in range(200)],
        )
        put("silu", x=[finite() for _ in range(8)] + small(8))
        heads, tokens, d = rng.randrange(1, 3), rng.randrange(1, 4), rng.randrange(1, 4)
        c = heads * d
        causal = rng.randrange(2)
        put("scores", dims=[heads, tokens, d, causal], x=small(tokens * c), w=small(tokens * c))
        put("softmax", dims=[heads * tokens, tokens], x=small(heads * tokens * tokens))
        put("mix", dims=[heads, tokens, d], x=small(heads * tokens * tokens), w=small(tokens * c))
        put(
            "attention",
            dims=[tokens, heads, d, causal],
            x=small(tokens * c),
            w=small(4 * c * c),
            b=small(4 * c),
        )
    return out


# Boundary bases: every geometric field away from its default, so each rule can be broken.
BOUNDARY_CONV = conv_dims(2, 2, 3, 4, 2, 3, stride=(2, 1), pad=(1, 0, 1, 2), groups=2)
BOUNDARY_CONVT = conv_dims(
    2, 2, 3, 4, 2, 3, stride=(2, 1), pad=(1, 0, 0, 1), dil=(1, 2), groups=2, out_pad=(1, 1)
)
BOUNDARY_POOL = pool_dims(
    2, 3, 5, 2, 3, stride=(1, 2), pad=(1, 1, 0, 2), ceil=True, count_include_pad=True
)
BOUNDARY_MUTATIONS = {
    "conv2d": [
        dict(groups=3),  # Divides neither channel count.
        dict(groups=4),  # Divides cout only.
        dict(cout=3),  # Not a multiple of groups.
        dict(dilH=3),  # Dilated filter of 4 rows fits exactly.
        dict(dilH=4),  # One row too tall.
        dict(dilW=2, padR=1),  # One column too wide.
        dict(padT=0, padB=0, kh=3),
    ],
    "convTranspose": [
        dict(outPadH=2),  # Not below stride 2 or dilation 1.
        dict(outPadW=2),  # Not below stride 1 or dilation 2.
        dict(outPadW=1, dilW=1),  # Not below stride 1 or dilation 1.
        dict(outPadH=1, strideH=1, dilH=2),  # Allowed by the dilation alone.
        dict(padT=4, padB=0),  # One output row survives (full height 5).
        dict(padT=3, padB=2),  # None survives.
        dict(padL=0, padR=7),  # One output column survives (full width 8).
        dict(padL=4, padR=4),  # None survives.
        dict(groups=3),
        dict(cout=3),
    ],
    "maxpool": [
        dict(padT=2),  # Padding as large as the window.
        dict(padR=3),
        dict(padB=1),  # Largest allowed.
        dict(kh=6),  # Taller than the padded image (3 + 1 + 0).
        dict(kh=4, padT=1),  # Exactly as tall as the padded image.
        dict(ceilMode=2, countIncludePad=7),  # Flags are any nonzero value.
    ],
}
BOUNDARY_MUTATIONS["avgpool"] = BOUNDARY_MUTATIONS["maxpool"]
BOUNDARY_MUTATIONS["depthwise"] = []


def feature_cases(put, rng, words):
    """Each generalised geometry feature, with values chosen to expose term-set mistakes."""
    conv_shapes = [
        ("groups", conv_dims(4, 3, 3, 6, 2, 2, groups=2)),
        ("groups", conv_dims(3, 3, 4, 3, 1, 3, groups=3)),
        ("dilation", conv_dims(1, 5, 5, 2, 2, 2, dil=2)),
        ("dilation", conv_dims(2, 4, 6, 1, 2, 3, dil=(1, 2))),
        ("asymmetric-pads", conv_dims(1, 3, 3, 1, 2, 2, pad=(0, 1, 2, 0))),
        ("asymmetric-pads", conv_dims(2, 2, 3, 2, 3, 1, pad=(2, 0, 1, 0))),
        ("unequal-strides", conv_dims(1, 5, 6, 2, 2, 2, stride=(1, 3))),
        ("unequal-strides", conv_dims(1, 6, 4, 1, 2, 1, stride=(3, 1), pad=(1, 0, 0, 0))),
        ("1d", conv_dims(2, 1, 7, 2, 1, 3, stride=(1, 2), pad=(0, 1, 0, 1))),
        ("1d", conv_dims(1, 6, 1, 1, 3, 1, dil=(2, 1))),
        ("all-padding-windows", conv_dims(1, 1, 1, 1, 1, 1, pad=(2, 0, 0, 3))),
        ("all-padding-windows", conv_dims(1, 2, 2, 2, 2, 2, stride=3, pad=3)),
        (
            "everything",
            conv_dims(4, 4, 5, 2, 2, 3, stride=(2, 1), pad=(1, 0, 2, 3), dil=(2, 1), groups=2),
        ),
    ]
    convt_shapes = [
        ("gaps", conv_dims(1, 1, 3, 1, 1, 1, stride=(1, 3), out_pad=0)),
        ("gaps-outpad", conv_dims(1, 2, 2, 1, 1, 1, stride=(3, 2), out_pad=(2, 1))),
        ("dilation-gaps", conv_dims(1, 2, 2, 1, 2, 2, dil=(3, 2), out_pad=0)),
        ("outpad-below-dilation", conv_dims(1, 2, 2, 1, 2, 2, dil=(3, 2), out_pad=(2, 1))),
        ("groups", conv_dims(4, 2, 2, 6, 2, 2, stride=2, groups=2, out_pad=1)),
        ("groups", conv_dims(3, 2, 3, 3, 2, 1, groups=3, out_pad=0)),
        ("asymmetric-crop", conv_dims(1, 3, 3, 2, 3, 3, stride=2, pad=(0, 2, 3, 1), out_pad=0)),
        ("crop-to-one", conv_dims(1, 2, 2, 1, 3, 3, pad=(2, 2, 1, 1), out_pad=0)),
        ("1d", conv_dims(2, 1, 4, 2, 1, 3, stride=(1, 2), pad=(0, 1, 0, 0), out_pad=(0, 1))),
        (
            "unequal-strides",
            conv_dims(2, 3, 2, 2, 2, 3, stride=(2, 3), pad=(1, 0, 0, 2), out_pad=0),
        ),
        (
            "everything",
            conv_dims(
                4,
                2,
                3,
                2,
                3,
                2,
                stride=(3, 2),
                pad=(2, 1, 0, 1),
                dil=(1, 2),
                groups=2,
                out_pad=(2, 1),
            ),
        ),
    ]
    pool_shapes = [
        ("rectangular", dict(c=1, h=4, w=6, kh=2, kw=3)),
        ("global", dict(c=2, h=3, w=5, kh=3, kw=5, stride=1)),
        ("overlapping", dict(c=1, h=5, w=5, kh=3, kw=2, stride=(1, 2))),
        ("gaps", dict(c=1, h=5, w=7, kh=1, kw=2, stride=3)),
        ("asymmetric-pads", dict(c=1, h=4, w=5, kh=3, kw=3, stride=2, pad=(1, 2, 2, 0))),
        ("ceil-partial", dict(c=1, h=5, w=5, kh=2, kw=2, stride=2)),
        ("ceil-drops-padding-start", dict(c=1, h=5, w=5, kh=2, kw=2, stride=2, pad=1)),
        ("1d", dict(c=1, h=1, w=7, kh=1, kw=3, stride=(1, 2), pad=(0, 1, 0, 2))),
        ("everything", dict(c=2, h=4, w=5, kh=3, kw=2, stride=(2, 3), pad=(2, 1, 1, 1))),
    ]
    # Specials (twice), signed zeros, subnormals, and overflowing or infinite values.
    pools = [words, words, [0, SIGN], [1, SIGN | 1, 0x7FFFFF, SIGN | 0x400001]]
    pools.append([MAX, MAX | SIGN, INF, INF | SIGN])
    fills = [lambda n, pool=pool: rng.choices(pool, k=n) for pool in pools]
    for op, shapes in [("conv2d", conv_shapes), ("depthwise", conv_shapes)]:
        for feature, dims in shapes:
            if op == "depthwise":
                dims = list(dims)
                dims[3], dims[14] = 0, 5  # Unused by depthwise; any value is accepted.
            assert shape_ok(op, dims), (op, dims)
            nx, nw, nb = layer_sizes(op, dims)
            for fill in fills:
                put(
                    op,
                    f"{op}-{feature}",
                    dims=dims,
                    x=fill(nx),
                    w=fill(nw),
                    b=fill(nb),
                    relu=bool(rng.getrandbits(1)),
                )
            # Padding is a +0 term: 0·inf is NaN and +0 decides a zero sum's sign.
            put(
                op,
                f"{op}-padding-terms",
                dims=dims,
                x=[ONE] * nx,
                w=[INF] * nw,
                b=[ONE] * nb,
            )
            put(
                op,
                f"{op}-padding-terms",
                dims=dims,
                x=[SIGN] * nx,
                w=[ONE] * nw,
                b=[SIGN] * nb,
            )
    for feature, dims in convt_shapes:
        op = "convTranspose"
        assert shape_ok(op, dims), dims
        nx, nw, nb = layer_sizes(op, dims)
        for fill in fills:
            put(
                op,
                f"{op}-{feature}",
                dims=dims,
                x=fill(nx),
                w=fill(nw),
                b=fill(nb),
                relu=bool(rng.getrandbits(1)),
            )
        # Taps that reach no input are not terms: no 0·inf, and no +0 to change a -0 bias.
        put(op, f"{op}-absent-taps", dims=dims, x=[0] * nx, w=[INF] * nw, b=[ONE] * nb)
        put(op, f"{op}-absent-taps", dims=dims, x=[SIGN] * nx, w=[ONE] * nw, b=[SIGN] * nb)
        put(
            op,
            f"{op}-absent-taps",
            dims=dims,
            x=[ONE] * nx,
            w=[NAN | 3] * nw,
            b=[0x7F800001] * nb,
        )
    for feature, shape in pool_shapes:
        for ceil in (False, True):
            for count_include_pad in (False, True):
                dims = pool_dims(**shape, ceil=ceil, count_include_pad=count_include_pad)
                assert shape_ok("maxpool", dims), dims
                nx = layer_sizes("maxpool", dims)[0]
                for op in ("avgpool", "maxpool"):
                    label = f"{op}-{feature}"
                    for fill in fills:
                        put(op, label, dims=dims, x=fill(nx))
                    # Padding is not a value: a negative window's maximum stays negative.
                    put(
                        op,
                        f"{op}-padding-not-a-value",
                        dims=dims,
                        x=[round32(Q(-1 - i, 4)) for i in range(nx)],
                    )
                    put(op, label, dims=dims, x=[round32(Q(i + 1)) for i in range(nx)])
    # The first cell is folded twice: a lone signalling NaN comes out quiet.
    put(
        "maxpool",
        "maxpool-lone-signalling-nan",
        dims=pool_dims(1, 1, 4, 1, 1),
        x=[0x7F800001, SIGN | 0x7F800002, NAN | 5, SIGN],
    )
    for first, second in [
        (0, SIGN),
        (SIGN, 0),
        (SIGN, SIGN),
        (0x7F800001, NAN | 9),
        (NAN | 9, 0x7F800001),
        (INF | SIGN, NAN | 1),
        (1, SIGN | 1),
    ]:
        put("maxpool", "maxpool-pair-order", dims=pool_dims(1, 1, 2, 1, 2), x=[first, second])


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
                    put(op, "pool-specials", dims=pool_dims(1, 2, 2, 2, 2), x=window)
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
        for op in ("conv2d", "depthwise", "convTranspose", "avgpool", "maxpool"):
            dims = random_geometry(rng, op)
            nx, nw, nb = layer_sizes(op, dims)
            put(
                op,
                "conv-layout-extremes" if nw else "pool-layout-extremes",
                dims=dims,
                x=rng.choices(words, k=nx),
                w=rng.choices(words, k=nw),
                b=rng.choices(words, k=nb),
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
    feature_cases(put, rng, words)
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
    # exp: specials, the overflow and underflow boundaries, tiny arguments, and the inputs on
    # which FloatLib's approximate exp misrounds (correct rounding must hold there too).
    exp_words = words + [
        0x42B17217,
        0x42B17218,
        0x42B17219,
        0xC2CE8ED0,
        0xC2CFF1B5,
        0xC2D00000,
        0x33800000,
        0xB3800000,
        0x33000000,
        0x00000001,
        0xBBF0EDF1,
        0xC16912CD,
        0x3EBF4A81,
        0x429675E7,
    ]
    put("exp", "exp-boundaries", x=exp_words)
    put("silu", "silu-specials", x=exp_words)
    masked = INF | SIGN
    for row in (
        [masked, ONE, 0],
        [masked, masked, masked],
        [NAN | 7, ONE, 0],
        [0x7F800123, ONE, NAN | 9],
        [INF, ONE, 0],
        [INF, INF, 0],
        [MAX, MAX | SIGN, 0],
        [0, SIGN, 1],
    ):
        put("softmax", "softmax-specials", dims=[1, 3], x=row)
    for _ in range(40):
        put(
            "scores",
            "scores-specials",
            dims=[1, 2, 2, 1],
            x=rng.choices(words, k=4),
            w=rng.choices(words, k=4),
        )
        put(
            "mix",
            "mix-specials",
            dims=[1, 2, 2],
            x=rng.choices(words, k=4),
            w=rng.choices(words, k=4),
        )
    bases = [
        dict(op="attention", dims=[2, 1, 2, 1], x=[ONE] * 4, w=[ONE] * 16, b=[0] * 8),
        dict(op="silu", x=[ONE] * 3),
        dict(op="linear", dims=[2, 3, 2], x=[ONE] * 6, w=[ONE] * 6, b=[0] * 2),
        dict(op="gemm", dims=[1, 2, 3, 2], x=[ONE] * 6, w=[ONE] * 6),
        dict(
            op="conv2d",
            dims=conv_dims(1, 2, 3, 2, 2, 3, stride=2, pad=1),
            x=[ONE] * 6,
            w=[ONE] * 12,
            b=[0] * 2,
        ),
        dict(op="conv2d", dims=BOUNDARY_CONV, x=[ONE] * 12, w=[ONE] * 24, b=[0] * 4),
        dict(
            op="depthwise",
            dims=conv_dims(2, 2, 3, 2, 2, 3, stride=2, pad=1),
            x=[ONE] * 12,
            w=[ONE] * 12,
            b=[0] * 2,
        ),
        dict(op="convTranspose", dims=BOUNDARY_CONVT, x=[ONE] * 12, w=[ONE] * 24, b=[0] * 4),
        dict(op="maxpool", dims=pool_dims(1, 3, 5, 2, 2), x=[ONE] * 15),
        dict(op="avgpool", dims=pool_dims(1, 3, 5, 2, 2), x=[ONE] * 15),
        dict(op="maxpool", dims=BOUNDARY_POOL, x=[ONE] * 30),
        dict(op="avgpool", dims=BOUNDARY_POOL, x=[ONE] * 30),
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
        if base.get("dims") in (BOUNDARY_CONV, BOUNDARY_CONVT, BOUNDARY_POOL):
            fields = POOL_FIELDS if base["dims"] == BOUNDARY_POOL else CONVT_FIELDS
            for changes in BOUNDARY_MUTATIONS[base["op"]]:
                dims = list(base["dims"])
                for name, val in changes.items():
                    dims[fields.index(name)] = val
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
        # Natural-number truncation outside the validity rules (kernel too large, stride 0).
        dict(defaults, op="shape", dims=conv_dims(1, 2, 2, 1, 5, 5)),
        dict(defaults, op="shape", dims=conv_dims(1, 3, 5, 1, 1, 1, stride=0)),
        dict(defaults, op="convTShape", dims=conv_dims(1, 2, 2, 1, 1, 1, pad=5, out_pad=0)),
        dict(defaults, op="poolShape", dims=pool_dims(1, 2, 2, 5, 5)),
        dict(defaults, op="poolShape", dims=pool_dims(1, 5, 5, 2, 2, pad=(0, 0, 1, 1), ceil=True)),
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
    for suite, count in zip(suites, [21, 39]):
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
