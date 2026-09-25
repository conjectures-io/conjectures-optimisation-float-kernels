"""The wire contract: what each endpoint returns."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class Health(BaseModel):
    # Liveness, the queue length (null when the store is down) and the promotion bar.
    ok: bool
    queued: int | None
    promotion_bar: float


class Ready(Health):
    # False when the process is up but the database is not reachable.
    database: bool


class SubmitAccepted(BaseModel):
    # The queued submission's id, and how many registrations the hotkey has left to spend.
    submission: int
    slot: str
    state: str
    digest: str
    slots_remaining: int


class SubmissionView(BaseModel):
    # One submission's state and, once gated, its verdict.
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    hotkey: str
    slot: str
    digest: str
    submitted_at: str
    state: str
    exit_code: int | None = None
    stage: str | None = None
    reason: str | None = None
    ratio: float | None = None
    classes: dict[str, object] | None = None
    seed: int | None = None
    incumbent_sha256: str | None = None


class Ranking(BaseModel):
    # One hotkey's best accepted submission against the current incumbent.
    rank: int
    submission: int
    hotkey: str
    ratio: float
    qualifies: bool
    submitted_at: str


class Leaderboard(BaseModel):
    # One slot's board, fastest first; `qualifies` marks ratios at or below the promotion bar.
    slot: str
    incumbent_sha256: str | None = None
    promotion_bar: float
    ranking: list[Ranking]
