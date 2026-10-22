# Remote worker database access

The coordinator defines required environments and their expected hardware identities in its
competition configuration. Administrator-managed database assignments authorize each login to
serve particular environment names. Assignments do not change the scoring configuration.

## Database deployment

Deploy a fresh tensor database and run `tensor_0001` with an administrator connection capable
of creating roles and granting privileges. This fresh initial migration includes the new protocol;
it does not upgrade a previously initialized tensor_0001 database. Recreate disposable dev
databases before using this version.

The migration creates two cluster-wide NOLOGIN permission groups:

- `tensor_benchmark_access`: execute-only benchmark protocol, no table access.
- `tensor_auditor_access`: source/history reads and audit-table writes; no competition mutations.

The groups contain no passwords. Migrations preserve them on downgrade because PostgreSQL roles
are cluster-wide. Separate competition instances should use separate login accounts. The central
API has its own role and provisioning; do not give it either worker role.

## Provision a machine

On the administrator machine, set `WORKER_ADMIN_DATABASE_URL` through protected configuration.
It must point to the competition database, not the maintenance `postgres` database. Do not place
administrator credentials on worker hosts. The CLI requires the service Python dependencies.

```bash
export PYTHONPATH=.:validator
.venv/bin/python -m tools.worker_access create --kind benchmark --login bench_dev \
  --environment dev-amd64 --host 127.0.0.1 --port 15435 \
  --output /secure/bench-dev.json
.venv/bin/python -m tools.worker_access create --kind auditor --login audit_dev \
  --host 127.0.0.1 --port 15435 --output /secure/audit-dev.json
```

`--host` and `--port` describe the endpoint on the worker, which can differ from the administrator's
endpoint. The example assumes an SSH tunnel. For direct private-network access use verified TLS,
for example `--sslmode verify-full`, and configure the appropriate trusted CA on the worker.
The CLI preserves database URL connection options unless explicitly overridden.

Each output is a JSON file with DATABASE_URL and, for a single benchmark assignment,
BENCHMARK_ENVIRONMENT. Files are created with mode 0600 and never overwrite an existing path.
Transfer each file privately to its worker. Give each machine an individual login. Benchmark
results are trusted worker assertions, not hardware-attested measurements.

## Prepare and check the worker

Native macOS benchmark workers currently fail closed. `./setup-macos.sh --benchmark-worker`,
preflight, worker startup and subprocess execution refuse before running candidate code.
`GATE_SANDBOX=0` cannot override this. See [macOS sandbox evidence](MACOS-WORKER-SANDBOX.md).
The setup and launch instructions below apply to Linux.

Keep PostgreSQL bound to loopback as provided by Compose. Use a private connection or a tunnel
such as `ssh -N -L 15435:127.0.0.1:5435 DATABASE_HOST`; restrict and manage SSH access separately.

```bash
./setup.sh --benchmark-worker
export PYTHONPATH=.:validator
.venv/bin/python -m tools.worker_preflight --credentials /secure/bench-dev.json \
  --environment dev-amd64
```

Preflight checks database authorization, native build tools, user namespaces, resource-limit
support, CPU affinity and writable output directories without claiming work. It prints the machine,
proof and benchmark identities. Place the machine identity under the matching environment name in
the coordinator configuration. Use `--expected-identity HASH` to check an already configured value.
Worker-local execution settings such as BENCH_CPU must match those used during preflight.

```bash
.venv/bin/python -m workers.benchmark_worker --credentials /secure/bench-dev.json
```

The slim setup still requires Lean and native compilation. Multiple logins can serve the same
environment; each must match its expected identity. For a login assigned several environments,
select one explicitly with `--environment`. Use a separate checkout for an independent execution
slot; workers sharing a checkout serialize through its build lock.

For the independent auditor:

```bash
./setup.sh --audit-worker
.venv/bin/python -m tools.worker_preflight --kind auditor --credentials /secure/audit-dev.json
.venv/bin/python -m workers.exact_auditor --credentials /secure/audit-dev.json \
  --output /var/lib/tensor-audits
```

PM2 supports `TENSOR_BENCHMARK_CREDENTIALS` and an optional auditor enabled with
`TENSOR_AUDITOR_ENABLED=1` plus `TENSOR_AUDITOR_CREDENTIALS`. Credential files override the
database URL inherited by those worker processes. The coordinator keeps its own connection.

## Maintain access

Run these on the administrator machine:

```bash
.venv/bin/python -m tools.worker_access list
.venv/bin/python -m tools.worker_access environments --login bench_dev --environment dev-amd64
.venv/bin/python -m tools.worker_access rotate --login bench_dev \
  --host 127.0.0.1 --port 15435 --output /secure/bench-dev-next.json
.venv/bin/python -m tools.worker_access revoke --login bench_dev
```

`environments` replaces the entire assignment list; repeat `--environment` to authorize several
names. An empty list withdraws benchmark access. Environment names must also exist in coordinator
configuration before jobs are created for them.

Rotation changes the password and terminates existing sessions. Distribute the new file and restart
the worker. Revocation disables authorization and login, removes its permission-group membership
and terminates sessions. Benchmark leases recover through normal expiry/retry. Auditor session
termination is required because its audit writes use table grants. Revoked accounts cannot be
reactivated through rotation; provision a new account.

Database commits and credential-file creation are separate operations. If file publication fails,
inspect `list` and rotate the account into a new file. The CLI never prints passwords or raw database
connection errors. Protect the administrator's environment and PostgreSQL logs as usual.

## Protocol guarantees

Claims are authorized using the authenticated PostgreSQL `session_user`, even inside SECURITY
DEFINER functions. A job records its authenticated claimant separately from its process owner.
Sources, heartbeat and result writes require that login, assigned environment, owner, attempt token
and a live lease. Knowing another worker's job ID or token does not grant access. Disabling an
assignment prevents further benchmark operations immediately after the administrative commit.

Superusers retain administrative recovery access. Worker logins have no superuser, database/role
creation, replication or RLS-bypass attributes. Independent audits remain report-only and never
block submission processing or publication. Central API availability is not part of this protocol.
