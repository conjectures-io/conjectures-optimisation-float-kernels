"""Pinned workload policy shared by planning, measurement and scoring."""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass

PROFILES = {"typical": 0.5, "scale": 0.3, "boundary": 0.2}


@dataclass(frozen=True)
class WorkloadRecord:
    """Validated identity for one synthetic workload draw."""

    id: str
    seed: int
    profile: str
    weight: float

    @classmethod
    def parse(cls, raw: dict) -> WorkloadRecord:
        if not isinstance(raw, dict) or set(raw) != {"id", "seed", "profile", "weight"}:
            raise ValueError("invalid workload record fields")
        if not isinstance(raw["id"], str) or not raw["id"]:
            raise ValueError("workload id must be nonempty text")
        if type(raw["seed"]) is not int or not 0 <= raw["seed"] < 2**32:
            raise ValueError("workload seed must be UInt32")
        if raw["profile"] not in PROFILES:
            raise ValueError("unknown workload profile")
        weight = raw["weight"]
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight <= 0
        ):
            raise ValueError("workload weight must be positive")
        return cls(raw["id"], raw["seed"], raw["profile"], float(weight))

    def json(self) -> dict:
        return asdict(self)


def workload_records(seed: int) -> tuple[WorkloadRecord, ...]:
    rng = random.Random(seed)
    seeds = rng.sample(range(2**32), 6)
    names = tuple(PROFILES)
    return tuple(
        WorkloadRecord(
            id=f"s{i}",
            seed=value,
            profile=names[i // 2],
            weight=PROFILES[names[i // 2]] / 2,
        )
        for i, value in enumerate(seeds)
    )


def workloads(seed):
    return [record.json() for record in workload_records(seed)]


def expanded(request):
    return {f"{w['id']}|{c}": w["weight"] for w in request["workloads"] for c in request["cells"]}
