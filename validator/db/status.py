"""The submission lifecycle, as the strings the API has always returned."""

from __future__ import annotations

from enum import StrEnum


class SubmissionState(StrEnum):
    # Waiting for a gate worker to claim it.
    QUEUED = "queued"
    # Claimed; gate.py is running against it.
    VERIFYING = "verifying"
    # Gate exit 0: proven and scored; it spends the entry's registration.
    ACCEPTED = "accepted"
    # Gate exit 1: the submission was refused; it spends the entry's registration too.
    REJECTED = "rejected"
    # The gate crashed or gave no verdict on this submission SERVICE_MAX_GATE_ERRORS times, so it is
    # given up uncharged (fewer times requeue it instead).
    ERROR = "error"


# The states that still count against a hotkey's slots and the queue depth.
PENDING = (SubmissionState.QUEUED, SubmissionState.VERIFYING)

STATE_VALUES = tuple(s.value for s in SubmissionState)

# The competition slots, as gate.py names them: `full` is the paid entry, the rest practice boards.
SLOTS = ("full", "add", "mul", "fma", "div", "sqrt", "sum", "bmm", "exact")
