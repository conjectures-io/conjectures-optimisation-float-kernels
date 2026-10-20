"""Atomic intake and the leased proof lifecycle. Performance belongs to evaluations."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .engine import session_scope
from .models import CoordinatorState, Registration, Source, Submission
from .registrations import NoSlot


def db_now(session):
    return session.scalar(select(func.clock_timestamp()))


def lead(session, token):
    row = session.get(CoordinatorState, 1, with_for_update=True)
    if token is None or row is None or row.leader_token != token:
        raise RuntimeError("coordinator leadership lost")
    return row


def put_source(session, content):
    if not 0 < len(content) <= 256000:
        raise ValueError("expected one UTF-8 source under 256 KB")
    content.decode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    session.execute(insert(Source).values(sha256=digest, content=content).on_conflict_do_nothing())
    if session.get(Source, digest).content != content:
        raise ValueError("source hash collision")
    return digest


class SubmissionsDb:
    def __init__(self, sessions):
        self.sessions = sessions
        self.leader_token = None

    def add(self, hotkey, content, *, cutoff=None, account_id=None):
        """Latest registration only; duplicate bytes are rejected even after re-registration."""
        with session_scope(self.sessions) as s:
            # Serialize intake with activation-boundary recording, before allocating the ID.
            s.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("tensor:intake", 0))))
            reg = s.scalar(
                select(Registration)
                .where(Registration.ss58_hot == hotkey)
                .order_by(Registration.block.desc(), Registration.id.desc())
                .limit(1)
                .with_for_update()
            )
            if reg is None or (cutoff is not None and reg.block_date <= cutoff):
                raise NoSlot("no eligible latest registration")
            if s.scalar(select(Submission.id).where(Submission.registration_id == reg.id)):
                raise NoSlot("latest registration already used")
            digest = put_source(s, content)
            if s.scalar(
                select(Submission.id).where(
                    Submission.hotkey == hotkey, Submission.source_sha256 == digest
                )
            ):
                raise ValueError("duplicate submission")
            row = Submission(
                hotkey=hotkey, registration_id=reg.id, source_sha256=digest, account_id=account_id
            )
            s.add(row)
            s.flush()
            return row.id

    def claim_proof(self, owner, identity, lease):
        with session_scope(self.sessions) as s:
            lead(s, self.leader_token)
            now = db_now(s)
            row = s.scalar(
                select(Submission)
                .where(
                    or_(
                        and_(Submission.state == "queued", Submission.next_attempt_at <= now),
                        and_(Submission.state == "running", Submission.lease_until <= now),
                    )
                )
                .order_by(Submission.submitted_at, Submission.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            cached = s.scalar(
                select(Submission)
                .where(
                    Submission.source_sha256 == row.source_sha256,
                    Submission.verification_identity == identity,
                    Submission.state == "proved",
                )
                .limit(1)
            )
            if cached:
                row.state = "proved"
                row.verification_identity, row.proof_report = identity, cached.proof_report
                row.stage, row.reason, row.finished_at = cached.stage, cached.reason, now
                row.owner = row.token = row.lease_until = None
                return None
            row.state, row.owner, row.token = "running", owner, uuid.uuid4().hex
            row.lease_until = now + dt.timedelta(seconds=lease)
            row.attempts += 1
            s.flush()
            return row

    def heartbeat_proof(self, sid, token, lease):
        with session_scope(self.sessions) as s:
            lead(s, self.leader_token)
            now = db_now(s)
            return bool(
                s.execute(
                    update(Submission)
                    .where(
                        Submission.id == sid,
                        Submission.state == "running",
                        Submission.token == token,
                        Submission.lease_until > now,
                    )
                    .values(lease_until=now + dt.timedelta(seconds=lease))
                ).rowcount
            )

    def finish_proof(self, sid, token, identity, verdict, retry_seconds):
        with session_scope(self.sessions) as s:
            lead(s, self.leader_token)
            row = s.get(Submission, sid, with_for_update=True)
            now = db_now(s)
            if (
                row is None
                or row.state != "running"
                or row.token != token
                or row.lease_until <= now
            ):
                return False
            code = verdict["exit_code"]
            row.state = "proved" if code == 0 else "rejected" if code == 1 else "queued"
            row.verification_identity, row.proof_report = identity, verdict
            row.stage, row.reason = verdict.get("stage"), verdict.get("reason")
            row.owner = row.token = row.lease_until = None
            row.next_attempt_at = now + dt.timedelta(seconds=retry_seconds)
            row.finished_at = now if code in (0, 1) else None
            return True
