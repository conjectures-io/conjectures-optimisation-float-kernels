# Run a competition instance

This guide is for operators. Miners only need the [manual](../MANUAL.md) and public API.
The service consists of a coordinator, benchmark workers, chain watcher and weight setter;
an independent auditor is optional. The central HTTP API lives in `conjectures-validator`.

## Setup and database

On Linux, install the OS prerequisites reported by setup, then:

```bash
./setup.sh --service
# Edit .env: PostgreSQL credentials, competition config and local execution limits.
docker compose up -d db
docker compose build migrate
docker compose run --rm migrate
```

The migration owner needs privileges to create the schema and worker permission groups.
Apply the repository's migration head to a dedicated competition database. Host processes use
the published database port; containers on its network use `conjectures_tensor_db:5432`.
`DATABASE_URL` overrides the separate PostgreSQL settings; percent-encode passwords in URLs.

Use `./setup.sh --chain` when running the chain watcher or weight setter, `--benchmark-worker`
on a remote timing host, and `--audit-worker` on an auditor. See [worker access](WORKER-DATABASE-ACCESS.md)
for restricted credentials; keep administrator credentials off worker machines.

## Configure and start

```bash
export PYTHONPATH=.:validator
.venv/bin/python scripts/exact-config.py --output competition.toml
pm2 start deploy/pm2.config.js --only tensor-coordinator,tensor-benchmark-worker
```

Set `COMPETITION_CONFIG` in `.env` to the chosen file. PM2 loads `.env`; direct Python commands
need those environment values exported separately. The generator refuses to overwrite a file.
It creates a **development policy** measured for the current checkout/host:

| Setting | Generated default |
|---|---|
| Root seeds | One fresh seed per evaluation, six generated workloads per seed |
| Paired / inner rounds | 11 / 3 |
| Cell calibration | 50 ms |
| Build / measurement budgets | 960 s / 1200 s |
| Per-binary invocation cap | 300 s |
| Promotion / guard | 0.97 / 1.10 |
| Paid ranks | Current / previous, 80/20 |
| Board allocation | Overall 50%; each model family 10% |

`--quick` uses a fixed seed, five rounds and 2 ms cells for workflow smoke tests. Neither mode
substitutes for a production timing study. Review allocations, thresholds, proof budget and CPU
identities before use. Every configured environment must have a matching authorized worker.

The coordinator owns policy; workers receive immutable jobs and do not load the policy file.
Only one coordinator leads. Each host serializes benchmark execution. See [workers](WORKERS.md)
for leases, caching, retries and failure behavior.

## Public API connection

Provision the dedicated API account and apply its grants from `deploy/platform/grant.sh` using
administrative credentials. Configure the central API's TorchLean database URL and enable its
TorchLean routes/uploads separately. Do not give the API coordinator or benchmark-result privileges.

**Compatibility must match the central API release.** Its current TorchLean intake expects
`intake_mode` and nullable registration IDs for explicit development uploads, and its dashboard
needs public coordinator configuration and source-restriction reads. Migration `tensor_0002`
and the current `grant.sh` provide them: apply both before enabling that central API; a
successful database connection alone is insufficient.
See [the database contract](../deploy/platform/CONTRACT.md) for the integration boundary.

Registration bypass belongs only to a development API and is not a miner-controlled upload field.
Production must enforce the current-registration and cutoff policy. The coordinator evaluates
dev uploads only when its config has `dev_intake = true` (`scripts/exact-config.py --dev-intake`);
generate production configs without it.

## Chain and rewards

Configure the subnet, network, wallet and treasury in `.env`. Start the watcher:

```bash
pm2 start deploy/pm2.config.js --only tensor-chain-watcher
```

Inspect registration freshness and the coordinator's publication. Before enabling the setter,
stop any other setter using the same validator wallet:

```bash
pm2 start deploy/pm2.config.js --only tensor-weight-setter
```

Rehearse with `WEIGHT_DRY_RUN=1`; use `0` only for actual publication. After `.env` changes:

```bash
pm2 startOrReload deploy/pm2.config.js --update-env
pm2 save
```

The setter applies the configured competition allocation to the published board/rank vector.
Missing or ineligible recipients go to treasury. Audits are report-only; they do not automatically
hold rewards or demote a champion.

## Monitoring and changes

Use `pm2 status` and `pm2 logs tensor-coordinator --lines 50`, or the corresponding process name.
Watch proof failures, retry counts, stale job leases, queued-job age and chain publication errors.
Logs saying a benchmark is waiting do not by themselves imply failure; check its lease and progress.

After source/contract/benchmark changes, rebuild dependencies and regenerate the affected identities
and reference hashes in policy. Deploy matching proof/benchmark code on participating hosts.
Changing a board replans its pending evaluation; completed compatible evidence can be reused.
A newly activated or re-added board has a new upload boundary. Existing champions are not replaced
merely by editing the shipped starter file.

For source disqualification/restoration and independent audits, use the commands in
[WORKERS.md](WORKERS.md). For a disposable full-worker test, use
[the local rehearsal](LOCAL-EXPERIMENTS.md#full-local-rehearsal).

## Events

With `AXIOM_TOKEN` and `AXIOM_DATASET` set, each entry point sends structured events to the
platform's Axiom dataset (`validator/axiom.py`), every one tagged `competition: "torchlean"`.
Without them nothing is sent. ERROR logs and uncaught exceptions arrive as `log_error`.

| source | events |
|---|---|
| `competition-coordinator` | `proof_checked` (proved, rejected, or retry when the infrastructure failed), `evaluation_planned`, `evaluation_decided`, `evaluation_quarantined` (unreadable results; never the miner's fault), `champion_promoted`, `rewards_published`, `board_paused`/`board_resumed`, `benchmark_stalled`; from the operator tools, `promotion_disqualified` and `source_disqualification_set` |
| `competition-benchmark-worker` | `benchmark_job_finished` per job, tagged with the worker's environment; a host needs the token to report |
| `competition-chain-watcher` | `registrations_recorded`, `chain_read_failed` |
| `competition-weight-setter` | `weights_planned`/`weights_set`/`weights_skipped`/`weights_failed` |

Each has `service_started`, `service_stopped` and `service_misconfigured`.
The coordinator, benchmark worker and auditor also send heartbeats and `service_failed` on
abnormal exit; their stop event records `outcome`. Other entry points record a stop `reason`.
Warnings mark the miner's failures (a rejected proof, a crashing candidate); errors mark ours.
The `torchlean-audit-worker` source reports audit outcomes and backlog warnings. Instrumenting
its source changes its audit identity, so deployment backfills historical champions. See
[Axiom monitoring](AXIOM-MONITORING.md) for event severities and monitor setup.
