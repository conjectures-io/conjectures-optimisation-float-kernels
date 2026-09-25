"""The submission lifecycle, as the strings the API has always returned."""

from __future__ import annotations

from enum import StrEnum


class SubmissionState(StrEnum):
    # Waiting for a gate worker to claim it.
    QUEUED = "queued"
    # Claimed; gate.py is running against it.
    VERIFYING = "verifying"
    # Gate exit 0: proven and scored; the only state that spends a registration.
    ACCEPTED = "accepted"
    # Gate exit 1: the submission was refused, and a corrected one may be sent for free.
    REJECTED = "rejected"
    # Gate exit 2 or no exit: the validator is at fault; requeued, never charged.
    ERROR = "error"


# The states that still count against a hotkey's slots and the queue depth.
PENDING = (SubmissionState.QUEUED, SubmissionState.VERIFYING)

STATE_VALUES = tuple(s.value for s in SubmissionState)
