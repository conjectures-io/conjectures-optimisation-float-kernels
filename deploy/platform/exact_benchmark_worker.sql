-- Create exact_benchmark_worker as an operator before applying; set its password separately.
-- It can fetch work/sources and submit fenced observations; it cannot promote or publish policy.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM exact_benchmark_worker;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM exact_benchmark_worker;
GRANT USAGE ON SCHEMA public TO exact_benchmark_worker;
DO $$ BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO exact_benchmark_worker', current_database());
END $$;
GRANT EXECUTE ON FUNCTION public.exact_claim(text,text,text,text,double precision) TO exact_benchmark_worker;
GRANT EXECUTE ON FUNCTION public.exact_heartbeat(bigint,text,text,double precision) TO exact_benchmark_worker;
GRANT EXECUTE ON FUNCTION public.exact_complete(bigint,text,text,jsonb) TO exact_benchmark_worker;
GRANT EXECUTE ON FUNCTION public.exact_fail(bigint,text,text,jsonb,double precision) TO exact_benchmark_worker;
GRANT EXECUTE ON FUNCTION public.exact_sources(bigint) TO exact_benchmark_worker;
