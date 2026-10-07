"""Score snapshots per slot: every slot's leader is paid an equal part of the competition's share.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07 UTC
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every earlier snapshot scored the add board, the only one that paid.
    op.add_column(
        "score_snapshots",
        sa.Column("slot", sa.Text(), server_default="add", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("score_snapshots", "slot")
