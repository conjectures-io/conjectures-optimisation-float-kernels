"""The sum slot: stage 2's first tensor slot, proved against TorchLean's left-to-right sum.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08 UTC
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint(
        "ck_submissions_slot",
        "submissions",
        "slot IN ('add', 'mul', 'fma', 'div', 'sqrt', 'sum')",
    )


def downgrade() -> None:
    # Fails while a sum submission exists: delete those first, deliberately.
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint(
        "ck_submissions_slot", "submissions", "slot IN ('add', 'mul', 'fma', 'div', 'sqrt')"
    )
