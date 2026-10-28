"""Pure paired scoring; no database connection or machine-specific timing comparisons."""

from __future__ import annotations

import math
import random
from statistics import median

from config import digest, measurement_hash


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
    from multiseed import expanded

    if result.get("workloads") != request["workloads"]:
        raise ValueError("workload plan mismatch")
    cells = list(expanded(request))
    if cell_weights:
        cell_weights = {
            f"{w['id']}|{c}": w["weight"] * weight
            for w in request["workloads"]
            for c, weight in cell_weights.items()
        }
    if set(reps) != set(cells) or any(
        type(n) is not int or not 1 <= n <= 1_000_000 for n in reps.values()
    ):
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
        for cell, weight in (cell_weights or dict.fromkeys(cells, 1.0)).items():
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
            checksums = (obs.get("reference_checksum"), obs.get("candidate_checksum"))
            if (
                any(type(x) is not int or not 0 <= x < 2**32 for x in checksums)
                or checksums[0] != checksums[1]
            ):
                raise ValueError("checksum discrepancy")
            ratios.append((b / a, weight))
        scores.append(geomean(ratios))
    return scores


def assess(plan, measurements):
    from multiseed import PROFILES

    measurements = sorted(measurements, key=lambda pair: (pair[0]["environment"], pair[0]["seed"]))

    expected = {(s["environment"], s["seed"]): s for s in plan["strata"]}
    seen = set()
    environments = []
    diagnostics = []
    for request, result in measurements:
        key = request["environment"], request["seed"]
        if key not in expected or key in seen:
            raise ValueError("unexpected or duplicate stratum")
        seen.add(key)
        st = expected[key]
        if request["identity"] != st["identity"] or set(request["cells"]) != set(plan["cells"]):
            raise ValueError("incompatible stratum")
        if (
            request["timing"] != st.get("timing", plan["timing"])
            or request["benchmark_identity"] != plan["benchmark_identity"]
        ):
            raise ValueError("incompatible measurement policy")
        validate_result(request, result, plan["cells"])
        groups = []
        guards = {}
        for w in request["workloads"]:
            scores = []
            for r in result["rounds"]:
                scores.append(
                    geomean(
                        [
                            (
                                r["cells"][f"{w['id']}|{c}"]["candidate_ns"]
                                / r["cells"][f"{w['id']}|{c}"]["reference_ns"],
                                weight,
                            )
                            for c, weight in plan["cells"].items()
                        ]
                    )
                )
            if len(scores) < plan["admission"]["min_rounds"]:
                raise ValueError("insufficient rounds")
            groups.append((w, scores))
            for c in plan["cells"]:
                k = f"{w['id']}|{c}"
                guards[k] = median(
                    r["cells"][k]["candidate_ns"] / r["cells"][k]["reference_ns"]
                    for r in result["rounds"]
                )
        score = geomean([(median(xs), w["weight"]) for w, xs in groups])
        diagnostics.append(dict(environment=key[0], seed=key[1], score=score, guards=guards))
        environments.append((groups, st["weight"]))
    if seen != set(expected):
        raise ValueError("incomplete required environments/seeds")
    point = geomean(
        [(d["score"], expected[(d["environment"], d["seed"])]["weight"]) for d in diagnostics]
    )
    rng = random.Random(int(digest({"plan": plan, "measurements": measurements})[:16], 16))
    draws = []
    for _ in range(plan["admission"]["draws"]):
        envscores = []
        for groups, envweight in environments:
            n = len(groups[0][1])
            indices = rng.choices(range(n), k=n)
            terms = []
            for p, weight in PROFILES.items():
                items = [(w, xs) for w, xs in groups if w["profile"] == p]
                for w, xs in rng.choices(items, k=len(items)):
                    terms.append((median(xs[i] for i in indices), weight / len(items)))
            envscores.append((geomean(terms), envweight))
        draws.append(1 - geomean(envscores))
    lower = quantile(draws, plan["admission"]["quantile"])
    guard = all(v <= plan["admission"]["guard"] for d in diagnostics for v in d["guards"].values())
    outcome = (
        "guard-failed"
        if not guard
        else "above-bar"
        if point > plan["admission"]["bar"]
        else "not-significant"
        if lower <= 0
        else "qualifies"
    )
    return dict(
        score=point, gain_lower=lower, guard_passed=guard, outcome=outcome, strata=diagnostics
    )
