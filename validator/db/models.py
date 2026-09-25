"""The validator schema; the Alembic revisions under deploy/migrate must match these models."""

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .status import STATE_VALUES


class Base(DeclarativeBase):
    # The declarative root every table below hangs off.
    pass


def _in_list(values: tuple[str, ...]) -> str:
    # Render a tuple of strings as a SQL IN list body: 'a', 'b', 'c'.
    return ", ".join(f"'{v}'" for v in values)


class Registration(Base):
    # One uid's hot/cold key assignment as of the block it registered at; one submission slot.
    __tablename__ = "registrations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(Integer, nullable=False)
    ss58_hot: Mapped[str] = mapped_column(Text, nullable=False)
    ss58_cold: Mapped[str] = mapped_column(Text, nullable=False)
    # The block this uid registered at (its on-chain BlockAtRegistration).
    block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inserted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    claim: Mapped[EntitlementClaim | None] = relationship(back_populates="registration")

    __table_args__ = (
        UniqueConstraint("uid", "block", name="uq_registrations_uid_block"),
        Index("ix_registrations_uid", "uid"),
        Index("ix_registrations_ss58_hot", "ss58_hot"),
    )


class Submission(Base):
    # One signed upload of Kernel.lean, and what the gate made of it.
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256(Kernel.lean), hex: what the miner signed, and the submission's identity.
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    exit_code: Mapped[int | None] = mapped_column(Integer)

    # --- the gate's verdict ------------------------------------------------------
    # The stage it stopped at (or `score` when accepted) and the one-line reason.
    stage: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    # Submission time over incumbent time, summed over the timed classes; lower is faster.
    ratio: Mapped[float | None] = mapped_column(Float)
    # Per operand class: incumbent_ns, submission_ns, ratio, spreads.
    classes: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    # The incumbent the ratio was measured against; ratios compare only within one incumbent.
    incumbent_sha256: Mapped[str | None] = mapped_column(Text)
    gate_seconds: Mapped[float | None] = mapped_column(Float)

    # --- queue bookkeeping -------------------------------------------------------
    worker_id: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # A fresh token per claim, so a finish from a stale worker is refused.
    verification_attempt: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    claim: Mapped[EntitlementClaim | None] = relationship(back_populates="submission")

    __table_args__ = (
        UniqueConstraint("hotkey", "digest", name="uq_submissions_hotkey_digest"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_submissions_digest"),
        CheckConstraint(
            "incumbent_sha256 IS NULL OR incumbent_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_submissions_incumbent_sha256",
        ),
        CheckConstraint("ratio IS NULL OR ratio > 0", name="ck_submissions_ratio"),
        CheckConstraint(f"state IN ({_in_list(STATE_VALUES)})", name="ck_submissions_state"),
        Index("ix_submissions_hotkey", "hotkey"),
        # The queue scan: oldest queued first, arrival order.
        Index("ix_submissions_state_submitted_at", "state", "submitted_at", "id"),
    )


class EntitlementClaim(Base):
    # One registration spent on one accepted submission; the keys enforce the rule.
    __tablename__ = "entitlement_claims"

    registration_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("registrations.id", ondelete="CASCADE"), primary_key=True
    )
    submission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    claimed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    registration: Mapped[Registration] = relationship(back_populates="claim")
    submission: Mapped[Submission] = relationship(back_populates="claim")


class WeightSet(Base):
    # Every set_weights attempt, accepted or not, so a disputed epoch stays reconstructable.
    __tablename__ = "weight_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    netuid: Mapped[int] = mapped_column(Integer, nullable=False)
    block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    weights: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_weight_sets_created_at", "created_at"),)


class ScoreSnapshot(Base):
    # One hotkey's scoring input and output for one weight_sets row.
    __tablename__ = "score_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    weight_set_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("weight_sets.id", ondelete="CASCADE"), nullable=False
    )
    hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    submission_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("submissions.id", ondelete="SET NULL")
    )
    ratio: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    __table_args__ = (
        Index("ix_score_snapshots_weight_set_id", "weight_set_id"),
        Index("ix_score_snapshots_hotkey", "hotkey"),
    )


class RateLimitWindow(Base):
    # A fixed-window write counter per subject (a hotkey, or an IP), shared by all API workers.
    __tablename__ = "rate_limit_windows"

    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (Index("ix_rate_limit_windows_window_start", "window_start"),)
