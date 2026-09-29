"""Verifier fingerprint: the verifier a verdict came from, so a changed gate retires old verdicts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 UTC
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows have none, so they leave scoring until tools/reverify.py re-stamps them.
    op.add_column("submissions", sa.Column("verifier_fingerprint", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_submissions_verifier_fingerprint",
        "submissions",
        "verifier_fingerprint IS NULL OR verifier_fingerprint ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_submissions_verifier_fingerprint", "submissions", type_="check")
    op.drop_column("submissions", "verifier_fingerprint")
