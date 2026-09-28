"""Gate errors: how many gate runs crashed on a submission, so one cannot retry forever.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28 UTC
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows start at zero crashes.
    op.add_column(
        "submissions", sa.Column("gate_errors", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("submissions", "gate_errors")
