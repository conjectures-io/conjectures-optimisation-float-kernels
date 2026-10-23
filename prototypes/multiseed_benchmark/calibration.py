"""Reference-only warm calibration used by the local six-workload experiment."""

import math


def calibrated_counts(rows, target_ms, previous=None):
    previous = previous or {}
    return {
        key: max(1, min(1000000, math.ceil(previous.get(key, 1) * target_ms * 1e6 / row["ns"])))
        for key, row in rows.items()
    }


def calibrate(reference, target_ms=50):
    """Return repetitions after two reference-only best-of-three passes.

    ``reference(inner=3, repetitions={...})`` executes the pinned incumbent and
    returns ``{"rows": {"workload|cell": {"ns": total_nanoseconds, ...}}}``.
    The caller validates the measured rows before returning them. Empty counts
    mean one inference per cell. Both candidate and reference subsequently use
    the returned counts.
    """
    initial = reference(inner=3, repetitions={})
    repetitions = calibrated_counts(initial["rows"], target_ms)
    verified = reference(inner=3, repetitions=repetitions)
    return calibrated_counts(verified["rows"], target_ms, repetitions)
