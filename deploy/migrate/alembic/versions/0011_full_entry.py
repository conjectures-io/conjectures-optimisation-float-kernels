"""The full entry: one paid board, FloatLib's add and mul with TorchLean's sum and bmm.

The slot every entry lands in unless it names another: the platform inserts without a slot, so the
server default moves from `add` to `full`.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08 UTC
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SLOTS = "'full', 'add', 'mul', 'fma', 'div', 'sqrt', 'sum', 'bmm'"
OLD_SLOTS = "'add', 'mul', 'fma', 'div', 'sqrt', 'sum', 'bmm'"


def upgrade() -> None:
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint("ck_submissions_slot", "submissions", f"slot IN ({SLOTS})")
    op.alter_column("submissions", "slot", server_default="full")


def downgrade() -> None:
    # Fails while a full entry exists: delete those first, deliberately.
    op.alter_column("submissions", "slot", server_default="add")
    op.drop_constraint("ck_submissions_slot", "submissions", type_="check")
    op.create_check_constraint("ck_submissions_slot", "submissions", f"slot IN ({OLD_SLOTS})")
