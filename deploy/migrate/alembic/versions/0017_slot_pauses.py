"""Persist unavailable slots without disqualifying their champions or challengers."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slot_pauses",
        sa.Column("slot", sa.Text(), primary_key=True),
        sa.Column(
            "challenger_id", sa.BigInteger(), sa.ForeignKey("submissions.id", ondelete="SET NULL")
        ),
        sa.Column(
            "reference_id", sa.BigInteger(), sa.ForeignKey("submissions.id", ondelete="SET NULL")
        ),
        sa.Column("reference_sha256", sa.Text()),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("slot_pauses")
