"""Allow baseline-role failures to retain bounded benchmark evidence."""

from pathlib import Path

from alembic import op

revision = "tensor_0007"
down_revision = "tensor_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.fail(jid bigint, worker text, attempt text, failure jsonb)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE evidence jsonb := jsonb_build_object();
        BEGIN
            IF failure IS NULL OR failure->>'kind' IS NULL
               OR failure->>'kind' NOT IN ('candidate','reference','infrastructure','checksum')
               OR failure->>'reason' IS NULL THEN RAISE EXCEPTION 'invalid failure'; END IF;
            IF jsonb_typeof(failure->'timeout')='object' THEN
                evidence := evidence || jsonb_build_object('timeout',failure->'timeout');
            END IF;
            IF jsonb_typeof(failure->'partial')='object' THEN
                evidence := evidence || jsonb_build_object('partial',failure->'partial');
            END IF;
            IF octet_length(evidence::text)>1048576 THEN
                evidence := (evidence - 'partial') || jsonb_build_object('truncated',true);
            END IF;
            IF octet_length(evidence::text)>1048576 THEN
                evidence := jsonb_build_object('truncated',true);
            END IF;
            UPDATE public.benchmark_jobs SET
                state=CASE WHEN failure->>'kind'='candidate' THEN 'failed' ELSE 'queued' END,
                error=evidence || jsonb_build_object('kind',failure->>'kind','reason',left(failure->>'reason',2000)),
                due_at=clock_timestamp()+make_interval(secs=>least(86400,retry_seconds*power(2,least(attempts-1,4)))),
                claimant=NULL,owner=NULL,token=NULL,lease_until=NULL
              WHERE id=jid AND state='running' AND claimant=session_user
                AND public.worker_authorized(environment) AND owner=worker AND token=attempt
                AND lease_until>clock_timestamp();
            RETURN FOUND;
        END $$;
        """
    )
