-- The conjectures platform API's access to this database, and nothing more.
--
-- Once the platform serves FloatLib (deploy/platform/CONTRACT.md), it queues submissions and
-- reads the published scoring straight from this schema, connecting as `platform_api`: a role
-- that can queue a submission and read what the weight setter published, and can change
-- nothing the gate, the chain watcher or the weight setter own.
--
-- Plain SQL, no psql variables: deploy/platform/grant.sh pipes it through psql, and
-- tests/test_platform_grants.py runs it against a migrated scratch database and checks every
-- privilege it leaves. The role must exist already; grant.sh creates it.
--
-- Idempotent and exact: it revokes first, so re-running after a migration leaves exactly this
-- set rather than whatever was granted by hand. A migration that adds a table the platform
-- reads changes this file and that test together.

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM platform_api;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM platform_api;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO platform_api', current_database());
END
$$;
GRANT USAGE ON SCHEMA public TO platform_api;

-- Intake: a submission row and its Kernel.lean, idempotent per (hotkey, slot, digest). The
-- INSERT ... RETURNING id needs SELECT, and the id comes from the sequence.
GRANT SELECT, INSERT ON submissions, submission_files TO platform_api;
GRANT USAGE ON SEQUENCE submissions_id_seq TO platform_api;

-- The per-hotkey submit limit: an upsert on a counter row.
GRANT SELECT, INSERT, UPDATE ON rate_limit_windows TO platform_api;

-- Entitlement: a registration that no claim has spent yet.
GRANT SELECT ON registrations, entitlement_claims TO platform_api;

-- Reads: the scoring the weight setter published (weight_sets.api_snapshot carries the
-- admission outcomes too), and its per-hotkey lines.
GRANT SELECT ON weight_sets, score_snapshots TO platform_api;
