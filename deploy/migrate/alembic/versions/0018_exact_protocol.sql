-- Immutable migration companion. All worker writes pass through fenced operations.
CREATE FUNCTION public.exact_claim(env text, identity text, worker text, attempt text, lease_seconds double precision)
RETURNS SETOF public.exact_jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE chosen bigint;
BEGIN
    IF lease_seconds <= 0 OR lease_seconds > 86400 OR length(attempt) < 16 THEN
        RAISE EXCEPTION 'invalid claim';
    END IF;
    UPDATE public.exact_jobs SET state='queued', token=NULL, lease_until=NULL,
        due_at=clock_timestamp()+make_interval(secs=>lease_seconds),
        error=jsonb_build_object('kind','infrastructure','reason','lease expired')
    WHERE environment=env AND state='running' AND lease_until <= clock_timestamp();
    SELECT j.id INTO chosen FROM public.exact_jobs j
      JOIN public.exact_evaluations e ON e.id=j.evaluation_id
      WHERE j.environment=env AND j.request->>'identity'=identity
        AND j.state='queued' AND j.due_at<=clock_timestamp() AND e.state='pending'
      ORDER BY j.due_at,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED;
    RETURN QUERY UPDATE public.exact_jobs SET state='running',owner=worker,token=attempt,
        lease_until=clock_timestamp()+make_interval(secs=>COALESCE((request->>'lease_seconds')::double precision,lease_seconds)),started_at=clock_timestamp(),attempts=attempts+1
      WHERE id=chosen RETURNING *;
END $$;

CREATE FUNCTION public.exact_heartbeat(jid bigint, worker text, attempt text, seconds double precision)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF seconds <= 0 OR seconds > 86400 THEN RAISE EXCEPTION 'invalid lease'; END IF;
    UPDATE public.exact_jobs SET lease_until=clock_timestamp()+make_interval(secs=>seconds)
      WHERE id=jid AND state='running' AND owner=worker AND token=attempt AND lease_until>clock_timestamp();
    RETURN FOUND;
END $$;

CREATE FUNCTION public.exact_complete(jid bigint, worker text, attempt text, payload jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF payload IS NULL OR jsonb_typeof(payload) <> 'object' OR octet_length(payload::text)>16777216 THEN
        RAISE EXCEPTION 'invalid result';
    END IF;
    UPDATE public.exact_jobs SET state='completed',result=payload,completed_at=clock_timestamp(),
        token=NULL,lease_until=NULL,error=NULL
      WHERE id=jid AND state='running' AND owner=worker AND token=attempt AND lease_until>clock_timestamp();
    RETURN FOUND;
END $$;

CREATE FUNCTION public.exact_fail(jid bigint, worker text, attempt text, failure jsonb, seconds double precision)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF seconds<=0 OR seconds>86400 OR failure->>'kind' NOT IN ('candidate','reference','infrastructure','checksum') THEN
        RAISE EXCEPTION 'invalid failure';
    END IF;
    UPDATE public.exact_jobs SET state=CASE WHEN failure->>'kind'='candidate' THEN 'failed' ELSE 'queued' END,
        error=jsonb_build_object('kind',failure->>'kind','reason',left(failure->>'reason',2000)),
        due_at=clock_timestamp()+make_interval(secs=>least(86400,seconds*power(2,least(attempts-1,4)))),token=NULL,lease_until=NULL
      WHERE id=jid AND state='running' AND owner=worker AND token=attempt AND lease_until>clock_timestamp();
    RETURN FOUND;
END $$;

CREATE FUNCTION public.exact_sources(jid bigint)
RETURNS TABLE(candidate bytea, reference bytea) LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT f.content,e.reference_source FROM public.exact_jobs j
      JOIN public.exact_evaluations e ON e.id=j.evaluation_id
      JOIN public.submission_files f ON f.submission_id=e.submission_id AND f.name='Kernel.lean'
      WHERE j.id=jid;
$$;

REVOKE ALL ON FUNCTION public.exact_claim(text,text,text,text,double precision) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.exact_heartbeat(bigint,text,text,double precision) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.exact_complete(bigint,text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.exact_fail(bigint,text,text,jsonb,double precision) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.exact_sources(bigint) FROM PUBLIC;
