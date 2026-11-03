"""Support sources up to a fixed 4 MiB ceiling; upload policy lives in the API."""

from pathlib import Path

from alembic import op

revision = "tensor_0004"
down_revision = "tensor_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    # Frozen historical values: do not import the current runtime ceiling here.
    op.execute(
        """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM sources WHERE octet_length(content) > 256000) THEN
            RAISE EXCEPTION 'Cannot downgrade: sources larger than 256000 bytes exist';
        END IF;
        END $$;"""
    )
    op.execute(
        "ALTER TABLE sources DROP CONSTRAINT ck_sources_size, "
        "ADD CONSTRAINT ck_sources_size CHECK (octet_length(content) BETWEEN 1 AND 256000)"
    )
