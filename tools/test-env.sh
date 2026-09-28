#!/usr/bin/env bash
# A local test environment for the validator suite, no sudo or Docker: uv, .venv, Postgres 17.
#   tools/test-env.sh install      uv, .venv from the pinned requirements, Postgres binaries + data
#   tools/test-env.sh up           start Postgres (port 5433, local connections, no password)
#   tools/test-env.sh test [args]  start Postgres if needed, then pytest (default: -q)
#   tools/test-env.sh down         stop Postgres
#   tools/test-env.sh status       what is installed and whether Postgres is running
# Knobs: TEST_PG_DIR (~/.local/share/floatlib-pg), TEST_PG_PORT (5433), TEST_VENV (.venv),
# UV_INSTALL_DIR (~/.local/bin). Never a validator store: every local user may connect.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
pg="${TEST_PG_DIR:-$HOME/.local/share/floatlib-pg}"
port="${TEST_PG_PORT:-5433}"
venv="${TEST_VENV:-$here/.venv}"
uv_dir="${UV_INSTALL_DIR:-$HOME/.local/bin}"
pg_version=17.11.0
pg_jar="https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-linux-amd64/$pg_version/embedded-postgres-binaries-linux-amd64-$pg_version.jar"
export PATH="$uv_dir:$PATH"
log() { echo "[test-env] $(date -u +%H:%M:%S) $*"; }

running() { [ -d "$pg/data" ] && "$pg/dist/bin/pg_ctl" -D "$pg/data" status >/dev/null 2>&1; }

install() {
  # Each step is skipped when already done, so re-running only fills in what is missing.
  if ! command -v uv >/dev/null 2>&1; then
    log "installing uv into $uv_dir"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$uv_dir" UV_NO_MODIFY_PATH=1 sh
  fi
  [ -x "$venv/bin/python" ] || { log "creating $venv (Python 3.12)"; uv venv -q -p 3.12 "$venv"; }
  log "installing the pinned requirements into $venv"
  uv pip install -q -p "$venv/bin/python" -r "$here/requirements.txt" \
    -r "$here/requirements-service.txt" ruff pip   # pip: setup.sh installs with it
  if [ ! -x "$pg/dist/bin/postgres" ]; then
    log "downloading Postgres $pg_version into $pg/dist"
    mkdir -p "$pg/dist"
    curl -sSfLo "$pg/pg.jar" "$pg_jar"
    # The jar is a zip holding one .txz of the server binaries.
    python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); \
open(sys.argv[2], 'wb').write(z.read('postgres-linux-x86_64.txz'))" "$pg/pg.jar" "$pg/pg.txz"
    tar -xJf "$pg/pg.txz" -C "$pg/dist"
    rm -f "$pg/pg.jar" "$pg/pg.txz"
  fi
  if [ ! -d "$pg/data" ]; then
    log "initialising $pg/data"
    # No locale: the tests need none, and a host without the default one would warn.
    LC_ALL=C "$pg/dist/bin/initdb" -D "$pg/data" -U conjectures --auth=trust -E UTF8 --no-locale \
      >/dev/null
  fi
  log "done - run: tools/test-env.sh test"
}

up() {
  [ -x "$pg/dist/bin/postgres" ] || { log "not installed - run: tools/test-env.sh install"; exit 1; }
  if running; then log "Postgres already running on port $port"; return; fi
  # Unix socket in $pg, not /var/run/postgresql, which needs root.
  "$pg/dist/bin/pg_ctl" -D "$pg/data" -o "-p $port -k $pg -c listen_addresses=localhost" \
    -l "$pg/log" -w start >/dev/null
  log "Postgres running on localhost:$port (log: $pg/log)"
}

down() {
  if running; then "$pg/dist/bin/pg_ctl" -D "$pg/data" -w stop >/dev/null; log "Postgres stopped"
  else log "Postgres not running"; fi
}

status() {
  command -v uv >/dev/null 2>&1 && log "ok       uv $(uv --version | cut -d' ' -f2)" || log "MISSING  uv"
  [ -x "$venv/bin/python" ] && log "ok       $venv" || log "MISSING  $venv"
  [ -x "$pg/dist/bin/postgres" ] && log "ok       Postgres $pg_version in $pg" || log "MISSING  Postgres"
  running && log "running  Postgres on port $port" || log "stopped  Postgres"
}

run_tests() {
  up
  cd "$here"
  [ "$#" -gt 0 ] || set -- -q
  # The suite's own settings, not a validator's .env: a DATABASE_URL would point elsewhere.
  env -u DATABASE_URL POSTGRES_USER=conjectures POSTGRES_PASSWORD=test POSTGRES_HOST=localhost \
    POSTGRES_PORT="$port" "$venv/bin/python" -m pytest "$@"
}

case "${1:-}" in
  install) install ;;
  up) up ;;
  down) down ;;
  status) status ;;
  test) shift; run_tests "$@" ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
