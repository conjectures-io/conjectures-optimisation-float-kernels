-- Keep bounded, append-only measurement progress separate from the immutable final result.
CREATE TABLE public.benchmark_job_progress (
    job_id bigint NOT NULL REFERENCES public.benchmark_jobs(id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    sequence integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL,
    CONSTRAINT ck_job_progress_sequence CHECK (attempt > 0 AND sequence >= 0),
    CONSTRAINT ck_job_progress_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    PRIMARY KEY (job_id, attempt, sequence)
);
CREATE INDEX ix_job_progress_created ON public.benchmark_job_progress(job_id, created_at);
REVOKE ALL ON TABLE public.benchmark_job_progress FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.worker_access() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT jsonb_build_object('protocol',3,'login',session_user,
 'kind',w.kind,'enabled',coalesce(w.enabled,false),
 'environments',coalesce((SELECT jsonb_agg(e.environment ORDER BY e.environment)
 FROM public.worker_environments e WHERE e.login=session_user),'[]'::jsonb))
 FROM (SELECT 1) x LEFT JOIN public.worker_logins w ON w.login=session_user;
$$;

CREATE FUNCTION public.write_benchmark_progress(
    jid bigint, worker text, attempt_token text, attempt_number integer,
    event_sequence integer, event jsonb
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
BEGIN
    IF attempt_number < 1 OR event_sequence < 0 OR event IS NULL
       OR jsonb_typeof(event) <> 'object' OR octet_length(event::text) > 1048576 THEN
        RAISE EXCEPTION 'invalid benchmark progress event';
    END IF;
    PERFORM 1 FROM public.benchmark_jobs j
      WHERE j.id=jid AND j.state='running' AND j.claimant=session_user
        AND public.worker_authorized(j.environment) AND j.owner=worker AND j.token=attempt_token
        AND j.attempts=attempt_number AND j.lease_until>clock_timestamp()
      FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.benchmark_job_progress(job_id,attempt,sequence,payload)
      VALUES (jid,attempt_number,event_sequence,event)
      ON CONFLICT (job_id,attempt,sequence) DO NOTHING;
    RETURN true;
END $$;

REVOKE ALL ON FUNCTION public.write_benchmark_progress(bigint,text,text,integer,integer,jsonb)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.write_benchmark_progress(bigint,text,text,integer,integer,jsonb)
  TO tensor_benchmark_access;
