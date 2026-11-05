"""Allow a claimed benchmark bundle to fetch only its listed champion sources."""

from pathlib import Path

from alembic import op

revision = "tensor_0006"
down_revision = "tensor_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    progress = Path(__file__).with_name("0005_incremental_progress.sql").read_text(encoding="utf-8")
    start = progress.index("CREATE OR REPLACE FUNCTION public.worker_access()")
    end = progress.index("$$;", start) + len("$$;")
    op.execute(progress[start:end])
    op.execute("DROP FUNCTION public.bundle_sources(bigint,text,text)")
