"""Benchmark suites for the full entry: a suite file describes kinds of input, a plan draws cases.

A suite is a JSON file. Its *groups* are the published headings results are reported under (a
group's name is public, as are the scores under it); its *families* are the value generators inside
a group, with parameter ranges each case draws from. The paid board is scored on a private suite
the operator keeps outside the repository (GATE_FULL_SUITE); `suites/public-v2.json` is the public
suite for local tuning, built from the same generators. Without GATE_FULL_SUITE the gate times the
legacy twelve classes, as before.

`make_plan` turns a suite, a seed and a work budget into concrete cases for the Lean harness
(`lean/bench/BenchSuite.lean`, which reads them from BENCH_PLAN). The plan never leaves the gate:
the verdict carries each case's group and its times, never its parameters or its size.

`summarise` turns the timed rounds into the verdict's score. Per case, the fastest round of each
side gives a ratio; a cell (kernel and group) is the geometric mean of its cases' ratios; the
score is the weighted geometric mean of the cells. A fast path that helps one kind of input
therefore cannot hide a slowdown on another behind its share of the total time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
from pathlib import Path

KERNELS = ("add", "mul", "sum", "bmm")
SCALAR = ("add", "mul")

# Enumerations the Lean harness reads as numbers; the suite file names them.
MAGNITUDES = {"uniform": 0, "geometric": 1, "raw": 2}
ZERO_KINDS = {"pos": 0, "neg": 1, "signed": 2, "leak": 3}
PLACEMENTS = {"random": 0, "stride": 1, "burst": 2, "edge": 3}
SPECIALS = {"inf": 1, "nan": 2, "max": 4, "sub": 8, "zero": 16}
RELATIONS = {
    "independent": 0,
    "equal": 1,
    "negate": 2,
    "cancel": 3,
    "broadcast": 4,
    "identity": 5,
    "offset": 6,
    "near": 7,
}

# Value parameters with their defaults (each one off), in the order the harness's Case lists them.
DEFAULTS: dict[str, object] = {
    "magnitude": "uniform",
    "center": 127,
    "spread": 3,
    "positive": 50,
    "mantissa_bits": 23,
    "codebook": 0,
    "scale_block": 0,
    "scale_shift": 0,
    "zero_pct": 0,
    "zero_cluster": 1,
    "zero_kind": "pos",
    "sub_pct": 0,
    "sub_block": 1,
    "special_rate": 0,
    "special_place": "random",
    "special_period": 64,
    "special_burst": 8,
    "specials": ["inf", "nan", "max", "sub", "zero"],
    "relation": "independent",
    "relation_param": 0,
}
SIZE_KEYS = {"sum": ("length",), "bmm": ("m", "n", "p")}
# A plan's JSON travels in the bench's environment, which caps one string at 128 KiB.
MAX_PLAN_BYTES = 100_000
# A guard on every drawn size, so a suite typo cannot ask for a gigabyte buffer.
MAX_DIM = 1 << 16
MAX_ELEMENTS = 1 << 22


class SuiteError(ValueError):
    """A suite file the gate refuses to time with."""


def load(path: str | os.PathLike[str] | None = None) -> dict | None:
    # The suite GATE_FULL_SUITE names, validated, with its file hash; None for the legacy bench.
    path = path if path is not None else os.environ.get("GATE_FULL_SUITE") or None
    if path is None:
        return None
    raw = Path(path).read_bytes()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SuiteError(f"{path}: not JSON: {exc}") from exc
    validate(spec)
    return {**spec, "sha256": hashlib.sha256(raw).hexdigest()}


def file_sha256() -> str | None:
    # The hash of the suite GATE_FULL_SUITE names, for the verifier fingerprint; None if unset.
    path = os.environ.get("GATE_FULL_SUITE")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None


def validate(spec: dict) -> None:
    # Refuses a suite that could not be drawn from, before any bench runs.
    if not isinstance(spec.get("version"), str) or not spec["version"]:
        raise SuiteError("suite needs a version")
    weights = spec.get("kernel_weights", {})
    if set(weights) != set(KERNELS) or any(not _positive(w) for w in weights.values()):
        raise SuiteError(f"kernel_weights must give a positive weight to each of {KERNELS}")
    work = spec.get("work", {})
    if set(work) != set(KERNELS) or any(not _positive(w) for w in work.values()):
        raise SuiteError(f"work must give a positive multiple of BENCH_PAIRS to each of {KERNELS}")
    groups = spec.get("groups")
    if not isinstance(groups, list) or not groups:
        raise SuiteError("suite needs groups")
    names = [g.get("name") for g in groups]
    if len(set(names)) != len(names) or not all(isinstance(n, str) and n for n in names):
        raise SuiteError("group names must be distinct, non-empty strings")
    for name in names:
        if ":" in name or name == "check":
            raise SuiteError(f"group name {name!r} is reserved or contains ':'")
    covered = set()
    for g in groups:
        kernels = g.get("kernels", list(KERNELS))
        if not set(kernels) <= set(KERNELS) or not kernels:
            raise SuiteError(f"group {g['name']}: unknown kernels {kernels}")
        covered |= set(kernels)
        if not _positive(g.get("weight", 1)) or not isinstance(g.get("cases", 1), int):
            raise SuiteError(f"group {g['name']}: weight must be positive, cases an integer")
        if g.get("cases", 1) < 1:
            raise SuiteError(f"group {g['name']}: at least one case per run")
        families = g.get("families")
        if not isinstance(families, list) or not families:
            raise SuiteError(f"group {g['name']}: needs families")
        for f in families:
            unknown = set(f) - set(DEFAULTS) - {"weight", "kernels"}
            if unknown:
                raise SuiteError(f"group {g['name']}: unknown family keys {sorted(unknown)}")
    if covered != set(KERNELS):
        raise SuiteError(f"no group times {sorted(set(KERNELS) - covered)}")
    sizes = spec.get("sizes", {})
    for kernel, keys in SIZE_KEYS.items():
        options = sizes.get(kernel)
        if not isinstance(options, list) or not options:
            raise SuiteError(f"sizes.{kernel} must list at least one size distribution")
        for option in options:
            if set(keys) - set(option):
                raise SuiteError(f"sizes.{kernel}: each option needs {keys}")


def _positive(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


def draw(rng: random.Random, value: object) -> object:
    # One parameter: a constant, [lo, hi] inclusive, {"choices": [...]} or {"loguniform": [lo, hi]}.
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value):
        lo, hi = sorted(value)
        return rng.randint(lo, hi)
    if isinstance(value, dict) and "choices" in value:
        return rng.choice(value["choices"])
    if isinstance(value, dict) and "loguniform" in value:
        lo, hi = value["loguniform"]
        return max(lo, min(hi, round(math.exp(rng.uniform(math.log(lo), math.log(hi))))))
    return value


def _pick(rng: random.Random, options: list[dict]) -> dict:
    # One option, by its "weight" (1 when absent).
    return rng.choices(options, weights=[o.get("weight", 1) for o in options])[0]


def _encode(params: dict, kernel: str) -> dict:
    # Drawn family parameters as the numbers BenchSuite.lean reads.
    mask = 0
    for name in params["specials"]:
        mask |= SPECIALS[name]
    relation = params["relation"] if kernel in SCALAR else "independent"
    return {
        "mag": MAGNITUDES[params["magnitude"]],
        "center": int(params["center"]),
        "spread": int(params["spread"]),
        "positive": int(params["positive"]),
        "mantBits": int(params["mantissa_bits"]),
        "codebook": int(params["codebook"]),
        "scaleBlock": int(params["scale_block"]),
        "scaleShift": int(params["scale_shift"]),
        "zeroPct": int(params["zero_pct"]),
        "zeroCluster": max(1, int(params["zero_cluster"])),
        "zeroKind": ZERO_KINDS[params["zero_kind"]],
        "subPct": int(params["sub_pct"]),
        "subBlock": max(1, int(params["sub_block"])),
        "specRate": int(params["special_rate"]),
        "specPlace": PLACEMENTS[params["special_place"]],
        "specPeriod": max(1, int(params["special_period"])),
        "specBurst": max(1, int(params["special_burst"])),
        "specMask": mask or SPECIALS["nan"],
        "relation": RELATIONS[relation],
        "relParam": int(params["relation_param"]),
    }


def _check_ranges(case: dict) -> None:
    # Bounds the harness relies on; a suite that draws outside them is a suite bug.
    ok = (
        1 <= case["center"] <= 254
        and 0 <= case["spread"] <= 253
        and 0 <= case["positive"] <= 100
        and 0 <= case["mantBits"] <= 23
        and 0 <= case["zeroPct"] <= 100
        and 0 <= case["subPct"] <= 100
        and 0 <= case["specRate"] <= 100_000
        and 0 <= case["scaleShift"] <= 126
        and 0 <= case["relParam"] <= 253
    )
    if not ok:
        raise SuiteError(f"case {case['name']} drew a parameter out of range: {case}")


def _size(rng: random.Random, kernel: str, spec: dict, group: dict, budget: int) -> dict:
    # A drawn size within the case's budget: scalar steps, vectors of a length, or matrices.
    if kernel in SCALAR:
        return {"length": 1, "m": 1, "n": 1, "p": 1, "batch": 1, "work": budget}
    option = _pick(rng, group.get("sizes", {}).get(kernel) or spec["sizes"][kernel])
    if kernel == "sum":
        length = min(int(draw(rng, option["length"])), MAX_DIM)
        length = max(1, min(length, budget))
        batch = max(1, budget // length)
        return {"length": length, "m": 1, "n": 1, "p": 1, "batch": batch, "work": batch * length}
    dims = [max(1, min(int(draw(rng, option[k])), MAX_DIM)) for k in ("m", "n", "p")]
    # A shape over twice the budget is halved along its largest side until it fits.
    while dims[0] * dims[1] * dims[2] > 2 * budget and max(dims) > 1:
        k = dims.index(max(dims))
        dims[k] = max(1, dims[k] // 2)
    m, n, p = dims
    batch = max(1, budget // (m * n * p))
    return {"length": 1, "m": m, "n": n, "p": p, "batch": batch, "work": batch * m * n * p}


def cells(spec: dict) -> dict[str, float]:
    # Each cell's share of the score: its kernel's weight split over the kernel's groups by weight.
    total_k = sum(spec["kernel_weights"].values())
    out: dict[str, float] = {}
    for kernel in KERNELS:
        groups = [g for g in spec["groups"] if kernel in g.get("kernels", KERNELS)]
        total_g = sum(g.get("weight", 1) for g in groups)
        for g in groups:
            share = spec["kernel_weights"][kernel] / total_k * g.get("weight", 1) / total_g
            out[f"{kernel}:{g['name']}"] = share
    return out


def make_plan(spec: dict, seed: int, work: int) -> dict:
    # Concrete cases for one gate run: per cell its group's number of cases, each drawing a family,
    # its parameters and a size, plus one untimed raw-bits case per kernel for the checksum.
    if work < 1:
        raise SuiteError("BENCH_PAIRS must be positive")
    rng = random.Random(f"{spec['version']}:{seed}:{work}")
    cases: list[dict] = []
    for kernel in KERNELS:
        groups = [g for g in spec["groups"] if kernel in g.get("kernels", KERNELS)]
        n_cases = sum(g.get("cases", 1) for g in groups)
        budget = max(1, int(spec["work"][kernel] * work) // n_cases)
        for g in groups:
            families = [f for f in g["families"] if kernel in f.get("kernels", KERNELS)]
            if not families:
                raise SuiteError(f"group {g['name']} has no family for {kernel}")
            for j in range(g.get("cases", 1)):
                family = _pick(rng, families)
                params = {k: draw(rng, family.get(k, d)) for k, d in DEFAULTS.items()}
                case = {
                    "name": f"{kernel}:{g['name']}:{j}",
                    "kernel": kernel,
                    "timed": True,
                    "seed": rng.getrandbits(32),
                    **_encode(params, kernel),
                    **_size(rng, kernel, spec, g, budget),
                }
                _check_ranges(case)
                cases.append(case)
        check = {
            "name": f"{kernel}:check",
            "kernel": kernel,
            "timed": False,
            "seed": rng.getrandbits(32),
            **_encode({**DEFAULTS, "magnitude": "raw"}, kernel),
            **_size(rng, kernel, spec, {}, min(budget, 4096)),
        }
        cases.append(check)
    for case in cases:
        elements = case["batch"] * max(
            case["length"], case["m"] * case["n"] + case["n"] * case["p"]
        )
        if case["work"] < 1 or elements > MAX_ELEMENTS:
            raise SuiteError(f"case {case['name']} is empty or too large: {case}")
    plan = {"version": "suite-v2", "cases": cases}
    if len(json.dumps(plan, separators=(",", ":"))) > MAX_PLAN_BYTES:
        raise SuiteError("plan too large for the bench environment; fewer cases per run")
    return plan


def summarise(
    spec: dict,
    plan: dict,
    inc: list[dict[str, tuple[int, int]]],
    sub: list[dict[str, tuple[int, int]]],
) -> tuple[float, dict[str, dict[str, float]], list[list[float]], dict]:
    # The verdict's score, its per-cell classes, its per-round scores and its published evidence.
    timed = [c for c in plan["cases"] if c["timed"]]
    weights = cells(spec)
    members: dict[str, list[dict]] = {cell: [] for cell in weights}
    for case in timed:
        members[case["name"].rsplit(":", 1)[0]].append(case)

    def score(ratio_of: dict[str, float]) -> float:
        logs = {
            cell: sum(math.log(ratio_of[c["name"]]) for c in cs) / len(cs)
            for cell, cs in members.items()
        }
        return math.exp(sum(weights[cell] * lg for cell, lg in logs.items()))

    best = {
        c["name"]: (min(r[c["name"]][0] for r in inc), min(r[c["name"]][0] for r in sub))
        for c in timed
    }
    ratio = score({name: s / i for name, (i, s) in best.items()})
    classes: dict[str, dict[str, float]] = {}
    for cell, cs in members.items():
        ratios = [best[c["name"]][1] / best[c["name"]][0] for c in cs]
        spread_i = max(max(r[c["name"]][0] for r in inc) / best[c["name"]][0] for c in cs)
        spread_s = max(max(r[c["name"]][0] for r in sub) / best[c["name"]][1] for c in cs)
        classes[cell] = {
            "incumbent_ns": sum(best[c["name"]][0] / c["work"] for c in cs) / len(cs),
            "submission_ns": sum(best[c["name"]][1] / c["work"] for c in cs) / len(cs),
            "ratio": math.exp(sum(math.log(r) for r in ratios) / len(ratios)),
            "spread_inc": spread_i,
            "spread_sub": spread_s,
        }
    rounds = [
        [1.0, score({c["name"]: s[c["name"]][0] / i[c["name"]][0] for c in timed})]
        for i, s in zip(inc, sub)
    ]
    evidence = {
        "version": spec["version"],
        "sha256": spec["sha256"],
        "cells": weights,
        "cases": [c["name"].rsplit(":", 1)[0] for c in timed],
        "rounds": [[[i[c["name"]][0], s[c["name"]][0]] for c in timed] for i, s in zip(inc, sub)],
    }
    return ratio, classes, rounds, evidence
