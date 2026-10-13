"""Retired intake rules, used only by the historical worker regression harness."""

from loguru import logger
from sqlalchemy import func, select

from db import PENDING, SubmissionState, clock, models
from db.engine import session_scope
from db.registrations import _available_slots

KERNEL = "Kernel.lean"


class QueueFull(Exception):
    # A hotkey already has as much work queued as its unclaimed registrations can pay for.
    def __init__(self, slots: int, pending: int) -> None:
        super().__init__(f"{slots} unclaimed registration(s) and {pending} already in the queue")
        self.slots, self.pending = slots, pending


def enqueue(self, hotkey: str, digest: str, slot: str, kernel: bytes) -> tuple[int, bool, int]:
    # Queue a submission if the hotkey can pay for it, atomically under a per-hotkey lock, so
    # two API processes cannot both spend one free registration; returns (id, fresh, slots).
    with session_scope(self._sessions) as session:
        session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(hotkey, 0))))
        slots = _available_slots(session, hotkey)
        existing = session.execute(
            select(models.Submission)
            .where(
                models.Submission.hotkey == hotkey,
                models.Submission.slot == slot,
                models.Submission.digest == digest,
            )
            .with_for_update()
        ).scalar_one_or_none()
        pending = int(
            session.execute(
                select(func.count())
                .select_from(models.Submission)
                .where(
                    models.Submission.hotkey == hotkey,
                    models.Submission.state.in_([s.value for s in PENDING]),
                )
            ).scalar_one()
        )
        if existing is not None:
            # A retry of the same file is the same submission, even with no registration left;
            # one the gate gave up on goes back in the queue when the hotkey can pay for it.
            if existing.state == SubmissionState.ERROR.value and pending < slots:
                existing.state = SubmissionState.QUEUED.value
                existing.gate_errors = 0
                existing.submitted_at = clock.now()
                existing.exit_code = existing.stage = existing.reason = None
                existing.finished_at = None
                logger.info(f"[submissions] submission {existing.id} resent after an error")
            return int(existing.id), False, slots
        if pending >= slots:
            raise QueueFull(slots, pending)
        row = models.Submission(
            hotkey=hotkey,
            slot=slot,
            digest=digest,
            submitted_at=clock.now(),
            state=SubmissionState.QUEUED.value,
        )
        session.add(row)
        session.flush()
        session.add(models.SubmissionFile(submission_id=row.id, name=KERNEL, content=kernel))
        return int(row.id), True, slots
