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
        ci, h, w, co, kh, kw, stride, pad = d
        assert min(ci, h, w, co, kh, kw, stride) > 0
        assert kh <= h + 2 * pad and kw <= w + 2 * pad
        channels = ci if op == "depthwise" else co
        assert size == ci * h * w and bias == channels
        assert weights == channels * kh * kw * (1 if op == "depthwise" else ci)
        size = channels * ((h + 2 * pad - kh) // stride + 1) * ((w + 2 * pad - kw) // stride + 1)
    elif op == "scaleShift":
        c, hw = d
        assert min(d) > 0 and size == c * hw and weights == bias == c
    elif op in ("maxpool", "avgpool"):
        c, h, w, k = d
        assert min(d) > 0 and k <= min(h, w) and size == c * h * w
        size = c * (h // k) * (w // k)
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
    else:
        raise ValueError(op)
    assert layer["parameters_finite"] and layer["valid"]
    assert (size, saved) == (layer["output"], layer["saved_after"])
    return size, saved


def check_model(model):
    family, batch, kind = model["cell"].split(":")
    batch = int(batch.removeprefix("b"))
    assert family in {"mlp", "cnn", "mnet", "nmlp", "mixer"}
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
    assert len(models) == len(seeds) * 30
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
