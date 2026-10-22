"""Exact-runtime persistence: short transactions, immutable plans and fenced job attempts."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid
from pathlib import Path

from sqlalchemy import and_, exists, func, or_, select, update

from exact.config import Config, digest, measurement_hash
from exact.scoring import assess, validate_result

from . import models as m
from .engine import session_scope


def db_now(session):
    return session.scalar(select(func.clock_timestamp()))


def lock_board(session, board):
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("exact:" + board, 0))))


def source_blocked(source):
    return exists().where(
        m.ExactDisqualification.source_hash == source, m.ExactDisqualification.active.is_(True)
    )


def lock_source(session, source):
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended("source:" + source, 0)))
    )


def champion(session, board):
    return session.scalar(
        select(m.ExactPromotion)
        .where(
            m.ExactPromotion.board == board,
            m.ExactPromotion.disqualified_at.is_(None),
            ~source_blocked(m.ExactPromotion.source_hash),
        )
        .order_by(m.ExactPromotion.id.desc())
        .limit(1)
    )


def cancel(session, evaluation):
    evaluation.state = "superseded"
    session.execute(
        update(m.ExactJob)
        .where(
            m.ExactJob.evaluation_id == evaluation.id,
            m.ExactJob.state.in_(("queued", "running", "failed")),
        )
        .values(state="cancelled", token=None, lease_until=None)
    )


class ExactDb:
    def __init__(self, sessions):
        self.sessions = sessions
        self.leader_token = None

    def set_leader(self, token):
        with session_scope(self.sessions) as s:
            row = s.get(m.ObservedState, "exact:leader", with_for_update=True)
            if row is None:
                s.add(m.ObservedState(key="exact:leader", details={"token": token}))
            else:
                row.details = {"token": token}
        self.leader_token = token

    def _lead(self, session):
        if self.leader_token is not None:
            row = session.get(m.ObservedState, "exact:leader", with_for_update=True)
            if row is None or row.details["token"] != self.leader_token:
                raise RuntimeError("coordinator leadership lost")

    def claim_proof(self, owner: str, identity: str, lease: float):
        with session_scope(self.sessions) as s:
            self._lead(s)
            now = db_now(s)
            row = s.scalar(
                select(m.Submission)
                .where(
                    m.Submission.slot == "exact",
                    ~source_blocked(m.Submission.digest),
                    or_(
                        and_(
                            m.Submission.state == "queued",
                            or_(m.Submission.claimed_at.is_(None), m.Submission.claimed_at <= now),
                        ),
                        and_(m.Submission.state == "verifying", m.Submission.claimed_at < now),
                        and_(
                            m.Submission.state == "accepted",
                            m.Submission.exact_verification["identity"].astext.is_distinct_from(
                                identity
                            ),
                        ),
                    ),
                )
                .order_by(m.Submission.submitted_at, m.Submission.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.state, row.worker_id, row.verification_attempt = (
                "verifying",
                owner,
                uuid.uuid4().hex,
            )
            row.claimed_at = now + dt.timedelta(seconds=lease)
            s.flush()
            return row

    def heartbeat_proof(self, sid, token, lease):
        with session_scope(self.sessions) as s:
            self._lead(s)
            now = db_now(s)
            return bool(
                s.execute(
                    update(m.Submission)
                    .where(
                        m.Submission.id == sid,
                        m.Submission.state == "verifying",
                        m.Submission.verification_attempt == token,
                        m.Submission.claimed_at > now,
                    )
                    .values(claimed_at=now + dt.timedelta(seconds=lease))
                ).rowcount
            )

    def finish_proof(self, sid, token, identity, verdict, retry_seconds):
        with session_scope(self.sessions) as s:
            self._lead(s)
            row = s.get(m.Submission, sid, with_for_update=True)
            now = db_now(s)
            if (
                row is None
                or row.state != "verifying"
                or row.verification_attempt != token
                or row.claimed_at <= now
            ):
                return False
            code = verdict["exit_code"]
            row.state = "accepted" if code == 0 else "rejected" if code == 1 else "queued"
            row.exact_verification = {
                "identity": identity,
                "verdict": verdict,
                "accepted": code == 0,
            }
            row.report, row.stage, row.reason = (
                json.dumps(verdict),
                verdict.get("stage"),
                verdict.get("reason"),
            )
            row.verification_attempt = row.worker_id = None
            row.claimed_at = (
                now + dt.timedelta(seconds=retry_seconds) if code not in (0, 1) else None
            )
            row.finished_at = now if code in (0, 1) else None
            return True

    def reconcile(self, config: Config):
        """Single atomic transition: settings evidence, pending work and reward publication."""
        with session_scope(self.sessions) as s:
            self._lead(s)
            for e in s.scalars(
                select(m.ExactEvaluation)
                .where(m.ExactEvaluation.state == "pending")
                .with_for_update()
            ):
                if e.board not in config.boards or e.config_hash != config.identity(e.board):
                    cancel(s, e)
            details = config.public()
            state = s.get(m.ObservedState, "exact:config")
            old_boards = set(state.details["boards"]) if state else set()
            for board in config.boards:
                start = s.get(m.ObservedState, f"exact:board-start:{board}")
                if start is None:
                    # New boards start with currently pending uploads and future uploads, rather
                    # than replaying every historical, already-decided submission.
                    cutoff = (
                        s.scalar(select(func.max(m.ExactEvaluation.id)))
                        if state and board not in old_boards
                        else 0
                    )
                    s.add(
                        m.ObservedState(
                            key=f"exact:board-start:{board}",
                            details={"historical_evaluation_id": cutoff or 0},
                        )
                    )
            if state is None:
                s.add(m.ObservedState(key="exact:config", details=details))
            else:
                state.details = details
            s.flush()
            self._publish(s, config)

    def plan(self, config: Config):
        created = []
        with session_scope(self.sessions) as s:
            self._lead(s)
            for board in config.boards:
                lock_board(s, board)
                # One head challenger per board. Slow/paused boards never block other boards.
                pending = s.scalar(
                    select(m.ExactEvaluation)
                    .where(m.ExactEvaluation.board == board, m.ExactEvaluation.state == "pending")
                    .limit(1)
                )
                if pending:
                    current = champion(s, board)
                    blocked = s.scalar(
                        select(source_blocked(s.get(m.Submission, pending.submission_id).digest))
                    )
                    if not blocked and pending.reference_promotion_id == (
                        current.id if current else None
                    ):
                        continue
                    cancel(s, pending)
                    s.flush()
                done = exists(
                    select(m.ExactEvaluation.id).where(
                        m.ExactEvaluation.submission_id == m.Submission.id,
                        m.ExactEvaluation.board == board,
                        m.ExactEvaluation.state == "completed",
                    )
                )
                sub = s.scalar(
                    select(m.Submission)
                    .where(
                        m.Submission.slot == "exact",
                        m.Submission.state == "accepted",
                        ~source_blocked(m.Submission.digest),
                        m.Submission.exact_verification["identity"].astext == config.proof_identity,
                        ~exists(
                            select(m.ExactEvaluation.id).where(
                                m.ExactEvaluation.submission_id == m.Submission.id,
                                m.ExactEvaluation.state == "completed",
                                m.ExactEvaluation.id
                                <= (
                                    s.get(m.ObservedState, f"exact:board-start:{board}").details[
                                        "historical_evaluation_id"
                                    ]
                                    if s.get(m.ObservedState, f"exact:board-start:{board}")
                                    else 0
                                ),
                            )
                        ),
                        ~done,
                    )
                    .order_by(m.Submission.submitted_at, m.Submission.id)
                    .limit(1)
                )
                # Do not overtake an older unverified upload on any board.
                older = (
                    s.scalar(
                        select(m.Submission.id)
                        .where(
                            m.Submission.slot == "exact",
                            m.Submission.state.in_(("queued", "verifying")),
                            ~source_blocked(m.Submission.digest),
                            or_(
                                m.Submission.submitted_at < sub.submitted_at,
                                and_(
                                    m.Submission.submitted_at == sub.submitted_at,
                                    m.Submission.id < sub.id,
                                ),
                            ),
                        )
                        .limit(1)
                    )
                    if sub
                    else None
                )
                if sub is None or older is not None:
                    continue
                ref = champion(s, board)
                if ref:
                    source = s.scalar(
                        select(m.SubmissionFile.content).where(
                            m.SubmissionFile.submission_id == ref.submission_id,
                            m.SubmissionFile.name == "Kernel.lean",
                        )
                    )
                    refhash = ref.source_hash
                else:
                    source = Path(config.boards[board]["reference"]["path"]).read_bytes()
                    refhash = config.boards[board]["reference"]["sha256"]
                if source is None or hashlib.sha256(source).hexdigest() != refhash:
                    raise ValueError("reference source missing or hash mismatch")
                generation = (
                    s.scalar(
                        select(func.max(m.ExactEvaluation.generation)).where(
                            m.ExactEvaluation.submission_id == sub.id,
                            m.ExactEvaluation.board == board,
                        )
                    )
                    or 0
                ) + 1
                plan = config.plan(board)
                if config.seed_mode == "fresh":
                    previous = s.scalar(
                        select(m.ExactEvaluation)
                        .where(
                            m.ExactEvaluation.submission_id == sub.id,
                            m.ExactEvaluation.board == board,
                        )
                        .order_by(m.ExactEvaluation.generation.desc())
                        .limit(1)
                    )
                    values = previous.plan.get("seed_values", []) if previous else []
                    if len(values) != len(config.seeds):
                        values = []
                        while len(values) < len(config.seeds):
                            value = secrets.randbits(32)
                            if value not in values:
                                values.append(value)
                    plan["seed_values"] = values
                    replace_seeds = dict(zip(config.seeds, values, strict=True))
                    for stratum in plan["strata"]:
                        stratum["seed"] = replace_seeds[stratum["seed"]]
                e = m.ExactEvaluation(
                    submission_id=sub.id,
                    board=board,
                    generation=generation,
                    planning_key=digest([sub.id, board, generation]),
                    config_hash=config.identity(board),
                    reference_promotion_id=ref.id if ref else None,
                    reference_hash=refhash,
                    reference_source=source,
                    plan=plan,
                )
                s.add(e)
                s.flush()
                for stratum in plan["strata"]:
                    request = {k: v for k, v in stratum.items() if k != "weight"} | {
                        "candidate_hash": sub.digest,
                        "reference_hash": refhash,
                        "cells": plan["cells"],
                        "timing": plan["timing"],
                        "proof_identity": plan["proof_identity"],
                        "benchmark_identity": plan["benchmark_identity"],
                        "retry_seconds": config.retry_seconds,
                        "lease_seconds": config.lease_seconds,
                    }
                    # Reuse completed evidence only for an identical measurement request.
                    old = s.scalar(
                        select(m.ExactJob)
                        .where(
                            m.ExactJob.request_hash == measurement_hash(request),
                            m.ExactJob.state == "completed",
                            m.ExactJob.result.is_not(None),
                        )
                        .order_by(m.ExactJob.id.desc())
                        .limit(1)
                    )
                    # Link to old evidence instead of copying raw JSON when reused.
                    if old:
                        request = request | {"reuse_job_id": old.id}
                    s.add(
                        m.ExactJob(
                            evaluation_id=e.id,
                            request_hash=measurement_hash(request),
                            request=request,
                            environment=stratum["environment"],
                            state="completed" if old else "queued",
                        )
                    )
                created.append(e.id)
        return created

    def claim(self, environment, identity, owner, lease):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            # Expired attempts are delayed, so a rapidly failing reference cannot monopolize claims.
            s.execute(
                update(m.ExactJob)
                .where(
                    m.ExactJob.environment == environment,
                    m.ExactJob.state == "running",
                    m.ExactJob.lease_until <= now,
                )
                .values(
                    state="queued",
                    token=None,
                    lease_until=None,
                    due_at=now + dt.timedelta(seconds=lease),
                    error={"kind": "infrastructure", "reason": "lease expired"},
                )
            )
            row = s.scalar(
                select(m.ExactJob)
                .join(m.ExactEvaluation)
                .where(
                    m.ExactJob.environment == environment,
                    m.ExactJob.request["identity"].astext == identity,
                    m.ExactJob.state == "queued",
                    m.ExactJob.due_at <= now,
                    m.ExactEvaluation.state == "pending",
                )
                .order_by(m.ExactJob.due_at, m.ExactJob.id)
                .limit(1)
                .with_for_update(skip_locked=True, of=m.ExactJob)
            )
            if row is None:
                return None
            row.state, row.owner, row.token = "running", owner, uuid.uuid4().hex
            row.lease_until = now + dt.timedelta(seconds=row.request.get("lease_seconds", lease))
            row.started_at = now
            row.attempts += 1
            s.flush()
            return row

    def heartbeat(self, jid, owner, token, lease):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            return bool(
                s.execute(
                    update(m.ExactJob)
                    .where(*self._owned(jid, owner, token, now))
                    .values(lease_until=now + dt.timedelta(seconds=lease))
                ).rowcount
            )

    @staticmethod
    def _owned(jid, owner, token, now):
        return (
            m.ExactJob.id == jid,
            m.ExactJob.state == "running",
            m.ExactJob.owner == owner,
            m.ExactJob.token == token,
            m.ExactJob.lease_until > now,
        )

    def complete(self, jid, owner, token, result):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            row = s.scalar(
                select(m.ExactJob).where(*self._owned(jid, owner, token, now)).with_for_update()
            )
            if row is None:
                return False
            validate_result(row.request, result)
            row.state, row.result, row.completed_at = "completed", result, now
            row.token = row.lease_until = None
            row.error = None
            return True

    def fail(self, jid, owner, token, error, retry_seconds):
        if error.get("kind") not in ("candidate", "reference", "infrastructure", "checksum"):
            raise ValueError("unknown failure classification")
        with session_scope(self.sessions) as s:
            now = db_now(s)
            row = s.scalar(
                select(m.ExactJob).where(*self._owned(jid, owner, token, now)).with_for_update()
            )
            if row is None:
                return False
            row.state = "failed" if error["kind"] == "candidate" else "queued"
            row.due_at = now + dt.timedelta(
                seconds=min(86400, retry_seconds * 2 ** min(row.attempts - 1, 4))
            )
            row.token = row.lease_until = None
            row.error = {"kind": error["kind"], "reason": str(error.get("reason", ""))[:2000]}
            return True

    def sources(self, jid):
        with session_scope(self.sessions) as s:
            e = s.scalar(select(m.ExactEvaluation).join(m.ExactJob).where(m.ExactJob.id == jid))
            source = s.scalar(
                select(m.SubmissionFile.content).where(
                    m.SubmissionFile.submission_id == e.submission_id,
                    m.SubmissionFile.name == "Kernel.lean",
                )
            )
            if source is None:
                raise ValueError("candidate source missing")
            return source, e.reference_source

    def ready(self):
        with session_scope(self.sessions) as s:
            out = []
            for e in s.scalars(
                select(m.ExactEvaluation)
                .where(m.ExactEvaluation.state == "pending")
                .order_by(m.ExactEvaluation.id)
            ):
                jobs = list(
                    s.scalars(
                        select(m.ExactJob)
                        .where(m.ExactJob.evaluation_id == e.id)
                        .order_by(m.ExactJob.id)
                    )
                )
                pairs = []
                for j in jobs:
                    if "reuse_job_id" in j.request:
                        reused = s.get(m.ExactJob, j.request["reuse_job_id"])
                        req = {k: v for k, v in j.request.items() if k != "reuse_job_id"}
                        if (
                            reused
                            and reused.state == "completed"
                            and reused.request_hash == measurement_hash(req)
                        ):
                            pairs.append((req, reused.result))
                    elif j.state == "completed":
                        pairs.append((j.request, j.result))
                if jobs and len(pairs) == len(jobs):
                    out.append((e, pairs))
            return out

    def finalize(self, eid, config, decision):
        with session_scope(self.sessions) as s:
            self._lead(s)
            e = s.get(m.ExactEvaluation, eid)
            if e is None:
                return False
            lock_board(s, e.board)
            s.refresh(e, with_for_update=True)
            if e.state != "pending":
                return False
            current = champion(s, e.board)
            sub = s.get(m.Submission, e.submission_id)
            lock_source(s, sub.digest)
            if (
                e.board not in config.boards
                or e.config_hash != config.identity(e.board)
                or e.reference_promotion_id != (current.id if current else None)
                or sub.state != "accepted"
                or s.scalar(select(source_blocked(sub.digest)))
                or not sub.exact_verification
                or sub.exact_verification.get("identity") != config.proof_identity
            ):
                cancel(s, e)
                return False
            e.state, e.decision, e.completed_at = "completed", decision, db_now(s)
            s.execute(
                update(m.ExactJob)
                .where(
                    m.ExactJob.evaluation_id == e.id, m.ExactJob.state.in_(("queued", "running"))
                )
                .values(state="cancelled", token=None, lease_until=None)
            )
            if decision["outcome"] == "qualifies":
                s.add(
                    m.ExactPromotion(
                        board=e.board,
                        submission_id=e.submission_id,
                        evaluation_id=e.id,
                        source_hash=sub.digest,
                        source_environment=sub.exact_verification.get("verdict", {}).get(
                            "source_environment", {}
                        ),
                    )
                )
            s.flush()
            self._publish(s, config)
            return True

    def aggregate(self, config):
        count = 0
        with session_scope(self.sessions) as s:
            failed = list(
                s.execute(
                    select(m.ExactEvaluation.id, m.ExactJob.error)
                    .join(m.ExactJob)
                    .where(
                        m.ExactEvaluation.state == "pending",
                        m.ExactJob.state == "failed",
                        m.ExactJob.error["kind"].astext == "candidate",
                    )
                )
            )
        for eid, error in failed:
            count += self.finalize(
                eid,
                config,
                {"outcome": "benchmark-failed", "reason": error["reason"], "score": None},
            )
        # No database session is held during bootstrap.
        for e, pairs in self.ready():
            try:
                decision = assess(e.plan, pairs)
            except (ValueError, KeyError, TypeError) as exc:
                # A malformed worker observation is infrastructure evidence, never a takeover
                # or a miner rejection. Retain its raw payload for diagnosis and retry a new plan.
                with session_scope(self.sessions) as s:
                    self._lead(s)
                    row = s.get(m.ExactEvaluation, e.id, with_for_update=True)
                    if row.state == "pending":
                        cancel(s, row)
                        for job in s.scalars(
                            select(m.ExactJob).where(
                                m.ExactJob.evaluation_id == e.id,
                                m.ExactJob.state == "completed",
                            )
                        ):
                            target = (
                                s.get(m.ExactJob, job.request["reuse_job_id"])
                                if "reuse_job_id" in job.request
                                else job
                            )
                            target.state = "failed"
                            target.error = {"kind": "infrastructure", "reason": str(exc)[:2000]}
                continue
            count += self.finalize(e.id, config, decision)
        return count

    def publish(self, config):
        with session_scope(self.sessions) as s:
            self._lead(s)
            self._publish(s, config)

    def _publish(self, s, config):
        boards = {}
        for board in config.boards:
            query = (
                select(m.ExactPromotion, m.Submission.hotkey)
                .join(m.Submission)
                .where(
                    m.ExactPromotion.board == board,
                    m.ExactPromotion.disqualified_at.is_(None),
                    ~source_blocked(m.ExactPromotion.source_hash),
                )
                .order_by(m.ExactPromotion.id.desc())
                .limit(2)
            )
            rows = s.execute(query).all()
            paused = bool(
                s.scalar(
                    select(m.ExactJob.id)
                    .join(m.ExactEvaluation)
                    .where(
                        m.ExactEvaluation.board == board,
                        m.ExactEvaluation.state == "pending",
                        m.ExactJob.state.in_(("queued", "running")),
                        m.ExactJob.error["kind"].astext.in_(("reference", "checksum")),
                    )
                    .limit(1)
                )
            )
            boards[board] = {
                "paused": paused,
                "kings": [
                    {
                        "promotion_id": p.id,
                        "submission_id": p.submission_id,
                        "hotkey": hotkey,
                    }
                    for p, hotkey in rows
                ],
            }
        publication = {"version": "exact-top2-v1", "rewards": config.rewards, "boards": boards}
        row = s.get(m.ObservedState, "exact:publication")
        if row is None:
            s.add(m.ObservedState(key="exact:publication", details=publication))
        elif row.details != publication:
            row.details = publication

    def publication(self):
        with session_scope(self.sessions) as s:
            row = s.get(m.ObservedState, "exact:publication")
            if row is None:
                raise RuntimeError("exact coordinator has not published reward policy")
            return row.details

    def set_disqualification(self, source_hash, active, reason, actor, config):
        """An explicit operator action, serialized with promotion and published atomically."""
        from exact.config import sha

        sha(source_hash)
        if not reason.strip() or not actor.strip():
            raise ValueError("operator and reason are required")
        with session_scope(self.sessions) as s:
            # Coordinate publication/config changes with the active leader, without adopting
            # its fencing token or requiring the audit service to own leadership.
            s.get(m.ObservedState, "exact:leader", with_for_update=True)
            observed = s.get(m.ObservedState, "exact:config")
            if observed is None or digest(observed.details) != digest(config.public()):
                raise ValueError("operator configuration must match the active coordinator")
            lock_source(s, source_hash)
            row = s.get(m.ExactDisqualification, source_hash)
            if row is None:
                row = m.ExactDisqualification(source_hash=source_hash)
                s.add(row)
            row.active, row.reason, row.actor, row.updated_at = active, reason, actor, db_now(s)
            row.history = [
                *(row.history or []),
                dict(active=active, reason=reason, actor=actor, at=row.updated_at.isoformat()),
            ]
            s.flush()
            self._publish(s, config)

    def alerts(self, age):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            rows = s.scalars(
                select(m.ExactJob)
                .join(m.ExactEvaluation)
                .where(
                    m.ExactEvaluation.state == "pending",
                    or_(
                        and_(
                            m.ExactJob.state == "queued",
                            m.ExactJob.created_at < now - dt.timedelta(seconds=age),
                        ),
                        and_(m.ExactJob.state == "running", m.ExactJob.lease_until < now),
                        m.ExactJob.attempts >= 3,
                    ),
                )
            )
            return [
                {
                    "job": j.id,
                    "environment": j.environment,
                    "state": j.state,
                    "attempts": j.attempts,
                    "error": j.error,
                }
                for j in rows
            ]
