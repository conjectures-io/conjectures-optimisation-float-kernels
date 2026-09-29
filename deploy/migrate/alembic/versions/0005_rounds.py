"""Rounds: the gate's paired per-round timings, the evidence statistical admission resamples.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29 UTC
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows have none: they rank, but cannot lead until re-verified.
    op.add_column("submissions", sa.Column("rounds", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("submissions", "rounds")
