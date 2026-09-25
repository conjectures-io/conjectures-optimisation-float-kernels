"""Submission slot: add, mul or fma, part of each submission's identity.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25 UTC
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows are add submissions; the same file may now be queued once per slot.
    op.add_column("submissions", sa.Column("slot", sa.Text(), server_default="add", nullable=False))
    op.create_check_constraint(
        "ck_submissions_slot", "submissions", "slot IN ('add', 'mul', 'fma')"
    )
    op.drop_constraint("uq_submissions_hotkey_digest", "submissions", type_="unique")
    op.create_unique_constraint(
        "uq_submissions_hotkey_slot_digest", "submissions", ["hotkey", "slot", "digest"]
    )


def downgrade() -> None:
    # Back to one submission per (hotkey, digest); fails if a file was queued in two slots.
    op.drop_constraint("uq_submissions_hotkey_slot_digest", "submissions", type_="unique")
    op.create_unique_constraint("uq_submissions_hotkey_digest", "submissions", ["hotkey", "digest"])
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.drop_column("submissions", "slot")
