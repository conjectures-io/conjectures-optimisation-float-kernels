"""Execution budgets shared by the coordinator config and benchmark workers."""

import math

SUPERVISOR_GRACE_SECONDS = 60
DEFAULT_TIMING = dict(
    rounds=11,
    inner=3,
    cell_ms=50,
    run_seconds=300,
    build_seconds=960,
    cached_build_seconds=180,
    benchmark_seconds=1200,
)


def build_defaults(architecture: str = "arm64") -> dict:
    if architecture in ("amd64", "x86_64"):
        return dict(build_seconds=300, cached_build_seconds=120)
    if architecture in ("arm64", "aarch64"):
        return dict(build_seconds=960, cached_build_seconds=180)
    raise ValueError("build architecture must be amd64/x86_64 or arm64/aarch64")


def validate_timing(timing: dict) -> None:
    required = {"rounds", "inner", "cell_ms", "run_seconds", "build_seconds", "benchmark_seconds"}
    if not required <= timing.keys() or set(timing) - required - {"cached_build_seconds"}:
        raise ValueError("timing requires " + ", ".join(sorted(required)))
    for key, value in timing.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"timing {key} must be finite and positive")
    if any(type(timing[k]) is not int for k in ("rounds", "inner")):
        raise ValueError("rounds and inner must be integers")
    if timing.get("cached_build_seconds", 0) > timing["build_seconds"]:
        raise ValueError("cached build limit must not exceed the cold build limit")


def preparation_seconds(timing: dict, mode: str = "cold") -> float:
    if mode not in ("cold", "cached"):
        raise ValueError("unknown preparation mode")
    return (
        timing.get("cached_build_seconds", timing["build_seconds"])
        if mode == "cached"
        else timing["build_seconds"]
    )


def supervisor_seconds(timing: dict, mode: str = "cold") -> float:
    validate_timing(timing)
    return (
        preparation_seconds(timing, mode) + timing["benchmark_seconds"] + SUPERVISOR_GRACE_SECONDS
    )
