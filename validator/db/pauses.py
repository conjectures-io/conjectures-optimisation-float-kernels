"""Persistent slot outages. No database transaction spans a gate subprocess."""

from __future__ import annotations

import datetime as dt
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import clock, models
from .engine import session_scope


class PausesDb:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def all(self) -> dict[str, models.SlotPause]:
        with session_scope(self._sessions) as session:
            rows = session.scalars(select(models.SlotPause)).all()
            session.expunge_all()
            return {r.slot: r for r in rows}

    def fail(
        self,
        sub: models.Submission,
        *,
        reference_id: int | None,
        reference_sha256: str | None,
        phase: str,
        reason: str,
        retry_seconds: float,
    ) -> bool:
        """Pause and requeue atomically, but refuse results from an abandoned attempt."""
        with session_scope(self._sessions) as session:
            row = session.get(models.Submission, sub.id, with_for_update=True)
            if row is None or row.verification_attempt != sub.verification_attempt:
                return False
            pause = session.get(models.SlotPause, sub.slot, with_for_update=True)
            changed = pause is None or (pause.phase, pause.reason) != (phase, reason[:2000])
            now = clock.now()
            if pause is None:
                pause = models.SlotPause(slot=sub.slot, paused_at=now)
                session.add(pause)
            pause.challenger_id = sub.id
            pause.reference_id = reference_id
            pause.reference_sha256 = reference_sha256
            pause.revision = uuid.uuid4().hex
            pause.last_failure_at = now
            pause.next_retry_at = now + dt.timedelta(seconds=retry_seconds)
            pause.phase, pause.reason = phase, reason[:2000]
            row.state = "queued"
            row.worker_id = row.claimed_at = row.verification_attempt = None
        (logger.warning if changed else logger.debug)(
            "[slots] {} paused: {}: {}", sub.slot, phase, reason[:2000]
        )
        return True

    def recover(self, sub: models.Submission, revision: str | None) -> bool:
        """Only the still-owning attempt may clear the pause it observed before running."""
        if revision is None:
            return False
        with session_scope(self._sessions) as session:
            row = session.get(models.Submission, sub.id, with_for_update=True)
            pause = session.get(models.SlotPause, sub.slot, with_for_update=True)
            if (
                row is None
                or row.verification_attempt != sub.verification_attempt
                or pause is None
                or pause.revision != revision
            ):
                return False
            session.delete(pause)
        logger.info("[slots] {} recovered; verification resumed", sub.slot)
        return True

    def defer(self, sub: models.Submission, revision: str | None, retry_seconds: float) -> None:
        # An early challenger failure has not tested the reference. Keep the cooldown measured
        # from completion, not from when a potentially long verification started.
        if revision is None:
            return
        with session_scope(self._sessions) as session:
            row = session.get(models.Submission, sub.id, with_for_update=True)
            pause = session.get(models.SlotPause, sub.slot, with_for_update=True)
            if (
                row is not None
                and row.verification_attempt == sub.verification_attempt
                and pause is not None
                and pause.revision == revision
            ):
                pause.next_retry_at = clock.now() + dt.timedelta(seconds=retry_seconds)

    def retry(self, slot: str) -> None:
        with session_scope(self._sessions) as session:
            pause = session.get(models.SlotPause, slot, with_for_update=True)
            if pause is None:
                raise ValueError(f"{slot} is not paused")
            pause.next_retry_at = clock.now()
            pause.revision = uuid.uuid4().hex
        logger.info("[slots] {} immediate retry requested", slot)

    def demote(self, slot: str, submission_id: int, reason: str) -> None:
        """Caller holds the gate lock; require an explicit, still-current champion identity."""
        with session_scope(self._sessions) as session:
            promotion = session.scalar(
                select(models.Promotion)
                .where(models.Promotion.slot == slot, models.Promotion.demoted_at.is_(None))
                .order_by(models.Promotion.id.desc())
                .limit(1)
                .with_for_update()
            )
            if promotion is None or promotion.submission_id != submission_id:
                raise ValueError("the named submission is no longer the current champion")
            now = clock.now()
            promotion.demoted_at = now
            pause = session.get(models.SlotPause, slot, with_for_update=True)
            if pause is None:
                pause = models.SlotPause(slot=slot, paused_at=now, last_failure_at=now)
                session.add(pause)
            pause.reference_id = None
            pause.reference_sha256 = None
            pause.revision = uuid.uuid4().hex
            pause.next_retry_at = now
            pause.phase = "operator-demotion"
            pause.reason = reason[:2000]
        logger.warning("[slots] {} champion {} demoted: {}", slot, submission_id, reason)
