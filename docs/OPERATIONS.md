# Operating the competition

The central `conjectures-validator` API handles uploads and public reads. This
repository runs the competition's PostgreSQL database and three host workers.
It does not serve a standalone API.

| Worker | Responsibility |
| --- | --- |
| `floatlib-gate-worker` | Claim queued entries, verify and benchmark, store verdicts, promote winners |
| `floatlib-chain-watcher` | Record subnet registrations and their on-chain timestamps |
| `floatlib-weight-setter` | Publish scoring snapshots and submit chain weights |

## Host and worker account

Use one account to own the checkout, run setup, and manage PM2. Install the
packages listed in the [miner guide](../MANUAL.md#1-set-up), Docker with Compose,
and PM2. Docker access is needed for database administration; host gate workers
connect to PostgreSQL directly and do not need Docker to verify entries.

```bash
./setup.sh --chain
```

`--service` adds database/worker dependencies and creates `.env` if missing.
`--chain` also installs the Bittensor SDK. Plain setup includes miner dependencies.

The gate requires the worker account's systemd user manager for resource limits.
An administrator can enable lingering:

```bash
sudo loginctl enable-linger <worker-user>
```

In that user's worker-launch shell:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user is-system-running
```

Setup builds pinned bubblewrap in `.venv/bin`; the gate selects that binary
without requiring venv activation. If user namespaces are blocked, inspect the
host's AppArmor/user-namespace policy. Do not disable sandboxing on a validator.

## Configure and migrate the database

Edit `.env` using [.env.example](../.env.example). Set a real database password,
the database name and host port, chain network/subnet, benchmark CPU, and wallet
settings. `DATABASE_URL`, if set, overrides the individual database settings.

Use a distinct host port when other databases run on the same host. For example,
workers can use `localhost:5434`, while API containers use
`conjectures_floatlib_db:5432` on the shared Docker network.

For central API access, set:

```dotenv
COMPOSE_FILE=compose.yaml:deploy/platform/compose.yaml
PLATFORM_NETWORK=conjectures-api_default
PLATFORM_API_PASSWORD=<dedicated-platform-role-password>
```

The external network must already exist. Apply migrations from the current
checkout and refresh grants, using an account with Docker access:

```bash
docker compose up -d db
docker compose run --build --rm migrate

bash -c '
  set -e
  set -a
  . ./.env
  set +a
  deploy/platform/grant.sh
'
```

`grant.sh` creates or rotates `platform_api` using `PLATFORM_API_PASSWORD`, then
applies [platform_api.sql](../deploy/platform/platform_api.sql). This role can
queue submissions, update rate-limit counters, and read scoring/history. It
cannot publish verdicts or promote champions. Reapply grants after migrations.

## Configure the central API

In the central repository's `.env`, preserve other database connections and add:

```dotenv
COMPETITIONS_ENABLED=1
COMPETITIONS=deflate,floatlib
COMPETITION_FLOATLIB_DATABASE_URL=postgresql+psycopg://platform_api:<url-encoded-platform-password>@conjectures_floatlib_db:5432/floatlib
FLOATLIB_SUBMISSIONS_ENABLED=0
FLOATLIB_REGISTRATION_CUTOFF=<ISO-8601-UTC-timestamp>
FLOATLIB_CHECK_DEFLATE_USAGE=0
```

Use the role's `PLATFORM_API_PASSWORD`, not the database owner's
`POSTGRES_PASSWORD`. URL-encode reserved password characters in the URL; leave
`PLATFORM_API_PASSWORD` itself unchanged.

Open uploads after connectivity and worker checks by setting
`FLOATLIB_SUBMISSIONS_ENABLED=1`. If enabling `FLOATLIB_CHECK_DEFLATE_USAGE=1`,
DEFLATE must also be configured and its uploads disabled with
`DEFLATE_SUBMISSIONS_ENABLED=0`. The usage check replaces the timestamp cutoff
check; database connection failures fall back to the cutoff.

Build/recreate the central API using that repository's deployment instructions.
Run Compose from a fresh shell so exported FloatLib database variables do not
override the central `.env`. The competition has no API container to restart.

Check `/readyz` and `/v1/competitions/floatlib/health`. Published policy fields can
be null until the first weight-setter publication. Empty champions/rankings are
normal before the first promotion. See the
[central API reference](https://github.com/conjectures-io/conjectures-validator/blob/main/docs/COMPETITION_API.md).

## Start workers and switch rewards

Keep `WEIGHT_DRY_RUN=1` during preparation. Start only the gate and watcher while
another competition's setter owns live weights:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
pm2 startOrReload deploy/pm2.config.js \
  --only floatlib-gate-worker,floatlib-chain-watcher --update-env
pm2 save
```

The PM2 config reads `.env`; exported shell variables override it. Explicitly
reload through the config after changing settings. Check the startup logs to
confirm effective values.

When switching from DEFLATE, set `WEIGHT_DRY_RUN=0` in FloatLib `.env`, then:

```bash
pm2 stop deflate-weight-setter
WEIGHT_DRY_RUN=0 pm2 startOrReload deploy/pm2.config.js \
  --only floatlib-weight-setter --update-env
pm2 save
pm2 logs floatlib-weight-setter --lines 50 --nostream
```

Only one live setter may publish for the validator hotkey. Stopping the old
setter does not replace on-chain weights immediately; check the next successful
permitted update. With no eligible champions, FloatLib sends the unpaid share to
treasury. Closing DEFLATE uploads does not stop its gate or finish its queue.

The watcher needs both a live node and an archive for historical registration
dates. A failed archive lookup can prevent a changed registration batch from
being stored; the watcher retries. Check logs for successful updates.

## Promotion and reference failures

An accepted entry meeting the promotion bar and paired statistical test becomes
champion in the same transaction as its verdict. The worker stages that
champion's source for later gates. Measurements identify the reference source
with `champion_sha256`; the default leaderboard shows comparisons against the
current champion. Historical boards can be selected through the central API.

A champion or shipped reference that cannot build or benchmark pauses its slot.
The challenger remains first in that slot and normal verification is retried
after `CHAMPION_RETRY_SECONDS` (default 300). Other slots receive turns between
retries, although gate execution itself is serial. Recovery does not require a
separate probe or automatically demote the champion.

`WEIGHT_PAUSED_SLOT_POLICY=preserve` keeps rewards. With `treasury`, the paused
slot's current champion share is withheld; the previous paid champions retain
theirs. The change applies at publication and the next permitted chain update.

Run tools as the worker user, with its database environment loaded:

```bash
set -a; . ./.env; set +a
.venv/bin/python validator/tools/slots.py status
.venv/bin/python validator/tools/slots.py retry --slot full
.venv/bin/python validator/tools/slots.py demote \
  --slot full --submission-id <current-id> --reason "operator audit"
```

Mutations use the host gate lock and refuse while it is busy. Demotion requires
the current champion's exact ID. The next attempt uses the remaining champion
or shipped reference. Run gate workers on one host per shared queue; the host
lock is not a distributed lock.

## Provenance and explicit reverification

Verifier fingerprints and incumbent binary hashes record provenance. Changes are
logged and do not automatically retire verdicts or trigger reverification.
A reference source change still changes which comparisons appear on its board.

After an operator has identified affected results:

```bash
.venv/bin/python fingerprint.py
.venv/bin/python validator/tools/reverify.py
.venv/bin/python validator/tools/reverify.py --apply
```

The last command requeues accepted entries from another verifier; inspect its
selection before applying it. Requeued entries retain their registration and
are temporarily off the accepted board while processing. This is an operator
repair tool, not a prerequisite for deploying every code change.

## Storage and observability

Unchanged scoring polls reuse the latest publication, including after restart.
Relevant policy, scoring, submission, vector, and mode changes create a new one;
actual chain submission attempts have their own history. This avoids storing an
expanded JSON payload every polling interval.

The setter clears expired `api_snapshot` payloads in batches and retains the
latest valid snapshot per subnet. Defaults: one-hour retention, a cleanup check
every 60 seconds, and batches of 10. Configure the three
`API_SNAPSHOT_*` settings in `.env.example`. Weight-set rows and score history
remain. Clients must restart pagination after an expired snapshot.

Worker logs report failures and recovery. Optional `AXIOM_TOKEN` and
`AXIOM_DATASET` enable structured events; see [axiom.py](../validator/axiom.py).
Repeated identical pause reasons are debug-level in console logs, but failed
attempts also emit `slot_paused` events when observability is configured.

## Development tests

Use a dedicated development PostgreSQL server, with an account allowed to create
scratch databases. Test fixtures create and remove scratch databases; do not
point the test environment at production.

```bash
.venv/bin/pip install -r requirements/test.txt ruff==0.16.9
.venv/bin/python -m pytest -q -m "not slow" -rs
.venv/bin/python selftest.py
```

Tests read the `POSTGRES_*` connection settings. Inspect skip reasons if no test
server is available. The real self-test can take substantial time and contains
hardware-sensitive timing assertions.

For a local PostgreSQL instance without Docker, use
`bash validator/tools/test-env.sh test`. That helper installs development tools
and stores its PostgreSQL files under the user's local data directory.
