"""The bmm slot: TorchLean's batched matrix multiply, beside its sum.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08 UTC
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint(
        "ck_submissions_slot",
        "submissions",
        "slot IN ('add', 'mul', 'fma', 'div', 'sqrt', 'sum', 'bmm')",
    )


def downgrade() -> None:
    # Fails while a bmm submission exists: delete those first, deliberately.
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint(
        "ck_submissions_slot",
        "submissions",
        "slot IN ('add', 'mul', 'fma', 'div', 'sqrt', 'sum')",
    )
