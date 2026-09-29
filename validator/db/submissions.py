"""The submission queue and the leaderboard."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from loguru import logger
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from . import clock, models
from .engine import session_scope
from .registrations import NoSlot, RegistrationsDb, _available_slots
from .status import PENDING, SubmissionState
from .verifier import required_fingerprint

# The columns the gate fills in; finish() refuses any other field instead of dropping it.
SCORE_FIELDS = frozenset(
    {
        "exit_code",
        "stage",
        "reason",
        "ratio",
        "classes",
        "seed",
        "incumbent_sha256",
        "verifier_fingerprint",
        "gate_seconds",
    }
)


class SubmissionsDb:
    # The submission queue, its verdicts and the leaderboard.
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        # Holds the session factory and the registrations an acceptance spends.
        self._sessions = sessions
        self._registrations = RegistrationsDb(sessions)

    # --- writing --------------------------------------------------------------
    def add(self, hotkey: str, digest: str, slot: str = "add") -> tuple[int, bool]:
        # Queue a submission; returns (id, fresh), and a resubmitted file keeps its id.
        with session_scope(self._sessions) as session:
            stmt = (
                insert(models.Submission)
                .values(
                    hotkey=hotkey,
                    slot=slot,
                    digest=digest,
                    submitted_at=clock.now(),
                    state=SubmissionState.QUEUED.value,
                )
                .on_conflict_do_nothing(index_elements=["hotkey", "slot", "digest"])
                .returning(models.Submission.id)
            )
            fresh_id = session.execute(stmt).scalar_one_or_none()
            if fresh_id is not None:
                return int(fresh_id), True
            existing = session.execute(
                select(models.Submission.id).where(
                    models.Submission.hotkey == hotkey,
                    models.Submission.slot == slot,
                    models.Submission.digest == digest,
                )
            ).scalar_one()
            return int(existing), False

    def claim_next(self, worker_id: str) -> models.Submission | None:
        # Claim the oldest queued submission, fewest gate crashes first, so one that crashes the
        # gate cannot hold up the rest; SKIP LOCKED lets several workers drain one queue.
        with session_scope(self._sessions) as session:
            row = session.execute(
                select(models.Submission)
                .where(models.Submission.state == SubmissionState.QUEUED.value)
                .order_by(
                    models.Submission.gate_errors,
                    models.Submission.submitted_at,
                    models.Submission.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.state = SubmissionState.VERIFYING.value
            row.worker_id = worker_id
            row.claimed_at = clock.now()
            row.verification_attempt = uuid.uuid4().hex
            session.flush()
            session.expunge(row)
            return row

    def finish(
        self,
        sub_id: int,
        state: SubmissionState,
        *,
        expected_attempt: str | None = None,
        **fields: object,
    ) -> str:
        # Record the verdict, and spend a registration in the same transaction only on acceptance.
        unknown = set(fields) - SCORE_FIELDS
        if unknown:
            raise ValueError(f"not submission columns: {sorted(unknown)}")
        with session_scope(self._sessions) as session:
            row = session.get(models.Submission, sub_id, with_for_update=True)
            if row is None:
                raise LookupError(f"no submission {sub_id}")
            if expected_attempt is not None and row.verification_attempt != expected_attempt:
                raise RuntimeError("stale worker claim")
            for key, value in fields.items():
                setattr(row, key, value)
            row.finished_at = clock.now()
            if state is SubmissionState.ACCEPTED:
                try:
                    registration = self._registrations.claim_slot(session, row.hotkey, sub_id)
                except NoSlot:
                    row.state = SubmissionState.REJECTED.value
                    row.reason = (
                        "accepted by the gate, but this hotkey has no unclaimed registration "
                        "left: one registration buys one accepted submission"
                    )
                    logger.warning(
                        f"[submissions] submission {sub_id} accepted with no slot left; rejected"
                    )
                    return SubmissionState.REJECTED.value
                logger.info(f"[submissions] submission {sub_id} spent registration {registration}")
            row.state = state.value
            return state.value

    def requeue(self, sub_id: int, *, expected_attempt: str | None = None) -> None:
        # Requeue without charging: the gate hit a validator-side error, not the miner's.
        with session_scope(self._sessions) as session:
            session.execute(
                update(models.Submission)
                .where(
                    models.Submission.id == sub_id,
                    models.Submission.verification_attempt == expected_attempt
                    if expected_attempt is not None
                    else True,
                )
                .values(
                    verification_attempt=None,
                    state=SubmissionState.QUEUED.value,
                    worker_id=None,
                    claimed_at=None,
                )
            )

    def gate_crashed(
        self,
        sub_id: int,
        *,
        max_errors: int,
        expected_attempt: str | None = None,
        **fields: object,
    ) -> str:
        # The gate crashed on this submission: requeue it, or at `max_errors` end it in `error`
        # with the gate's verdict; either way uncharged. Returns the new state.
        unknown = set(fields) - SCORE_FIELDS
        if unknown:
            raise ValueError(f"not submission columns: {sorted(unknown)}")
        with session_scope(self._sessions) as session:
            row = session.get(models.Submission, sub_id, with_for_update=True)
            if row is None:
                raise LookupError(f"no submission {sub_id}")
            if expected_attempt is not None and row.verification_attempt != expected_attempt:
                raise RuntimeError("stale worker claim")
            row.gate_errors += 1
            row.verification_attempt = None
            row.worker_id = None
            row.claimed_at = None
            if row.gate_errors < max_errors:
                row.state = SubmissionState.QUEUED.value
                return row.state
            for key, value in fields.items():
                setattr(row, key, value)
            row.finished_at = clock.now()
            row.state = SubmissionState.ERROR.value
            logger.warning(
                f"[submissions] submission {sub_id} crashed the gate {row.gate_errors} times; "
                "given up"
            )
            return row.state

    def requeue_stale(self, older_than_seconds: float) -> int:
        # Requeue work a dead worker left mid-gate; the age must exceed the gate's own timeout.
        cutoff = clock.now() - dt.timedelta(seconds=older_than_seconds)
        with session_scope(self._sessions) as session:
            result = session.execute(
                update(models.Submission)
                .where(
                    models.Submission.state == SubmissionState.VERIFYING.value,
                    models.Submission.claimed_at < cutoff,
                )
                .values(
                    verification_attempt=None,
                    state=SubmissionState.QUEUED.value,
                    worker_id=None,
                    claimed_at=None,
                )
            )
            return int(cast("CursorResult[Any]", result).rowcount or 0)

    def reverify(self, fingerprint: str) -> list[int]:
        # Send every accepted verdict from another verifier back through the gate; it keeps the
        # registration it spent (claim_slot is idempotent), so a re-acceptance costs nothing.
        with session_scope(self._sessions) as session:
            ids = session.execute(
                update(models.Submission)
                .where(
                    models.Submission.state == SubmissionState.ACCEPTED.value,
                    models.Submission.verifier_fingerprint.is_distinct_from(fingerprint),
                )
                .values(
                    state=SubmissionState.QUEUED.value,
                    verification_attempt=None,
                    worker_id=None,
                    claimed_at=None,
                    gate_errors=0,
                )
                .returning(models.Submission.id)
            )
            return sorted(int(i) for i in ids.scalars())

    def stale(self, fingerprint: str) -> list[models.Submission]:
        # Accepted submissions whose verdict came from another verifier, oldest first.
        with session_scope(self._sessions) as session:
            rows = (
                session.execute(
                    select(models.Submission)
                    .where(
                        models.Submission.state == SubmissionState.ACCEPTED.value,
                        models.Submission.verifier_fingerprint.is_distinct_from(fingerprint),
                    )
                    .order_by(models.Submission.submitted_at, models.Submission.id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    # --- reading --------------------------------------------------------------
    def get(self, sub_id: int) -> models.Submission | None:
        # One submission, detached so it stays readable after the session.
        with session_scope(self._sessions) as session:
            row = session.get(models.Submission, sub_id)
            if row is not None:
                session.expunge(row)
            return row

    def may_queue(self, hotkey: str) -> tuple[bool, int, int]:
        # (allowed, slots, pending): a hotkey may not queue more work than its slots can pay for.
        with session_scope(self._sessions) as session:
            slots = _available_slots(session, hotkey)
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
            return pending < slots, slots, pending

    def queue_depth(self) -> int:
        # How many submissions are queued or being verified.
        with session_scope(self._sessions) as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(models.Submission)
                    .where(models.Submission.state.in_([s.value for s in PENDING]))
                ).scalar_one()
            )

    def current_incumbent(self, slot: str = "add") -> str | None:
        # The slot's incumbent at its newest verdict; a promotion makes older ratios incomparable.
        with session_scope(self._sessions) as session:
            return session.execute(
                select(models.Submission.incumbent_sha256)
                .where(
                    models.Submission.slot == slot,
                    models.Submission.incumbent_sha256.is_not(None),
                )
                .order_by(models.Submission.id.desc())
                .limit(1)
            ).scalar_one_or_none()

    def leaderboard(self, slot: str = "add") -> list[models.Submission]:
        # Each hotkey's best accepted ratio vs the slot's incumbent, fastest first, ties earlier.
        # Only verdicts from the current verifier count; tools/reverify.py re-stamps older ones.
        incumbent = self.current_incumbent(slot)
        if incumbent is None:
            return []
        fingerprint = required_fingerprint()
        with session_scope(self._sessions) as session:
            rows = (
                session.execute(
                    select(models.Submission)
                    .distinct(models.Submission.hotkey)
                    .where(
                        models.Submission.slot == slot,
                        models.Submission.state == SubmissionState.ACCEPTED.value,
                        models.Submission.ratio.is_not(None),
                        models.Submission.incumbent_sha256 == incumbent,
                        models.Submission.verifier_fingerprint == fingerprint,
                    )
                    .order_by(
                        models.Submission.hotkey,
                        models.Submission.ratio,
                        models.Submission.submitted_at,
                        models.Submission.id,
                    )
                )
                .scalars()
                .all()
            )
            ordered = sorted(rows, key=lambda r: (r.ratio, r.submitted_at, r.id))
            for row in ordered:
                session.expunge(row)
            return ordered
