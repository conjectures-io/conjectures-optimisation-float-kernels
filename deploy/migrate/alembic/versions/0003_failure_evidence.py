"""Failed jobs keep the worker's evidence; candidate timeouts are decided `timed-out`."""

from pathlib import Path

from alembic import op

revision = "tensor_0003"
down_revision = "tensor_0002"
branch_labels = None
depends_on = None

VERSIONS = Path(__file__).parent


def upgrade():
    op.execute((VERSIONS / "0003_failure_evidence.sql").read_text(encoding="utf-8"))


def downgrade():
    # The original fail() is the one tensor_0001 created; a job's extra evidence is left in place.
    protocol = (VERSIONS / "0001_job_protocol.sql").read_text(encoding="utf-8")
    start = protocol.index("CREATE FUNCTION public.fail(")
    end = protocol.index("END $$;", start) + len("END $$;")
    op.execute("CREATE OR REPLACE" + protocol[start + len("CREATE") : end])
    op.execute(
        "UPDATE evaluations SET decision = (decision - 'timeout') "
        "|| jsonb_build_object('outcome','benchmark-failed') WHERE decision->>'outcome'='timed-out'"
    )
