"""Persist append-only benchmark progress events while a worker holds its lease."""

from pathlib import Path

from alembic import op

revision = "tensor_0005"
down_revision = "tensor_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    protocol = Path(__file__).with_name("0001_job_protocol.sql").read_text(encoding="utf-8")
    start = protocol.index("CREATE FUNCTION public.worker_access()")
    end = protocol.index("$$;", start) + len("$$;")
    op.execute("CREATE OR REPLACE" + protocol[start + len("CREATE") : end])
    op.execute(
        "DROP FUNCTION public.write_benchmark_progress(bigint,text,text,integer,integer,jsonb)"
    )
    op.drop_table("benchmark_job_progress")
