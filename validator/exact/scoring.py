"""Pure paired scoring; no database connection or machine-specific timing comparisons."""

from __future__ import annotations

import math
import random
from statistics import median

from .config import digest, measurement_hash


def geomean(values: list[tuple[float, float]]) -> float:
    return math.exp(sum(math.log(v) * w for v, w in values) / sum(w for _, w in values))


def quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)


def validate_result(request: dict, result: dict, cell_weights: dict | None = None) -> list[float]:
    if result.get("request_hash") != measurement_hash(request):
        raise ValueError("measurement request mismatch")
    provenance = result.get("provenance", {})
    if provenance.get("identity") != request["identity"] or not provenance.get("machine"):
        raise ValueError("environment provenance mismatch")
    reps = result.get("repetitions", {})
    cells = request["cells"]
    if set(reps) != set(cells) or any(type(n) is not int or n <= 0 for n in reps.values()):
        raise ValueError("invalid repetition counts")
    rounds = result.get("rounds", [])
    if len(rounds) != request["timing"]["rounds"]:
        raise ValueError("missing paired rounds")
    scores = []
    for i, row in enumerate(rounds):
        if row.get("reference_first") is not (i % 2 == 0):
            raise ValueError("invalid alternating order")
        if set(row.get("cells", {})) != set(cells):
            raise ValueError("missing or extra cells")
        ratios = []
        for cell, weight in (cell_weights or cells).items():
            obs = row["cells"][cell]
            a, b = obs["reference_ns"], obs["candidate_ns"]
            if any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                or x <= 0
                for x in (a, b)
            ):
                raise ValueError("invalid paired timing")
            if type(obs.get("reference_checksum")) is not int or obs[
                "reference_checksum"
            ] != obs.get("candidate_checksum"):
                raise ValueError("checksum discrepancy")
            ratios.append((b / a, weight))
        scores.append(geomean(ratios))
    return scores


def assess(plan: dict, measurements: list[tuple[dict, dict]]) -> dict:
    expected = {(s["environment"], s["seed"]): s for s in plan["strata"]}
    seen = set()
    samples, diagnostics = [], []
    for request, result in measurements:
        key = request["environment"], request["seed"]
        if key not in expected or key in seen:
            raise ValueError("unexpected or duplicate stratum")
        stratum = expected[key]
        for field in ("identity",):
            if request[field] != stratum[field]:
                raise ValueError("incompatible stratum identity")
        if set(request["cells"]) != set(plan["cells"]):
            raise ValueError("incompatible cell set")
        for field in ("timing", "proof_identity", "benchmark_identity"):
            if request[field] != plan[field]:
                raise ValueError(f"incompatible {field}")
        seen.add(key)
        scores = validate_result(request, result, plan["cells"])
        if len(scores) < plan["admission"]["min_rounds"]:
            raise ValueError("insufficient rounds")
        guards = {
            cell: median(
                r["cells"][cell]["candidate_ns"] / r["cells"][cell]["reference_ns"]
                for r in result["rounds"]
            )
            for cell in plan["cells"]
        }
        diagnostics.append(
            {"environment": key[0], "seed": key[1], "score": median(scores), "guards": guards}
        )
        samples.append((scores, stratum["weight"]))
    if seen != set(expected):
        raise ValueError("incomplete required environments/seeds")
    score = geomean([(median(xs), w) for xs, w in samples])
    rng = random.Random(int(digest({"plan": plan, "measurements": measurements})[:16], 16))
    gains = [
        1 - geomean([(median(rng.choices(xs, k=len(xs))), w) for xs, w in samples])
        for _ in range(plan["admission"]["draws"])
    ]
    lower = quantile(gains, plan["admission"]["quantile"])
    guard_ok = all(
        v <= plan["admission"]["guard"] for d in diagnostics for v in d["guards"].values()
    )
    outcome = (
        "guard-failed"
        if not guard_ok
        else "above-bar"
        if score > plan["admission"]["bar"]
        else "not-significant"
        if lower <= 0
        else "qualifies"
    )
    return {
        "score": score,
        "gain_lower": lower,
        "guard_passed": guard_ok,
        "outcome": outcome,
        "strata": diagnostics,
    }
