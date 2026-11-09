"""Execution budgets shared by the coordinator config and benchmark workers."""

import math

SUPERVISOR_GRACE_SECONDS = 60
DEFAULT_TIMING = dict(
    rounds=12,
    inner=1,
    baseline_rounds=3,
    run_seconds=300,
    build_seconds=960,
    cached_build_seconds=180,
    benchmark_seconds=1200,
)


def baseline_round_indices(rounds: int, count: int) -> tuple[int, ...]:
    """Space same-input baseline sentinels across the paired evaluation timeline."""
    if type(rounds) is not int or rounds < 1 or type(count) is not int or count < 0:
        raise ValueError(
            "baseline sentinel schedule requires positive rounds and nonnegative count"
        )
    count = min(rounds, count)
    if count == 0:
        return ()
    if count == 1:
        return (rounds // 2,)
    denominator = 2 * (count - 1)
    return tuple((2 * index * (rounds - 1) + (count - 1)) // denominator for index in range(count))


def build_defaults(architecture: str = "arm64") -> dict:
    if architecture in ("amd64", "x86_64"):
        return dict(build_seconds=300, cached_build_seconds=120)
    if architecture in ("arm64", "aarch64"):
        return dict(build_seconds=960, cached_build_seconds=180)
    raise ValueError("build architecture must be amd64/x86_64 or arm64/aarch64")


def validate_timing(timing: dict) -> None:
    required = {"rounds", "inner", "run_seconds", "build_seconds", "benchmark_seconds"}
    optional = {
        "cached_build_seconds",
        "baseline_rounds",
        "shared_build_seconds",
        "baseline_build_seconds",
        "reference_build_seconds",
        "candidate_build_seconds",
    }
    if not required <= timing.keys() or set(timing) - required - optional:
        raise ValueError("timing requires " + ", ".join(sorted(required)))
    for key, value in timing.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"timing {key} must be finite and positive")
    if any(
        type(timing[k]) is not int for k in ("rounds", "inner", "baseline_rounds") if k in timing
    ):
        raise ValueError("round counts must be integers")
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


def build_stage_seconds(timing: dict, stage: str) -> float:
    """Return a role-specific cap, inheriting the overall build cap until calibrated."""
    if stage not in ("shared", "baseline", "reference", "candidate"):
        raise ValueError("unknown build stage")
    return timing.get(f"{stage}_build_seconds", timing["build_seconds"])


def supervisor_seconds(timing: dict, mode: str = "cold") -> float:
    validate_timing(timing)
    return (
        preparation_seconds(timing, mode) + timing["benchmark_seconds"] + SUPERVISOR_GRACE_SECONDS
    )
