"""Record the champion each ratio was measured against, so a promotion starts a fresh board."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("champion_sha256", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_submissions_champion_sha256",
        "submissions",
        "champion_sha256 IS NULL OR champion_sha256 ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_submissions_champion_sha256", "submissions", type_="check")
    op.drop_column("submissions", "champion_sha256")
