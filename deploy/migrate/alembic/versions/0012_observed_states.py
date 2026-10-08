"""Persist informational gate provenance for transition-only drift warnings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "observed_states",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("details", JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("observed_states")
