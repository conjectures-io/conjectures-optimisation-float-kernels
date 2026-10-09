"""Deduplicate scoring publications and index bounded API snapshot cleanup."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("weight_sets", sa.Column("scoring_content_sha256", sa.Text(), nullable=True))
    op.create_index("ix_weight_sets_netuid_id", "weight_sets", ["netuid", "id"])
    op.create_index(
        "ix_weight_sets_snapshot_latest",
        "weight_sets",
        ["netuid", "id"],
        postgresql_where=sa.text("api_snapshot IS NOT NULL"),
    )
    op.create_index(
        "ix_weight_sets_snapshot_expiry",
        "weight_sets",
        ["netuid", "created_at", "id"],
        postgresql_where=sa.text("api_snapshot IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_weight_sets_snapshot_expiry", table_name="weight_sets")
    op.drop_index("ix_weight_sets_snapshot_latest", table_name="weight_sets")
    op.drop_index("ix_weight_sets_netuid_id", table_name="weight_sets")
    op.drop_column("weight_sets", "scoring_content_sha256")
