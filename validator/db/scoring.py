"""What the scorer reads (each hotkey's best ratio), and what it writes back (the audit trail)."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt

from sqlalchemy.orm import Session, sessionmaker

import admission

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
            # Only the add board pays until the rewards say how emission splits across slots.
            for r in self._submissions.leaderboard("add")
        ]

    def admission(self, slot: str, bar: float) -> list[admission.Decision]:
        # The statistical admission decisions over every comparable accepted submission.
        evidence = [
            admission.Evidence(
                r.id,
                r.hotkey,
                float(r.ratio or 0.0),
                r.submitted_at,
                tuple((float(i), float(s)) for i, s in (r.rounds or [])),
            )
            for r in self._submissions.scorable(slot)
        ]
        return admission.decide(evidence, bar)

    def leader(self, bar: float) -> ScoredSubmission | None:
        # The add board's admitted leader, the one submission the weight setter pays.
        lead = admission.leader(self.admission("add", bar))
        if lead is None:
            return None
        row = self._submissions.get(lead)
        if row is None:
            return None
        return ScoredSubmission(row.id, row.hotkey, float(row.ratio or 0.0), row.submitted_at)

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
        api_snapshot: dict[str, object] | None = None,
    ) -> int:
        # One weight vector, the per-hotkey reasoning behind it and its published scoring, in one
        # transaction.
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
                api_snapshot=api_snapshot,
            )
            session.add(row)
            session.flush()
            for snap in snapshots or []:
                session.add(models.ScoreSnapshot(weight_set_id=row.id, **snap))
            return int(row.id)

    def weight_set_outcome(self, weight_set_id: int, *, accepted: bool, error: str | None) -> None:
        # The chain's answer for a weight set already recorded; its scoring stays as recorded.
        with session_scope(self._sessions) as session:
            row = session.get(models.WeightSet, weight_set_id)
            if row is None:
                raise LookupError(f"no weight set {weight_set_id}")
            row.accepted = accepted
            row.error = error
