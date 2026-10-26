"""Pinned workload policy shared by planning, measurement and scoring."""

import random

PROFILES = {"typical": 0.5, "scale": 0.3, "boundary": 0.2}


def workloads(seed):
    rng = random.Random(seed)
    seeds = rng.sample(range(2**32), 6)
    return [
        dict(
            id=f"s{i}",
            seed=s,
            profile=list(PROFILES)[i // 2],
            weight=list(PROFILES.values())[i // 2] / 2,
        )
        for i, s in enumerate(seeds)
    ]


def expanded(request):
    return {f"{w['id']}|{c}": w["weight"] for w in request["workloads"] for c in request["cells"]}


def calibrated_counts(rows, target_ms, previous=None):
    """Reference-only two-pass calibration, rounded up and bounded."""
    import math

    previous = previous or {}
    return {
        cell: max(1, min(1_000_000, math.ceil(previous.get(cell, 1) * target_ms * 1e6 / ns)))
        for cell, (ns, _) in rows.items()
    }
