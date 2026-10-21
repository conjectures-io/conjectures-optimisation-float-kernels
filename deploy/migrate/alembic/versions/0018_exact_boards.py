"""Optional exact-runtime evaluations, leased jobs and independent board histories."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("submissions", sa.Column("exact_verification", JSONB))
    op.drop_constraint("ck_submissions_slot", "submissions")
    op.create_check_constraint(
        "ck_submissions_slot",
        "submissions",
        "slot IN ('full','add','mul','fma','div','sqrt','sum','bmm','exact')",
    )
    # Definitions are intentionally frozen here; later model edits must not alter this revision.
    op.create_table(
        "exact_evaluations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("submission_id", sa.BigInteger, sa.ForeignKey("submissions.id"), nullable=False),
        sa.Column("board", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("planning_key", sa.Text, nullable=False, unique=True),
        sa.Column("config_hash", sa.Text, nullable=False),
        sa.Column("reference_promotion_id", sa.BigInteger),
        sa.Column("reference_hash", sa.Text, nullable=False),
        sa.Column("reference_source", sa.LargeBinary, nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("decision", JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("submission_id", "board", "generation"),
        sa.CheckConstraint(
            "state IN ('pending','completed','superseded')", name="ck_exact_evaluation_state"
        ),
    )
    op.create_index(
        "ix_exact_evaluation_pending", "exact_evaluations", ["board", "state", "submission_id"]
    )
    op.create_table(
        "exact_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "evaluation_id",
            sa.BigInteger,
            sa.ForeignKey("exact_evaluations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("environment", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column(
            "due_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column("owner", sa.Text),
        sa.Column("token", sa.Text),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("result", JSONB),
        sa.Column("error", JSONB),
        sa.UniqueConstraint("evaluation_id", "request_hash"),
        sa.CheckConstraint(
            "state IN ('queued','running','completed','failed','cancelled')",
            name="ck_exact_job_state",
        ),
    )
    op.create_index("ix_exact_job_due", "exact_jobs", ["environment", "state", "due_at", "id"])
    op.create_index("ix_exact_job_expiry", "exact_jobs", ["state", "lease_until"])
    op.create_table(
        "exact_promotions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("board", sa.Text, nullable=False),
        sa.Column("submission_id", sa.BigInteger, sa.ForeignKey("submissions.id"), nullable=False),
        sa.Column(
            "evaluation_id",
            sa.BigInteger,
            sa.ForeignKey("exact_evaluations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("source_hash", sa.Text, nullable=False),
        sa.Column(
            "promoted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("disqualified_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_exact_promotions_board", "exact_promotions", ["board", "id"])
    op.execute(Path(__file__).with_name("0018_exact_protocol.sql").read_text())


def downgrade():
    # Refuse a destructive downgrade while exact submissions exist. Remove disposable test
    # entries explicitly first; a production downgrade must archive/migrate their data.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM submissions WHERE slot='exact')")
    ):
        raise RuntimeError("archive exact submissions before downgrade")
    for signature in (
        "exact_claim(text,text,text,text,double precision)",
        "exact_heartbeat(bigint,text,text,double precision)",
        "exact_complete(bigint,text,text,jsonb)",
        "exact_fail(bigint,text,text,jsonb,double precision)",
        "exact_sources(bigint)",
    ):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.drop_table("exact_promotions")
    op.drop_table("exact_jobs")
    op.drop_table("exact_evaluations")
    op.drop_column("submissions", "exact_verification")
    op.drop_constraint("ck_submissions_slot", "submissions")
    op.create_check_constraint(
        "ck_submissions_slot",
        "submissions",
        "slot IN ('full','add','mul','fma','div','sqrt','sum','bmm')",
    )
