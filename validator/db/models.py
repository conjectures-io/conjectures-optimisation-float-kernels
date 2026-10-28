"""Tensor competition schema; the initial migration freezes this layout."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def timestamp():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WorkerLogin(Base):
    __tablename__ = "worker_logins"
    login: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[dt.datetime] = timestamp()
    updated_at: Mapped[dt.datetime] = timestamp()
    __table_args__ = (CheckConstraint("kind IN ('benchmark','auditor')", name="ck_worker_kind"),)


class WorkerEnvironment(Base):
    __tablename__ = "worker_environments"
    login: Mapped[str] = mapped_column(ForeignKey("worker_logins.login"), primary_key=True)
    environment: Mapped[str] = mapped_column(Text, primary_key=True)


class Registration(Base):
    __tablename__ = "registrations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uid: Mapped[int] = mapped_column(Integer)
    ss58_hot: Mapped[str] = mapped_column(Text)
    ss58_cold: Mapped[str] = mapped_column(Text)
    block: Mapped[int] = mapped_column(BigInteger)
    block_date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    inserted_at: Mapped[dt.datetime] = timestamp()
    __table_args__ = (
        UniqueConstraint("uid", "block", name="uq_registrations_uid_block"),
        Index("ix_registrations_uid", "uid"),
        Index("ix_registrations_ss58_hot", "ss58_hot"),
    )


class Source(Base):
    __tablename__ = "sources"
    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = timestamp()
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_sources_hash"),
        CheckConstraint("octet_length(content) BETWEEN 1 AND 256000", name="ck_sources_size"),
    )


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotkey: Mapped[str] = mapped_column(Text)
    registration_id: Mapped[int | None] = mapped_column(ForeignKey("registrations.id"), unique=True)
    intake_mode: Mapped[str] = mapped_column(Text, server_default="subnet")
    source_sha256: Mapped[str] = mapped_column(ForeignKey("sources.sha256"))
    submitted_at: Mapped[dt.datetime] = timestamp()
    account_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    verification_identity: Mapped[str | None] = mapped_column(Text)
    proof_report: Mapped[dict | None] = mapped_column(JSONB)
    stage: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[dt.datetime] = timestamp()
    owner: Mapped[str | None] = mapped_column(Text)
    token: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("hotkey", "source_sha256", name="uq_submissions_hotkey_source"),
        CheckConstraint(
            "(intake_mode = 'subnet' AND registration_id IS NOT NULL) OR "
            "(intake_mode = 'dev' AND registration_id IS NULL)",
            name="ck_submissions_intake",
        ),
        CheckConstraint("state IN ('queued','running','proved','rejected')", name="ck_proof_state"),
        CheckConstraint(
            "(state = 'running') = (owner IS NOT NULL AND token IS NOT NULL "
            "AND lease_until IS NOT NULL)",
            name="ck_proof_lease",
        ),
        Index("ix_submissions_queue", "state", "next_attempt_at", "submitted_at", "id"),
        Index("ix_submissions_proof_cache", "source_sha256", "verification_identity", "state"),
    )


class BoardProgress(Base):
    __tablename__ = "board_progress"
    board: Mapped[str] = mapped_column(Text, primary_key=True)
    activation_boundary: Mapped[int] = mapped_column(BigInteger)
    last_submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"))
    last_submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    board: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer)
    config_hash: Mapped[str] = mapped_column(Text)
    reference_promotion_id: Mapped[int | None] = mapped_column(
        ForeignKey("promotions.id", use_alter=True, name="fk_evaluation_reference")
    )
    reference_hash: Mapped[str] = mapped_column(ForeignKey("sources.sha256"))
    plan: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, server_default="pending")
    decision: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = timestamp()
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("submission_id", "board", "generation"),
        CheckConstraint(
            "state IN ('pending','completed','superseded')", name="ck_evaluation_state"
        ),
        Index("ix_evaluation_pending", "board", "state", "submission_id"),
        Index(
            "uq_evaluation_board_pending", "board", unique=True, postgresql_where=state == "pending"
        ),
    )


class BenchmarkJob(Base):
    __tablename__ = "benchmark_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text)
    request: Mapped[dict] = mapped_column(JSONB)
    candidate_hash: Mapped[str] = mapped_column(ForeignKey("sources.sha256"))
    reference_hash: Mapped[str] = mapped_column(ForeignKey("sources.sha256"))
    environment: Mapped[str] = mapped_column(Text)
    lease_seconds: Mapped[float] = mapped_column(Float)
    retry_seconds: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    due_at: Mapped[dt.datetime] = timestamp()
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    claimant: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    token: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = timestamp()
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','completed','failed','cancelled')", name="ck_job_state"
        ),
        CheckConstraint(
            "lease_seconds > 0 AND lease_seconds <= 86400 AND retry_seconds > 0 "
            "AND retry_seconds <= 86400",
            name="ck_job_periods",
        ),
        CheckConstraint(
            "(state = 'running') = (owner IS NOT NULL AND token IS NOT NULL "
            "AND lease_until IS NOT NULL AND claimant IS NOT NULL)",
            name="ck_job_lease",
        ),
        Index("ix_job_due", "environment", "state", "due_at", "id"),
        Index("ix_job_reuse", "request_hash", "state"),
    )


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("benchmark_jobs.id"), primary_key=True)
    environment: Mapped[str] = mapped_column(Text)
    seed: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("evaluation_id", "environment", "seed"),)


class Promotion(Base):
    __tablename__ = "promotions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    board: Mapped[str] = mapped_column(Text)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), unique=True)
    source_environment: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    promoted_at: Mapped[dt.datetime] = timestamp()
    disqualified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    disqualification_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_promotions_board", "board", "id"),)


class CoordinatorState(Base):
    __tablename__ = "coordinator_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leader_token: Mapped[str | None] = mapped_column(Text)
    config_hash: Mapped[str | None] = mapped_column(Text)
    config_evidence: Mapped[dict | None] = mapped_column(JSONB)
    publication: Mapped[dict | None] = mapped_column(JSONB)
    publication_hash: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (CheckConstraint("id = 1", name="ck_coordinator_singleton"),)


class WeightSet(Base):
    __tablename__ = "weight_sets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    netuid: Mapped[int] = mapped_column(Integer)
    block: Mapped[int] = mapped_column(BigInteger)
    uids: Mapped[list] = mapped_column(JSONB)
    weights: Mapped[list] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(Boolean, server_default="false")
    error: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, server_default="false")
    publication_hash: Mapped[str | None] = mapped_column(Text)
    publication: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = timestamp()
    __table_args__ = (Index("ix_weight_sets_netuid_id", "netuid", "id"),)


class Audit(Base):
    """The champion audit of one promoted source by one audit version: its state and findings."""

    __tablename__ = "audits"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_hash: Mapped[str] = mapped_column(ForeignKey("sources.sha256"), nullable=False)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    audit_identity: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cases: Mapped[int] = mapped_column(Integer, nullable=False)
    report: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    due_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    request: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    owner: Mapped[str | None] = mapped_column(Text)
    token: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("source_hash", "audit_identity"),
        CheckConstraint(
            "state IN ('queued','running','clean','failed','error')",
            name="ck_exact_audit_state",
        ),
        CheckConstraint(
            "(state = 'running') = (owner IS NOT NULL AND token IS NOT NULL "
            "AND lease_until IS NOT NULL)",
            name="ck_audit_lease",
        ),
        Index("ix_audits_due", "state", "due_at"),
    )


class AuditCursor(Base):
    """A durable promotion-discovery cursor for each independently versioned audit profile."""

    __tablename__ = "audit_cursors"
    profile_identity: Mapped[str] = mapped_column(Text, primary_key=True)
    promotion_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class AuditAttempt(Base):
    """Attempt history survives retries, expired claims, and worker restarts."""

    __tablename__ = "audit_attempts"
    token: Mapped[str] = mapped_column(Text, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id"), nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    report: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class SourceDisqualification(Base):
    """Explicit operator eligibility decision; audit findings never write this table."""

    __tablename__ = "source_disqualifications"
    source_hash: Mapped[str] = mapped_column(ForeignKey("sources.sha256"), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    history: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
