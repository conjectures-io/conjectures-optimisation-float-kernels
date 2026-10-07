"""Division and square-root slots beside add, mul and fma.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06 UTC
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint(
        "ck_submissions_slot", "submissions", "slot IN ('add', 'mul', 'fma', 'div', 'sqrt')"
    )


def downgrade() -> None:
    # Fails while a div or sqrt submission exists: delete those first, deliberately.
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint(
        "ck_submissions_slot", "submissions", "slot IN ('add', 'mul', 'fma')"
    )
