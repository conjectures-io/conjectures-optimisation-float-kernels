-- Create benchmark_worker as an operator before applying; set its password separately.
-- It can fetch work/sources and submit fenced observations; it cannot promote or publish policy.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM benchmark_worker;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM benchmark_worker;
GRANT USAGE ON SCHEMA public TO benchmark_worker;
DO $$ BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO benchmark_worker', current_database());
END $$;
GRANT EXECUTE ON FUNCTION public.claim(text,text,text,text) TO benchmark_worker;
GRANT EXECUTE ON FUNCTION public.heartbeat(bigint,text,text) TO benchmark_worker;
GRANT EXECUTE ON FUNCTION public.complete(bigint,text,text,jsonb) TO benchmark_worker;
GRANT EXECUTE ON FUNCTION public.fail(bigint,text,text,jsonb) TO benchmark_worker;
GRANT EXECUTE ON FUNCTION public.sources(bigint) TO benchmark_worker;
