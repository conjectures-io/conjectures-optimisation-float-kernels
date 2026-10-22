"""Fresh tensor competition schema; not an upgrade of the former competition."""

from pathlib import Path

from alembic import op

revision = "tensor_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_name("0001_schema.sql").read_text(encoding="utf-8"))
    op.execute(Path(__file__).with_name("0001_job_protocol.sql").read_text(encoding="utf-8"))
    op.execute(Path(__file__).with_name("0001_worker_roles.sql").read_text(encoding="utf-8"))


def downgrade():
    for signature in (
        "claim(text,text,text,text)",
        "heartbeat(bigint,text,text)",
        "complete(bigint,text,text,jsonb)",
        "fail(bigint,text,text,jsonb)",
        "sources(bigint,text,text)",
        "worker_access()",
        "worker_authorized(text)",
        "immutable_measurement()",
        "immutable_source()",
    ):
        op.execute("DROP FUNCTION public." + signature + " CASCADE")
    op.execute("ALTER TABLE evaluations DROP CONSTRAINT fk_evaluation_reference")
    for table in (
        "audit_attempts",
        "audit_cursors",
        "audits",
        "source_disqualifications",
        "evaluation_jobs",
        "promotions",
        "evaluations",
        "benchmark_jobs",
        "board_progress",
        "submissions",
        "sources",
        "registrations",
        "coordinator_state",
        "weight_sets",
        "worker_environments",
        "worker_logins",
    ):
        op.drop_table(table)
