"""Automatic promotion: the submissions that beat their slot's champion and became the new one."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

SLOTS = "'full', 'add', 'mul', 'fma', 'div', 'sqrt', 'sum', 'bmm'"


def upgrade() -> None:
    op.create_table(
        "promotions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column(
            "submission_id",
            sa.BigInteger(),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("champion_sha256", sa.Text(), nullable=False),
        sa.Column(
            "promoted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"slot IN ({SLOTS})", name="ck_promotions_slot"),
        sa.CheckConstraint(
            "champion_sha256 ~ '^[0-9a-f]{64}$'", name="ck_promotions_champion_sha256"
        ),
    )
    op.create_index("ix_promotions_slot_id", "promotions", ["slot", "id"])


def downgrade() -> None:
    op.drop_index("ix_promotions_slot_id", table_name="promotions")
    op.drop_table("promotions")
