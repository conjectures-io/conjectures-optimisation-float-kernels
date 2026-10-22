"""Independent promotion discovery and fenced audit attempts; no competition writes."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from exact.config import digest

from . import models as m
from .engine import session_scope
from .exact import db_now


@dataclass(frozen=True)
class AuditTask:
    id: int
    source_hash: str
    seed: int
    cases: int
    content: bytes | None
    request: dict
    owner: str
    token: str
    attempts: int


class AuditDb:
    def __init__(self, sessions):
        self.sessions = sessions

    def discover(self, profile, batch=100):
        """Backfill every promotion, including superseded kings, once per coverage profile.

        Promotion commits are serialized by the coordinator. Advance the profile cursor in
        the same transaction as inserting tasks, so crashes cannot lose discovered events.
        """
        identity = digest(profile)
        with session_scope(self.sessions) as s:
            s.execute(
                insert(m.ExactAuditCursor)
                .values(profile_identity=identity)
                .on_conflict_do_nothing()
            )
            cursor = s.get(m.ExactAuditCursor, identity, with_for_update=True)
            events = list(
                s.execute(
                    select(m.ExactPromotion, m.ExactEvaluation.plan)
                    .join(m.ExactEvaluation, m.ExactPromotion.evaluation_id == m.ExactEvaluation.id)
                    .where(m.ExactPromotion.id > cursor.promotion_id)
                    .order_by(m.ExactPromotion.id)
                    .limit(batch)
                )
            )
            for promotion, plan in events:
                proof = plan["proof_identity"]
                request = {
                    "profile_identity": identity,
                    "profile": profile,
                    "proof_identity": proof,
                    "benchmark_identity": plan["benchmark_identity"],
                    "promotion_id": promotion.id,
                    "board": promotion.board,
                    "source_environment": promotion.source_environment,
                    "specification_identity": promotion.source_environment.get(
                        "specification_identity", ""
                    ),
                }
                s.execute(
                    insert(m.ExactAudit)
                    .values(
                        source_hash=promotion.source_hash,
                        submission_id=promotion.submission_id,
                        audit_identity=digest([identity, proof]),
                        seed=secrets.randbelow(2**32),
                        cases=profile["cases"],
                        request=request,
                    )
                    .on_conflict_do_nothing(index_elements=["source_hash", "audit_identity"])
                )
                cursor.promotion_id = promotion.id
            return len(events)

    def claim(self, profile, proof_identity, owner, lease, specification_identity=None):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            row = s.scalar(
                select(m.ExactAudit)
                .where(
                    m.ExactAudit.request["profile_identity"].astext == digest(profile),
                    or_(
                        and_(
                            m.ExactAudit.request["specification_identity"].astext
                            == specification_identity,
                            specification_identity is not None,
                        ),
                        and_(
                            or_(
                                m.ExactAudit.request["specification_identity"].astext.is_(None),
                                m.ExactAudit.request["specification_identity"].astext == "",
                            ),
                            m.ExactAudit.request["proof_identity"].astext == proof_identity,
                        ),
                    ),
                    or_(
                        and_(
                            m.ExactAudit.state.in_(("queued", "error")), m.ExactAudit.due_at <= now
                        ),
                        and_(m.ExactAudit.state == "running", m.ExactAudit.lease_until <= now),
                    ),
                )
                .order_by(m.ExactAudit.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            if row.token:
                s.execute(
                    update(m.ExactAuditAttempt)
                    .where(
                        m.ExactAuditAttempt.token == row.token,
                        m.ExactAuditAttempt.state == "running",
                    )
                    .values(state="expired", finished_at=now)
                )
            row.state, row.owner, row.token = "running", owner, uuid.uuid4().hex
            row.lease_until = now + dt.timedelta(seconds=lease)
            row.attempts += 1
            s.add(m.ExactAuditAttempt(token=row.token, audit_id=row.id, owner=owner))
            content = s.scalar(
                select(m.SubmissionFile.content).where(
                    m.SubmissionFile.submission_id == row.submission_id,
                    m.SubmissionFile.name == "Kernel.lean",
                )
            )
            return AuditTask(
                row.id,
                row.source_hash,
                row.seed,
                row.cases,
                content,
                row.request,
                owner,
                row.token,
                row.attempts,
            )

    @staticmethod
    def _owned(task, now):
        return and_(
            m.ExactAudit.id == task.id,
            m.ExactAudit.state == "running",
            m.ExactAudit.owner == task.owner,
            m.ExactAudit.token == task.token,
            m.ExactAudit.lease_until > now,
        )

    def heartbeat(self, task, lease):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            return bool(
                s.execute(
                    update(m.ExactAudit)
                    .where(self._owned(task, now))
                    .values(lease_until=now + dt.timedelta(seconds=lease))
                ).rowcount
            )

    def finish(self, task, state, report, retry):
        if state not in ("clean", "failed", "error"):
            raise ValueError("unknown audit outcome")
        with session_scope(self.sessions) as s:
            now = db_now(s)
            due = now + dt.timedelta(seconds=min(86400, retry * 2 ** min(task.attempts - 1, 4)))
            saved = bool(
                s.execute(
                    update(m.ExactAudit)
                    .where(self._owned(task, now))
                    .values(
                        state=state,
                        report=report,
                        finished_at=now,
                        due_at=due,
                        owner=None,
                        token=None,
                        lease_until=None,
                    )
                ).rowcount
            )
            if saved:
                s.execute(
                    update(m.ExactAuditAttempt)
                    .where(m.ExactAuditAttempt.token == task.token)
                    .values(state=state, report=report, finished_at=now)
                )
            return saved

    def alerts(self, profile, seconds):
        """Report old pending tasks, including those needing a different pinned checkout."""
        with session_scope(self.sessions) as s:
            cutoff = db_now(s) - dt.timedelta(seconds=seconds)
            return [
                dict(
                    id=r.id,
                    source=r.source_hash,
                    state=r.state,
                    proof_identity=r.request.get("proof_identity"),
                    attempts=r.attempts,
                )
                for r in s.scalars(
                    select(m.ExactAudit)
                    .where(
                        m.ExactAudit.request["profile_identity"].astext == digest(profile),
                        m.ExactAudit.state.in_(("queued", "running", "error")),
                        m.ExactAudit.created_at < cutoff,
                    )
                    .order_by(m.ExactAudit.id)
                    .limit(100)
                )
            ]
