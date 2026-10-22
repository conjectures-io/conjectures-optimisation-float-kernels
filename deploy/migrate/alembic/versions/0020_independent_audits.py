"""Independent leased audit workers and explicit operator source disqualification."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "exact_promotions",
        sa.Column("source_environment", JSONB, nullable=False, server_default="{}"),
    )
    op.add_column("exact_audits", sa.Column("request", JSONB, nullable=False, server_default="{}"))
    op.add_column("exact_audits", sa.Column("owner", sa.Text))
    op.add_column("exact_audits", sa.Column("token", sa.Text))
    op.add_column("exact_audits", sa.Column("lease_until", sa.DateTime(timezone=True)))
    # Old in-flight tasks have no immutable request. Preserve their history; independent
    # discovery backfills all promotions under a new profile rather than adopting these claims.
    op.execute("UPDATE exact_audits SET state = 'error' WHERE state = 'running'")
    op.create_table(
        "exact_audit_cursors",
        sa.Column("profile_identity", sa.Text, primary_key=True),
        sa.Column("promotion_id", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_table(
        "exact_audit_attempts",
        sa.Column("token", sa.Text, primary_key=True),
        sa.Column("audit_id", sa.BigInteger, sa.ForeignKey("exact_audits.id"), nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="running"),
        sa.Column("report", JSONB),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "exact_disqualifications",
        sa.Column("source_hash", sa.Text, primary_key=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("history", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    # Do not silently re-enable a source by reverting an active operator decision.
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM exact_disqualifications WHERE active)")):
        raise RuntimeError("restore active operator disqualifications before downgrading")
    op.drop_table("exact_disqualifications")
    op.drop_table("exact_audit_cursors")
    op.drop_table("exact_audit_attempts")
    op.drop_column("exact_promotions", "source_environment")
    for name in ("lease_until", "token", "owner", "request"):
        op.drop_column("exact_audits", name)
