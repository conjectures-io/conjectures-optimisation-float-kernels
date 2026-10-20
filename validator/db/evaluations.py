"""Coordinator planning, immutable evidence links and atomic promotion/publication."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from pathlib import Path

from config import Config, digest, measurement_hash
from scoring import assess
from sqlalchemy import exists, func, or_, select

from . import models as m
from .champions import champion, history
from .engine import session_scope
from .submissions import SubmissionsDb, db_now, lead, put_source


def lock_board(session, board):
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("tensor:" + board, 0))))


def job_query(eid):
    return select(m.BenchmarkJob).join(m.EvaluationJob).where(m.EvaluationJob.evaluation_id == eid)


def cancel(session, evaluation):
    evaluation.state = "superseded"
    # Only completed evidence is shared, but keep the active-consumer check explicit.
    session.flush()
    jobs = list(session.scalars(job_query(evaluation.id)))
    for job in jobs:
        consumers = session.scalar(
            select(m.Evaluation.id)
            .join(m.EvaluationJob)
            .where(m.EvaluationJob.job_id == job.id, m.Evaluation.state == "pending")
            .limit(1)
        )
        if consumers is None and job.state in ("queued", "running", "failed"):
            job.state = "cancelled"
            job.owner = job.token = job.lease_until = None


class EvaluationsDb:
    def __init__(self, sessions):
        self.sessions = sessions
        self.leader_token = None
        self.submissions = SubmissionsDb(sessions)

    def set_leader(self, token):
        with session_scope(self.sessions) as s:
            row = s.get(m.CoordinatorState, 1, with_for_update=True)
            if row is None:
                s.add(m.CoordinatorState(id=1, leader_token=token))
            else:
                row.leader_token = token
        self.leader_token = self.submissions.leader_token = token

    def _lead(self, session):
        return lead(session, self.leader_token)

    def reconcile(self, config: Config):
        with session_scope(self.sessions) as s:
            state = self._lead(s)
            # Same lock as intake: no partially received upload can cross the boundary.
            s.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("tensor:intake", 0))))
            old_boards = set(state.config_evidence["boards"]) if state.config_evidence else set()
            boundary = s.scalar(select(func.max(m.Submission.id))) or 0
            for board in config.boards:
                progress = s.get(m.BoardProgress, board)
                if progress is None:
                    s.add(m.BoardProgress(board=board, activation_boundary=boundary))
                elif board not in old_boards:
                    progress.activation_boundary = boundary
                source = Path(config.boards[board]["reference"]["path"]).read_bytes()
                if (
                    hashlib.sha256(source).hexdigest()
                    != config.boards[board]["reference"]["sha256"]
                ):
                    raise ValueError("initial reference hash mismatch")
                put_source(s, source)
            for e in s.scalars(
                select(m.Evaluation).where(m.Evaluation.state == "pending").with_for_update()
            ):
                if e.board not in config.boards or e.config_hash != config.identity(e.board):
                    cancel(s, e)
            state.config_hash, state.config_evidence = digest(config.public()), config.public()
            s.flush()
            self._publish(s, config)

    def plan(self, config: Config):
        created = []
        with session_scope(self.sessions) as s:
            self._lead(s)
            for board in config.boards:
                lock_board(s, board)
                progress = s.get(m.BoardProgress, board)
                if progress is None:
                    raise RuntimeError("reconcile configuration before planning")
                pending = s.scalar(
                    select(m.Evaluation).where(
                        m.Evaluation.board == board, m.Evaluation.state == "pending"
                    )
                )
                ref = champion(s, board)
                if pending:
                    if pending.reference_promotion_id == (ref.id if ref else None):
                        continue
                    cancel(s, pending)
                    s.flush()
                done = exists(
                    select(m.Evaluation.id).where(
                        m.Evaluation.submission_id == m.Submission.id,
                        m.Evaluation.board == board,
                        m.Evaluation.state == "completed",
                    )
                )
                # Rejected uploads do not block; queued/running proofs retain intake FIFO.
                sub = s.scalar(
                    select(m.Submission)
                    .where(
                        m.Submission.id > progress.activation_boundary,
                        m.Submission.state != "rejected",
                        ~done,
                    )
                    .order_by(m.Submission.submitted_at, m.Submission.id)
                    .limit(1)
                )
                if sub is None or sub.state != "proved":
                    continue
                # Existing paid state is preserved if proof policy changes; stale proofs are
                # explicitly audited/requeued by an operator, never silently reverified here.
                if sub.verification_identity != config.proof_identity:
                    continue
                refhash = s.get(m.Evaluation, ref.evaluation_id).submission_id if ref else None
                refhash = (
                    s.get(m.Submission, refhash).source_sha256
                    if ref
                    else config.boards[board]["reference"]["sha256"]
                )
                previous = s.scalar(
                    select(m.Evaluation)
                    .where(m.Evaluation.submission_id == sub.id, m.Evaluation.board == board)
                    .order_by(m.Evaluation.generation.desc())
                    .limit(1)
                )
                generation = previous.generation + 1 if previous else 1
                plan = config.plan(board)
                if config.seed_mode == "fresh":
                    values = previous.plan.get("seed_values", []) if previous else []
                    if len(values) != config.seed_count:
                        values = []
                        while len(values) < config.seed_count:
                            value = secrets.randbits(32)
                            if value not in values:
                                values.append(value)
                    plan["seed_values"] = values
                    for st in plan["strata"]:
                        st["seed"] = values[st["seed"]]
                e = m.Evaluation(
                    submission_id=sub.id,
                    board=board,
                    generation=generation,
                    config_hash=config.identity(board),
                    reference_promotion_id=ref.id if ref else None,
                    reference_hash=refhash,
                    plan=plan,
                )
                s.add(e)
                s.flush()
                for st in plan["strata"]:
                    request = {k: st[k] for k in ("environment", "identity", "seed")} | {
                        "candidate_hash": sub.source_sha256,
                        "reference_hash": refhash,
                        "cells": sorted(plan["cells"]),
                        "timing": plan["timing"],
                        "benchmark_identity": plan["benchmark_identity"],
                    }
                    request_hash = measurement_hash(request)
                    old = s.scalar(
                        select(m.BenchmarkJob)
                        .where(
                            m.BenchmarkJob.request_hash == request_hash,
                            m.BenchmarkJob.state == "completed",
                            m.BenchmarkJob.result.is_not(None),
                        )
                        .order_by(m.BenchmarkJob.id.desc())
                        .limit(1)
                    )
                    if old is None:
                        old = m.BenchmarkJob(
                            request_hash=request_hash,
                            request=request,
                            candidate_hash=sub.source_sha256,
                            reference_hash=refhash,
                            environment=st["environment"],
                            lease_seconds=config.lease_seconds,
                            retry_seconds=config.retry_seconds,
                        )
                        s.add(old)
                        s.flush()
                    s.add(
                        m.EvaluationJob(
                            evaluation_id=e.id,
                            job_id=old.id,
                            environment=st["environment"],
                            seed=st["seed"],
                        )
                    )
                created.append(e.id)
        return created

    def ready(self):
        with session_scope(self.sessions) as s:
            out = []
            for e in s.scalars(
                select(m.Evaluation)
                .where(m.Evaluation.state == "pending")
                .order_by(m.Evaluation.id)
            ):
                jobs = list(s.scalars(job_query(e.id).order_by(m.BenchmarkJob.id)))
                if jobs and all(j.state == "completed" for j in jobs):
                    out.append((e, [(j.request, j.result) for j in jobs]))
            return out

    def finalize(self, eid, config, decision):
        with session_scope(self.sessions) as s:
            self._lead(s)
            e = s.get(m.Evaluation, eid)
            if e is None:
                return False
            lock_board(s, e.board)
            s.refresh(e, with_for_update=True)
            if e.state != "pending":
                return False
            current = champion(s, e.board)
            sub = s.get(m.Submission, e.submission_id)
            if (
                e.board not in config.boards
                or e.config_hash != config.identity(e.board)
                or e.reference_promotion_id != (current.id if current else None)
                or sub.state != "proved"
                or sub.verification_identity != config.proof_identity
            ):
                cancel(s, e)
                return False
            e.state, e.decision, e.completed_at = "completed", decision, db_now(s)
            for job in s.scalars(job_query(e.id)):
                if job.state in ("queued", "running"):
                    job.state = "cancelled"
                    job.owner = job.token = job.lease_until = None
            if decision["outcome"] == "qualifies":
                s.add(m.Promotion(board=e.board, evaluation_id=e.id))
            progress = s.get(m.BoardProgress, e.board)
            progress.last_submission_id, progress.last_submitted_at = sub.id, sub.submitted_at
            s.flush()
            self._publish(s, config)
            return True

    def aggregate(self, config):
        count = 0
        with session_scope(self.sessions) as s:
            failed = list(
                s.execute(
                    select(m.Evaluation.id, m.BenchmarkJob.error)
                    .join(m.EvaluationJob, m.EvaluationJob.evaluation_id == m.Evaluation.id)
                    .join(m.BenchmarkJob, m.BenchmarkJob.id == m.EvaluationJob.job_id)
                    .where(
                        m.Evaluation.state == "pending",
                        m.BenchmarkJob.state == "failed",
                        m.BenchmarkJob.error["kind"].astext == "candidate",
                    )
                )
            )
        for eid, error in failed:
            count += self.finalize(
                eid,
                config,
                {"outcome": "benchmark-failed", "reason": error["reason"], "score": None},
            )
        # Bootstrap holds no database connection.
        for e, pairs in self.ready():
            try:
                decision = assess(e.plan, pairs)
            except (ValueError, KeyError, TypeError) as exc:
                with session_scope(self.sessions) as s:
                    self._lead(s)
                    row = s.get(m.Evaluation, e.id, with_for_update=True)
                    if row.state == "pending":
                        cancel(s, row)
                        for job in s.scalars(job_query(e.id)):
                            # Retain malformed payloads for diagnosis, but never reuse them.
                            if job.state == "completed":
                                job.state = "failed"
                                job.error = {"kind": "infrastructure", "reason": str(exc)[:2000]}
                                for consumer in s.scalars(
                                    select(m.Evaluation)
                                    .join(m.EvaluationJob)
                                    .where(
                                        m.EvaluationJob.job_id == job.id,
                                        m.Evaluation.state == "pending",
                                    )
                                ):
                                    cancel(s, consumer)
                continue
            count += self.finalize(e.id, config, decision)
        return count

    def disqualify(self, promotion_id, reason, config):
        if not reason.strip():
            raise ValueError("disqualification requires a reason")
        with session_scope(self.sessions) as s:
            self._lead(s)
            row = s.get(m.Promotion, promotion_id, with_for_update=True)
            if row is None:
                raise ValueError("unknown promotion")
            lock_board(s, row.board)
            row.disqualified_at, row.disqualification_reason = db_now(s), reason
            for pending in s.scalars(
                select(m.Evaluation).where(
                    m.Evaluation.board == row.board, m.Evaluation.state == "pending"
                )
            ):
                cancel(s, pending)
            s.flush()
            self._publish(s, config)

    def publish(self, config):
        with session_scope(self.sessions) as s:
            self._lead(s)
            self._publish(s, config)

    def _publish(self, s, config):
        boards = {}
        for board in config.boards:
            paused = bool(
                s.scalar(
                    select(m.BenchmarkJob.id)
                    .join(m.EvaluationJob)
                    .join(m.Evaluation)
                    .where(
                        m.Evaluation.board == board,
                        m.Evaluation.state == "pending",
                        m.BenchmarkJob.state.in_(("queued", "running")),
                        m.BenchmarkJob.error["kind"].astext.in_(("reference", "checksum")),
                    )
                    .limit(1)
                )
            )
            boards[board] = {
                "paused": paused,
                "kings": [
                    {"promotion_id": p.id, "submission_id": sid, "hotkey": hotkey}
                    for p, sid, hotkey in history(s, board, limit=2)
                ],
            }
        publication = {"version": "tensor-top2-v1", "rewards": config.rewards, "boards": boards}
        row = s.get(m.CoordinatorState, 1)
        if row.publication != publication:
            row.publication, row.publication_hash = publication, digest(publication)

    def alerts(self, age):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            rows = s.scalars(
                select(m.BenchmarkJob).where(
                    m.BenchmarkJob.state.in_(("queued", "running")),
                    or_(
                        m.BenchmarkJob.created_at < now - dt.timedelta(seconds=age),
                        m.BenchmarkJob.lease_until < now,
                        m.BenchmarkJob.attempts >= 3,
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
