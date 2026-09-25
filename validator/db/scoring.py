"""What the scorer reads (each hotkey's best ratio), and what it writes back (the audit trail)."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt

from sqlalchemy.orm import Session, sessionmaker

from . import models
from .engine import session_scope
from .submissions import SubmissionsDb


@dc.dataclass(frozen=True, slots=True)
class ScoredSubmission:
    # One accepted submission, reduced to what scoring uses; `ratio` is lower-is-faster.
    submission_id: int
    hotkey: str
    ratio: float
    submitted_at: dt.datetime


class ScoringDb:
    # What the weight setter scores: each hotkey's best accepted ratio.
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        # Reads through the leaderboard query, so what a miner sees ranked is what gets scored.
        self._sessions = sessions
        self._submissions = SubmissionsDb(sessions)

    def best_per_hotkey(self) -> list[ScoredSubmission]:
        # Each hotkey's best accepted ratio against the current incumbent, fastest first.
        return [
            ScoredSubmission(r.id, r.hotkey, float(r.ratio or 0.0), r.submitted_at)
            for r in self._submissions.leaderboard()
        ]

    def record_weight_set(
        self,
        *,
        netuid: int,
        block: int,
        uids: list[int],
        weights: list[float],
        summary: str | None,
        accepted: bool,
        dry_run: bool = False,
        error: str | None = None,
        snapshots: list[dict[str, object]] | None = None,
    ) -> int:
        # One weight vector and the per-hotkey reasoning behind it, in one transaction.
        with session_scope(self._sessions) as session:
            row = models.WeightSet(
                netuid=netuid,
                block=block,
                uids=list(uids),
                weights=[float(w) for w in weights],
                summary=summary,
                accepted=accepted,
                dry_run=dry_run,
                error=error,
            )
            session.add(row)
            session.flush()
            for snap in snapshots or []:
                session.add(models.ScoreSnapshot(weight_set_id=row.id, **snap))
            return int(row.id)
