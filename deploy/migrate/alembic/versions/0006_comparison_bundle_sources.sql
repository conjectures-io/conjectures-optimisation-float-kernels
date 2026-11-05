-- Bundle workers can read only the candidate and champion hashes named in their lease.
CREATE FUNCTION public.bundle_sources(jid bigint, worker text, attempt text)
RETURNS TABLE(candidate bytea, reference_hash text, reference bytea)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT DISTINCT a.content, b.sha256, b.content
      FROM public.benchmark_jobs j
      JOIN public.sources a ON a.sha256=j.candidate_hash
      CROSS JOIN LATERAL jsonb_each(j.request->'comparisons') AS comparison(board, spec)
      JOIN public.sources b ON b.sha256=comparison.spec->>'reference_hash'
     WHERE j.id=jid AND j.claimant=session_user
       AND public.worker_authorized(j.environment)
       AND j.state='running' AND j.owner=worker AND j.token=attempt
       AND j.lease_until>clock_timestamp()
     ORDER BY b.sha256;
$$;
REVOKE ALL ON FUNCTION public.bundle_sources(bigint,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.bundle_sources(bigint,text,text) TO tensor_benchmark_access;

CREATE OR REPLACE FUNCTION public.worker_access() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT jsonb_build_object('protocol',4,'login',session_user,
 'kind',w.kind,'enabled',coalesce(w.enabled,false),
 'environments',coalesce((SELECT jsonb_agg(e.environment ORDER BY e.environment)
 FROM public.worker_environments e WHERE e.login=session_user),'[]'::jsonb))
 FROM (SELECT 1) x LEFT JOIN public.worker_logins w ON w.login=session_user;
$$;
