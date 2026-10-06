"""Platform: submission files in the store, the gate's report, accounts, and the published scores.

What the conjectures platform needs to serve this competition from this database (its schema check
names `submission_files.content`, `submissions.account_id` and `weight_sets.api_snapshot`):
Kernel.lean kept in the database, so the platform can queue a submission without a shared disk;
the gate's verdict JSON as a submission's report; the platform account a submission came from;
and the scoring each weight set was computed from, published as one JSON object.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 UTC
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing submissions keep their file on disk; the gate worker falls back to it.
    op.create_table(
        "submission_files",
        sa.Column(
            "submission_id",
            sa.BigInteger(),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("name = 'Kernel.lean'", name="ck_submission_files_name"),
    )
    op.add_column("submissions", sa.Column("report", sa.Text(), nullable=True))
    op.add_column("submissions", sa.Column("account_id", sa.Text(), nullable=True))
    op.add_column("weight_sets", sa.Column("api_snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("weight_sets", "api_snapshot")
    op.drop_column("submissions", "account_id")
    op.drop_column("submissions", "report")
    op.drop_table("submission_files")
