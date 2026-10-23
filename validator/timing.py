"""Execution budgets shared by the coordinator config and benchmark workers."""

import math

SUPERVISOR_GRACE_SECONDS = 60


def validate_timing(timing: dict) -> None:
    required = {"rounds", "inner", "cell_ms", "run_seconds", "build_seconds", "benchmark_seconds"}
    if set(timing) != required:
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


def supervisor_seconds(timing: dict) -> float:
    validate_timing(timing)
    return timing["build_seconds"] + timing["benchmark_seconds"] + SUPERVISOR_GRACE_SECONDS
