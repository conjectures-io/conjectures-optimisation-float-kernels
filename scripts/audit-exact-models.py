#!/usr/bin/env python3
"""Independently check generated model shapes and parameter metadata, without inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def conv_geometry(d):
    """Check a convolution's sizes, strides, dilations and groups; its dilated filter extents."""
    ci, h, w, co, kh, kw, sh, sw, _, _, _, _, dh, dw, groups = d
    assert min(ci, h, w, co, kh, kw, sh, sw, dh, dw, groups) > 0
    assert ci % groups == 0 and co % groups == 0
    return dh * (kh - 1) + 1, dw * (kw - 1) + 1


def pool_extent(n, lo, hi, k, stride, ceil):
    """Output length of one pooling axis (PyTorch/ONNX Runtime rule for ceil mode)."""
    span = n + lo + hi - k
    if not ceil:
        return span // stride + 1
    out = -(-span // stride) + 1
    # A last window may not start in the bottom/right padding.
    return out - 1 if (out - 1) * stride >= n + lo else out


def shape_step(layer, shape):
    """Derive sizes from ordinary tensor rules, independently of Lean's shapeStep."""
    op, d = layer["op"], layer["dims"]
    size, saved = shape
    weights, bias = layer["weights"], layer["bias"]
    if op == "linear":
        rows, inner, outputs = d
        assert min(d) > 0 and size == rows * inner
        assert (weights, bias) == (outputs * inner, outputs)
        size = rows * outputs
    elif op == "gemm":
        rows, inner, cols = d
        assert min(d) > 0 and size == inner * cols and weights == rows * inner
        size = rows * cols
    elif op in ("conv2d", "depthwise"):
        d = list(d)
        if op == "depthwise":
            d[3] = d[14] = d[0]  # one group per input channel, one output each
        ci, h, w, co, kh, kw, sh, sw, pt, pl, pb, pr, _, _, groups = d
        ekh, ekw = conv_geometry(d)
        assert ekh <= h + pt + pb and ekw <= w + pl + pr
        assert size == ci * h * w and bias == co
        assert weights == co * (ci // groups) * kh * kw
        size = co * ((h + pt + pb - ekh) // sh + 1) * ((w + pl + pr - ekw) // sw + 1)
    elif op == "convTranspose":
        ci, h, w, co, kh, kw, sh, sw, pt, pl, pb, pr, dh, dw, groups, oph, opw = d
        ekh, ekw = conv_geometry(d[:15])
        assert (oph < sh or oph < dh) and (opw < sw or opw < dw)
        height = sh * (h - 1) + oph + ekh - pt - pb
        width = sw * (w - 1) + opw + ekw - pl - pr
        assert height > 0 and width > 0
        assert size == ci * h * w and bias == co
        assert weights == ci * (co // groups) * kh * kw
        size = co * height * width
    elif op == "scaleShift":
        c, hw = d
        assert min(d) > 0 and size == c * hw and weights == bias == c
    elif op in ("maxpool", "avgpool"):
        c, h, w, kh, kw, sh, sw, pt, pl, pb, pr, ceil, include_pad = d
        assert min(c, h, w, kh, kw, sh, sw) > 0 and ceil in (0, 1) and include_pad in (0, 1)
        assert kh <= h + pt + pb and kw <= w + pl + pr
        assert max(pt, pb) < kh and max(pl, pr) < kw  # every window holds an image cell
        assert size == c * h * w and weights == bias == 0
        size = c * pool_extent(h, pt, pb, kh, sh, ceil) * pool_extent(w, pl, pr, kw, sw, ceil)
    elif op in ("rmsnorm", "layernorm"):
        rows, n = d
        assert min(d) > 0 and size == rows * n and weights == n
        assert op != "layernorm" or bias == n
        eps = layer["eps"]
        assert 0 < eps < 0x7F800000
    elif op == "save":
        saved = size
    elif op == "addSaved":
        assert size == saved and size > 0
    elif op == "attention":
        tokens, heads, d_ = d
        c = heads * d_
        assert min(d) > 0 and size == tokens * c
        assert (weights, bias) == (4 * c * c, 4 * c)
    elif op == "silu":
        assert size > 0
    elif op in ("lstm", "gru"):
        steps, inputs, hidden = d
        gates = 4 if op == "lstm" else 3
        assert min(d) > 0 and size == steps * inputs
        assert weights == gates * hidden * inputs + gates * hidden * hidden
        assert bias == 2 * gates * hidden
        size = steps * hidden
    elif op == "lastStep":
        steps, width = d
        assert min(d) > 0 and size == steps * width and weights == bias == 0
        size = width
    else:
        raise ValueError(op)
    assert layer["parameters_finite"] and layer["valid"]
    assert (size, saved) == (layer["output"], layer["saved_after"])
    return size, saved


def check_model(model):
    family, batch, kind = model["cell"].split(":")
    batch = int(batch.removeprefix("b"))
    assert family in {"mlp", "cnn", "mnet", "nmlp", "mixer", "tfm", "rnn"}
    assert kind in {"normal", "wide", "sparse"}
    assert model["input_length"] == model["samples"] * model["input_size"]
    assert model["samples"] == (1 if family in {"mlp", "nmlp"} else batch)
    shape = model["input_size"], 0
    for layer in model["trace"]:
        assert (layer["input"], layer["saved"]) == shape
        shape = shape_step(layer, shape)
    assert shape[0] == model["output_size"]
    assert model["samples"] * shape[0] == batch * 10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--random-seeds", type=int, default=28)
    parser.add_argument("--output", type=Path, default=ROOT / "data/spec-audit/model-shapes.json")
    args = parser.parse_args()
    rng = random.Random(20261019)
    seeds = list(
        dict.fromkeys(
            [0, 1, 2**31, 2**32 - 1, 42, 20261019, 20261020]
            + [rng.getrandbits(32) for _ in range(args.random_seeds)]
        )
    )
    run = subprocess.run(
        [str(ROOT / "lean/.lake/build/bin/auditExactShapes"), *map(str, seeds)],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    if run.stderr.strip():
        raise RuntimeError(run.stderr)
    models = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(models) == len(seeds) * 42
    assert len({(m["seed"], m["cell"]) for m in models}) == len(models)
    for model in models:
        check_model(model)
    summary = dict(
        seeds=seeds,
        models=len(models),
        layers=sum(len(m["trace"]) for m in models),
        operations=dict(Counter(layer["op"] for m in models for layer in m["trace"])),
        failures=[],
        sha256={
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / "lean/bench/ExactSuite.lean",
                ROOT / "lean/audit/SuiteAudit.lean",
                Path(__file__).resolve(),
            ]
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dict(summary=summary, models=models), indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
