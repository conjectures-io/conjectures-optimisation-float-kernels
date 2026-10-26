"""Explicit development intake without fabricated chain registrations."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "tensor_0002"
down_revision = "tensor_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    if op.get_bind().scalar(
        text("SELECT EXISTS(SELECT 1 FROM submissions WHERE registration_id IS NULL)")
    ):
        raise RuntimeError("Remove development submissions before downgrading tensor_0002")
    op.drop_constraint("ck_submissions_intake", "submissions", type_="check")
    op.drop_column("submissions", "intake_mode")
    op.alter_column("submissions", "registration_id", nullable=False)
