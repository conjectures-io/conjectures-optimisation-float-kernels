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

# The columns the gate fills in; finish() refuses any other field instead of dropping it.
SCORE_FIELDS = frozenset(
    {
        "exit_code",
        "stage",
        "reason",
        "ratio",
        "classes",
        "rounds",
        "seed",
        "incumbent_sha256",
        "champion_sha256",
        "verifier_fingerprint",
        "gate_seconds",
        "report",
    }
)
# The one file a submission is; its name in submission_files.
KERNEL = "Kernel.lean"


def _current_champion(session: Session, slot: str) -> str | None:
    # The latest standing promotion's champion source; with every promotion demoted, the one the
    # first promoted submission was timed against (the shipped one); else the latest verdict's.
    standing = session.execute(
        select(models.Promotion.champion_sha256)
        .where(models.Promotion.slot == slot, models.Promotion.demoted_at.is_(None))
        .order_by(models.Promotion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if standing is not None:
        return standing
    shipped = session.execute(
        select(models.Submission.champion_sha256)
        .join(models.Promotion, models.Promotion.submission_id == models.Submission.id)
        .where(models.Promotion.slot == slot)
        .order_by(models.Promotion.id)
        .limit(1)
    ).scalar_one_or_none()
    if shipped is not None:
        return shipped
    return session.execute(
        select(models.Submission.champion_sha256)
        .where(models.Submission.slot == slot, models.Submission.champion_sha256.is_not(None))
        .order_by(models.Submission.id.desc())
        .limit(1)
    ).scalar_one_or_none()


class QueueFull(Exception):
    # A hotkey already has as much work queued as its unclaimed registrations can pay for.
    def __init__(self, slots: int, pending: int) -> None:
        super().__init__(f"{slots} unclaimed registration(s) and {pending} already in the queue")
        self.slots, self.pending = slots, pending


class SubmissionsDb:
    # The submission queue, its verdicts and the leaderboard.
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        # Holds the session factory and the registrations an acceptance spends.
        self._sessions = sessions
        self._registrations = RegistrationsDb(sessions)

    # --- writing --------------------------------------------------------------
    def add(
        self, hotkey: str, digest: str, slot: str = "add", kernel: bytes | None = None
    ) -> tuple[int, bool]:
        # Queue a submission and its Kernel.lean in one transaction; returns (id, fresh), and a
        # resubmitted file keeps its id.
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
                if kernel is not None:
                    session.add(
                        models.SubmissionFile(submission_id=fresh_id, name=KERNEL, content=kernel)
                    )
                return int(fresh_id), True
            existing = session.execute(
                select(models.Submission.id).where(
                    models.Submission.hotkey == hotkey,
                    models.Submission.slot == slot,
                    models.Submission.digest == digest,
                )
            ).scalar_one()
            return int(existing), False

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
        promote: bool = False,
        expected_attempt: str | None = None,
        **fields: object,
    ) -> str:
        # Record the verdict and spend a registration in the same transaction, accepted or rejected,
        # since one registration buys one entry; an accepted one with `promote` becomes champion.
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
                        "left: one registration buys one entry"
                    )
                    logger.warning(
                        f"[submissions] submission {sub_id} accepted with no slot left; rejected"
                    )
                    return SubmissionState.REJECTED.value
                logger.info(f"[submissions] submission {sub_id} spent registration {registration}")
                if promote:
                    # One promotion per slot at a time: two winners can't replace one champion.
                    session.execute(
                        select(
                            func.pg_advisory_xact_lock(
                                func.hashtextextended(f"promote:{row.slot}", 0)
                            )
                        )
                    )
                promoted = session.execute(
                    select(models.Promotion.id).where(models.Promotion.submission_id == sub_id)
                ).first()
                current = _current_champion(session, row.slot)
                if promote and promoted is None and row.champion_sha256 == current:
                    session.add(
                        models.Promotion(
                            slot=row.slot,
                            submission_id=sub_id,
                            champion_sha256=row.digest,
                            promoted_at=row.finished_at,
                        )
                    )
                    logger.info(f"[submissions] submission {sub_id} is the new {row.slot} champion")
            else:
                try:
                    registration = self._registrations.claim_slot(session, row.hotkey, sub_id)
                    logger.info(
                        f"[submissions] submission {sub_id} rejected: spent registration "
                        f"{registration}"
                    )
                except NoSlot:
                    logger.warning(
                        f"[submissions] submission {sub_id} rejected, but its hotkey has no "
                        "registration left to charge"
                    )
            row.state = state.value
            return state.value

    def gate_crashed(
        self,
        sub_id: int,
        *,
        max_errors: int,
        expected_attempt: str | None = None,
        **fields: object,
    ) -> str:
        # The gate crashed or gave no verdict: requeue the submission, or at `max_errors` end it in
        # `error` with whatever the gate recorded, uncharged either way, and return the new state.
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

    def kernel(self, sub_id: int) -> bytes | None:
        # The submission's Kernel.lean from the store; None for one queued before it kept files.
        with session_scope(self._sessions) as session:
            content = session.execute(
                select(models.SubmissionFile.content).where(
                    models.SubmissionFile.submission_id == sub_id,
                    models.SubmissionFile.name == KERNEL,
                )
            ).scalar_one_or_none()
            return bytes(content) if content is not None else None

    def requeue_interrupted(self, worker_id: str) -> int:
        # At startup, requeue this worker's own claims, which died with its last process, rather
        # than waiting out the stale-claim timeout (uncharged, like any validator error).
        with session_scope(self._sessions) as session:
            result = session.execute(
                update(models.Submission)
                .where(
                    models.Submission.state == SubmissionState.VERIFYING.value,
                    models.Submission.worker_id == worker_id,
                )
                .values(
                    verification_attempt=None,
                    state=SubmissionState.QUEUED.value,
                    worker_id=None,
                    claimed_at=None,
                )
            )
            return int(cast("CursorResult[Any]", result).rowcount or 0)

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
        return self._requeue(self._from_other_verifier(fingerprint))

    def stale(self, fingerprint: str) -> list[models.Submission]:
        # Accepted submissions whose verdict came from another verifier, oldest first.
        return self._accepted_where(self._from_other_verifier(fingerprint))

    def _from_other_verifier(self, fingerprint: str) -> list[Any]:
        # Accepted verdicts whose recorded verifier is not `fingerprint`.
        return [
            models.Submission.state == SubmissionState.ACCEPTED.value,
            models.Submission.verifier_fingerprint.is_distinct_from(fingerprint),
        ]

    def _requeue(self, where: list[Any]) -> list[int]:
        # Queue the matching submissions again, keeping each one's spent registration.
        with session_scope(self._sessions) as session:
            ids = session.execute(
                update(models.Submission)
                .where(*where)
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

    def _accepted_where(self, where: list[Any]) -> list[models.Submission]:
        # The matching submissions, oldest first, detached so they stay readable.
        with session_scope(self._sessions) as session:
            rows = (
                session.execute(
                    select(models.Submission)
                    .where(*where)
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
        # Latest recorded incumbent, for display only; it does not restrict scoring.
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

    def current_champion(self, slot: str = "add") -> str | None:
        # The slot's champion source: the latest promotion's, else the shipped one verdicts record.
        with session_scope(self._sessions) as session:
            return _current_champion(session, slot)

    def champion(self, slot: str = "add", before: int | None = None) -> models.Submission | None:
        # The submission that is the slot's champion, or None while the shipped one holds; with
        # `before`, the champion that submission took over from, if it was itself promoted.
        with session_scope(self._sessions) as session:
            query = (
                select(models.Submission)
                .join(models.Promotion, models.Promotion.submission_id == models.Submission.id)
                .where(models.Promotion.slot == slot, models.Promotion.demoted_at.is_(None))
            )
            if before is not None:
                own = select(models.Promotion.id).where(models.Promotion.submission_id == before)
                query = query.where(
                    models.Promotion.id < func.coalesce(own.scalar_subquery(), 2**62)
                )
            row = session.execute(
                query.order_by(models.Promotion.id.desc()).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def champions(self, slot: str = "add") -> list[tuple[models.Submission, models.Promotion]]:
        # Every submission ever promoted in the slot with its promotion, newest first: the first
        # one not demoted is the champion, every other one held the slot before it.
        with session_scope(self._sessions) as session:
            rows = session.execute(
                select(models.Submission, models.Promotion)
                .join(models.Promotion, models.Promotion.submission_id == models.Submission.id)
                .where(models.Promotion.slot == slot)
                .order_by(models.Promotion.id.desc())
            ).all()
            for row, promotion in rows:
                session.expunge(row)
                session.expunge(promotion)
            return [(row, promotion) for row, promotion in rows]

    def champion_failed(self, slot: str, champion_sha256: str, max_failures: int) -> str | None:
        # Count a gate run in which the slot's standing champion's own bench failed, demoting it at
        # `max_failures`; None when the failed bench was not that champion's, so nothing is counted.
        with session_scope(self._sessions) as session:
            promotion = session.execute(
                select(models.Promotion)
                .where(models.Promotion.slot == slot, models.Promotion.demoted_at.is_(None))
                .order_by(models.Promotion.id.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if promotion is None or promotion.champion_sha256 != champion_sha256:
                return None
            promotion.failures += 1
            who = f"{slot} champion, submission {promotion.submission_id}"
            if promotion.failures < max_failures:
                logger.warning(
                    f"[submissions] the {who} failed its bench, {promotion.failures} time(s)"
                )
                return "counted"
            promotion.demoted_at = clock.now()
            logger.warning(
                f"[submissions] the {who} failed its bench {promotion.failures} times: demoted"
            )
            return "demoted"

    def requeue(self, sub_id: int, *, expected_attempt: str | None = None) -> None:
        # Back in the queue uncounted and uncharged: the champion's bench failed, not this entry.
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

    def champion_kernel(self, slot: str = "add", before: int | None = None) -> bytes | None:
        # The Kernel.lean the gate times an entry against: the champion, or for a re-verified
        # champion the one before it, so no submission is ever timed against itself.
        champion = self.champion(slot, before)
        return self.kernel(champion.id) if champion is not None else None

    def _comparable(self, slot: str) -> list[Any]:
        # Ratios against the current champion only, so a promotion starts a fresh board; the
        # fingerprint and incumbent binary stay provenance, so a hotfix keeps every result.
        where = [
            models.Submission.slot == slot,
            models.Submission.state == SubmissionState.ACCEPTED.value,
            models.Submission.ratio.is_not(None),
        ]
        if (champion := self.current_champion(slot)) is not None:
            where.append(models.Submission.champion_sha256 == champion)
        return where

    def scorable(self, slot: str = "add") -> list[models.Submission]:
        # Every comparable accepted submission, in submission order: admission's input.
        where = self._comparable(slot)
        with session_scope(self._sessions) as session:
            rows = (
                session.execute(
                    select(models.Submission)
                    .where(*where)
                    .order_by(models.Submission.submitted_at, models.Submission.id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def evidence_key(self, slot: str = "add") -> tuple[tuple[int, float, str], ...]:
        # What admission's decisions depend on, read without the rounds: each comparable accepted
        # submission's id, ratio and verdict time (a re-verification moves the time).
        where = self._comparable(slot)
        with session_scope(self._sessions) as session:
            rows = session.execute(
                select(
                    models.Submission.id,
                    models.Submission.ratio,
                    models.Submission.finished_at,
                )
                .where(*where)
                .order_by(models.Submission.id)
            ).all()
            return tuple((int(i), float(r or 0.0), str(f)) for i, r, f in rows)

    def leaderboard(self, slot: str = "add") -> list[models.Submission]:
        # Each hotkey's best comparable accepted ratio, fastest first, ties earlier.
        where = self._comparable(slot)
        with session_scope(self._sessions) as session:
            rows = (
                session.execute(
                    select(models.Submission)
                    .distinct(models.Submission.hotkey)
                    .where(*where)
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
        # The champion leads its board, standing in for its hotkey's line.
        champion = self.champion(slot)
        if champion is None:
            return ordered
        return [champion, *(r for r in ordered if r.hotkey != champion.hotkey)]
