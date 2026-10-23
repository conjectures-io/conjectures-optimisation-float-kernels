"""Local profile-stratified workload/round bootstrap; no promotion or DB writes."""

import hashlib
import json
import math
import random
import statistics

MODEL_NAMES = {"resnet": "cnn", "mobilenet": "mnet", "normalized_mlp": "nmlp"}
FAMILIES = ["overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer"]
PROFILES = {"typical": 0.5, "scale": 0.3, "boundary": 0.2, "legacy": 1}


def quantile(xs, q):
    xs = sorted(xs)
    position = (len(xs) - 1) * q
    i = int(position)
    return xs[i] + (xs[min(i + 1, len(xs) - 1)] - xs[i]) * (position - i)


def summarize(session, family, draws=2000, hierarchical=True):
    model = MODEL_NAMES.get(family, family)
    cells = [
        c
        for c in session["rounds"][0]["cells"]
        if family == "overall" or c.split("|", 1)[1].split(":", 1)[0] == model
    ]
    groups = {}
    for w in session["workloads"]:
        selected = [c for c in cells if c.startswith(w["id"] + "|")]
        scores = [
            math.exp(
                statistics.mean(
                    math.log(r["cells"][c]["candidate_ns"] / r["cells"][c]["reference_ns"])
                    for c in selected
                )
            )
            for r in session["rounds"]
        ]
        med = statistics.median(scores)
        guard = max(
            statistics.median(
                r["cells"][c]["candidate_ns"] / r["cells"][c]["reference_ns"]
                for r in session["rounds"]
            )
            for c in selected
        )
        groups[w["id"]] = {
            "scores": scores,
            "median": med,
            "guard": guard,
            "profile": w["profile"],
            "weight": w["weight"],
            "seed": w["seed"],
        }
    point = math.exp(
        sum(w["weight"] * math.log(groups[w["id"]]["median"]) for w in session["workloads"])
    )
    profiles = {
        p: [v for v in groups.values() if v["profile"] == p]
        for p in set(v["profile"] for v in groups.values())
    }
    rng = random.Random(
        int(hashlib.sha256(json.dumps(session, sort_keys=True).encode()).hexdigest()[:16], 16)
    )
    samples = []
    for _ in range(draws):
        terms = []
        # A batch round is one paired process: preserve timing covariance across workloads.
        indices = rng.choices(range(len(session["rounds"])), k=len(session["rounds"]))
        for profile, items in sorted(profiles.items()):
            chosen = rng.choices(items, k=len(items)) if hierarchical and len(items) > 1 else items
            weight = PROFILES[profile] / len(items)
            for item in chosen:
                med = statistics.median(item["scores"][i] for i in indices)
                terms.append(weight * math.log(med))
        samples.append(math.exp(sum(terms)))
    upper = quantile(samples, 0.95)
    lower = quantile(samples, 0.05)
    guard_ok = all(v["guard"] <= 1.10 for v in groups.values())
    outcome = (
        "exploratory"
        if len(session["rounds"]) < 5
        else "guard-failed"
        if not guard_ok
        else "above-bar"
        if point > 0.97
        else "not-significant"
        if upper >= 1
        else "qualifies"
    )
    return {
        "score": point,
        "ratio_lower": lower,
        "ratio_upper": upper,
        "outcome": outcome,
        "guard_passed": guard_ok,
        "worst_cell_median": max(v["guard"] for v in groups.values()),
        "workloads": {
            key: {k: v for k, v in value.items() if k != "scores"} for key, value in groups.items()
        },
    }
