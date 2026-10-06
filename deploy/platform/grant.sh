#!/usr/bin/env bash
# The conjectures platform API's role: create it (needs PLATFORM_API_PASSWORD, which also
# rotates the password) and reset its grants to exactly deploy/platform/platform_api.sql.
# Re-run after every migration. The password travels by environment, never on a command line.
#   set -a; . ./.env; set +a; deploy/platform/grant.sh
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
psql=(docker exec -i -e PLATFORM_API_PASSWORD conjectures_floatlib_db
      psql -v ON_ERROR_STOP=1 -q -U "${POSTGRES_USER:-conjectures}" -d "${POSTGRES_DB:-conjectures}")
if [ -n "${PLATFORM_API_PASSWORD:-}" ]; then
  "${psql[@]}" <<'SQL'
\getenv pw PLATFORM_API_PASSWORD
SELECT format('CREATE ROLE platform_api LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'platform_api') \gexec
SELECT format('ALTER ROLE platform_api LOGIN PASSWORD %L', :'pw') \gexec
SQL
elif ! "${psql[@]}" -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'platform_api'" | grep -q 1; then
  echo "no platform_api role yet: set PLATFORM_API_PASSWORD in .env to create it" >&2
  exit 1
fi
"${psql[@]}" < "$here/deploy/platform/platform_api.sql"
echo "platform_api: grants reset to deploy/platform/platform_api.sql on ${POSTGRES_DB:-conjectures}"
