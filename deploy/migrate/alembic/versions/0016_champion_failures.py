"""A champion whose own bench keeps failing is demoted: count its failed runs and when it fell."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promotions", sa.Column("failures", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("promotions", sa.Column("demoted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("promotions", "demoted_at")
    op.drop_column("promotions", "failures")
