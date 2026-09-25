#!/usr/bin/env bash
# Runs exactly once, on first cluster initialization, as the superuser. Use it for what
# must exist BEFORE migrations and needs superuser rights: extensions, roles, database
# GUCs. Application tables live in Alembic so they stay versioned - never create one here.
#
# The official postgres entrypoint exports POSTGRES_USER / POSTGRES_DB and a working
# libpq environment, plus anything from .env; the ones psql needs are forwarded with -v
# so :'NAME' substitution works.
set -euo pipefail

: "${MONITOR_PASSWORD:=monitor}"   # fallback when .env does not set one

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v db_name="$POSTGRES_DB" \
     -v monitor_password="$MONITOR_PASSWORD" <<-'EOSQL'

    -- gen_random_uuid(), digest(), crypt().
    CREATE EXTENSION IF NOT EXISTS pgcrypto;

    -- Backing view for query-bottleneck analysis; the library itself is preloaded
    -- via shared_preload_libraries in postgresql.conf.
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

    -- Per-database defaults applied to every connection.
    ALTER DATABASE :"db_name" SET timezone TO 'UTC';
    -- The gate runs out of process, so no statement here is long: a query over this
    -- timeout is a bug or a lock pile-up, and failing fast beats holding the pool.
    ALTER DATABASE :"db_name" SET statement_timeout TO '30s';
    ALTER DATABASE :"db_name" SET idle_in_transaction_session_timeout TO '60s';
    ALTER DATABASE :"db_name" SET lock_timeout TO '10s';

    -- Read-only monitoring role: lets dashboards and on-call read pg_stat_* without
    -- any write access. (psql does not substitute :'vars' inside DO $$..$$, so the
    -- statement is built in plain SQL and run with \gexec - idempotent.)
    SELECT format('CREATE ROLE monitor LOGIN PASSWORD %L', :'monitor_password')
    WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'monitor')
    \gexec

    GRANT pg_monitor TO monitor;
    GRANT CONNECT ON DATABASE :"db_name" TO monitor;
    GRANT USAGE ON SCHEMA public TO monitor;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO monitor;

EOSQL

echo "00_init.sh: extensions, database settings and monitor role configured."
