"""Champion audits: each promoted exact-runtime source checked against the spec by each audit version."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    # Definitions are intentionally frozen here; later model edits must not alter this revision.
    op.create_table(
        "exact_audits",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("source_hash", sa.Text, nullable=False),
        sa.Column("submission_id", sa.BigInteger, sa.ForeignKey("submissions.id"), nullable=False),
        sa.Column("audit_identity", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column("seed", sa.BigInteger, nullable=False),
        sa.Column("cases", sa.Integer, nullable=False),
        sa.Column("report", JSONB),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "due_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_hash", "audit_identity"),
        sa.CheckConstraint(
            "state IN ('queued','running','clean','failed','error')", name="ck_exact_audit_state"
        ),
    )
    op.create_index("ix_exact_audits_due", "exact_audits", ["state", "due_at"])


def downgrade():
    op.drop_index("ix_exact_audits_due", table_name="exact_audits")
    op.drop_table("exact_audits")
