# Exact-runtime workers: local implementation

The optional workflow supports one complete, proved submission on independent model-family boards
and an overall board. It implements the configuration, proof, job, scoring, recovery and reward
layers. Central API intake/read changes and production rollout are separate work.

The existing FloatLib worker and reward policy remain the default. No new processes start merely
because the code or migration is installed. No chain transaction is sent by the rehearsal command.

## Components

| Component | Command/module | Responsibility |
|---|---|---|
| Coordinator | `workers.exact_coordinator` | Own config, verify sources, plan jobs, aggregate, promote and audit champions |
| Hardware worker | `workers.benchmark_worker` | Claim leased jobs, build locally, benchmark pairs, save observations |
| Benchmark subprocess | `exact.runner` | Sandbox builds and timing; report actual execution provenance |
| Weight setter | existing `workers.weight_setter` | Consume published reward policy when explicitly selected |

Use the same worker code on every architecture. Each environment identifies an actual CPU,
compiler/toolchain and execution policy. A label alone is insufficient. Both sides of a comparison
run locally; only ratios are combined across environments.

## 1. Prepare an isolated database and toolchain

Run the normal `./setup.sh` preparation on the benchmark host. User namespaces, bubblewrap with
overlay support and user systemd resource limits must work. Builds and benchmarks retain the gate
sandbox. Do not disable protections to process untrusted submissions.

Set `DATABASE_URL` to a **development** database using the SQLAlchemy psycopg form. Apply migrations:

```bash
(cd deploy/migrate && ../../.venv/bin/python -m alembic upgrade head)
```

The new revision is `0018`. Legacy records and promotion semantics are preserved. Downgrade refuses
while exact submissions exist; archive/remove disposable entries explicitly before rolling it back.

## 2. Generate and review development settings

```bash
PYTHONPATH=.:validator .venv/bin/python scripts/exact-config.py \
  --quick --output /tmp/exact-development.toml
```

This generates six boards, the shipped complete reference, one local environment, an illustrative
80/20 rank split, and illustrative board allocations. `--quick` uses five rounds and 2 ms cells;
these settings test plumbing and are unsuitable for choosing production promotion thresholds.
Without `--quick`, the provisional settings are 25 rounds and approximately 100 ms cells.

The file owns board workloads, environment weights, seeds, timing, admission and rewards. A board
may override required hardware weights with a `[boards.<name>.environments]` table. Scoring and
payout weights are independent. `seed_mode = 'fixed'` uses the explicit seed list for reproducible
development experiments. `fresh` draws a new seed for each list entry and each new board evaluation,
shared across its hardware jobs. The resolved seeds are stored, retained across compatible plan
replacements, and reused on retries. Non-quick generated settings select `fresh`; quick settings
select `fixed`. Review workload diversity and sampling for an actual competition.

Inspect local identities with:

```bash
PYTHONPATH=.:validator .venv/bin/python -m exact.runner --identity
```

Configure other environments with identities reported on those machines. Workers require matching
pinned source files. Changes to verification/benchmark code require reviewed identity updates and
a coordinator restart. Configuration uses explicit source hashes; initial references are not paid
miner promotions.

## 3. Run the coordinator

```bash
PYTHONPATH=.:validator .venv/bin/python -m workers.exact_coordinator \
  --config /tmp/exact-development.toml
```

Alternatively set `EXACT_COMPETITION_CONFIG`. There is one active coordinator, enforced by an
advisory leadership lock and a persisted fencing token. Proof verification runs separately from
its polling loop. `--once` reconciles, plans and aggregates once; it does not launch verification.

An exact submission uses `submissions.slot = 'exact'` and a `Kernel.lean` row in `submission_files`.
The legacy gate worker excludes these entries. The central API is not yet wired to accept them;
use the disposable rehearsal below rather than inserting test entries into a production database.

## 4. Run hardware workers

For an operator-owned local development database:

```bash
PYTHONPATH=.:validator .venv/bin/python -m workers.benchmark_worker --environment local
```

Production-style hardware workers should connect through a separate login role named
`exact_benchmark_worker`. An administrator creates the role/credential and applies
`deploy/platform/exact_benchmark_worker.sql` in the competition database. Keep the coordinator
and weight setter on their appropriate operator credentials.

The worker role receives execute access to fenced job/source functions, without table-update,
promotion or reward-policy privileges. Reapply its grants after schema changes. Credentials are
never embedded in jobs/results. Restrict database network access separately.

One shared host lock serializes timed jobs across worker processes. Its default is
`/tmp/floatlib-exact-benchmark.lock`; workers under different accounts must share a suitably
permissioned operator-owned lock path using `--host-lock`. The timed subprocess inherits the lock,
so an abruptly killed supervisor does not immediately allow overlapping timing. Each checkout also
serializes its shared Lake/sandbox workspace. Separate checkouts still need the same host lock.

Lease duration comes from the planned job; the worker heartbeat renews it while work runs. Jobs
also carry the coordinator retry period. Worker CLI defaults provide the initial claim/expiry
recovery interval. Expired or cancelled attempts cannot complete a job with an old token.

## 5. Configuration changes and failures

Restart the coordinator after editing its file. It atomically supersedes affected pending plans,
cancels obsolete attempts and publishes the updated reward policy. Exact-compatible completed
measurements can be reused by reference; raw results are not copied into each decision/publication.
Completed historic decisions are retained. Existing champions stay references under new scoring;
their old scores do not qualify a new challenger.

New boards do not automatically replay all previously decided submissions. Choose their initial
reference explicitly. Existing imported references receive no manufactured promotion or payout.
Removing a board removes its future allocation; normalize the remaining configured weights.

Reference build/benchmark failures pause that comparison and retry with backoff. Other boards
continue; its original challenger remains first. Candidate-specific benchmark failures complete that
board evaluation without promotion. Infrastructure/checksum failures do not reject the miner.
Unclaimed, expired and repeatedly failing jobs produce coordinator warnings. A maintainer can
repair the machine/reference and let the same job retry. No extra miner upload or registration
charge is introduced by recovery.

## 6. Champion audit

A proof shows an entry meets the specification; the audit checks the built entry against it as
well, on random and edge-case inputs of every kernel (`gate.py --slot exact --audit`, which runs
`exactAudit --json` in the sandbox). After each publication the coordinator queues an audit of
every board's two current champions' sources, once per audit version: a digest of
`lean/audit/ExactAudit*.lean` and the proof identity, so a stronger audit re-audits them. A queued
audit runs when no proof check waits, with a fresh seed and the `[audit]` settings:

```toml
[audit]
policy = 'alert'   # what a failure does: alert, disqualify or hold
cases = 50         # cases per kernel and input kind
seconds = 900      # the audit's time budget
```

| Policy | A failed audit | An unaudited champion |
|---|---|---|
| `alert` | logs an error; the champion stays paid | paid |
| `disqualify` | disqualifies every promotion of the source; the earlier champions are paid | paid |
| `hold` | as `disqualify` | not paid until one of its audits is clean; the earlier clean champions are |

Only a mismatch the audit reports fails a champion. Any other outcome (a sandbox or build error, a
timeout) is an error, retried with backoff, and never disqualifies. Each king in the publication
carries its source's audit state: `pending`, `clean` or `failed`. A coordinator stopped mid-audit
runs it again. Every outcome, with its seed and first mismatches, is in `exact_audits`.

## 7. Rewards: opt in only after review

The coordinator publishes compact board/rank/pause metadata atomically with promotions. The
weight setter reads that publication, without bootstrapping statistics or scanning raw timings.
It keeps using the last valid publication if the coordinator is temporarily offline.

Select it explicitly with `WEIGHT_SCORING_SOURCE=exact`; the default remains `legacy`. Keep
`WEIGHT_DRY_RUN=1` during development. Never run competing live weight setters for the same
wallet/subnet. Central API/frontend integration is still needed before a public rollout.

Per eligible rank, its fraction of validator weight is:

```text
competition_share × board_reward_weight × rank_share
```

The current and previous eligible promotion are paid. Contributions accumulate per hotkey;
vacancies and chain-ineligible hotkeys route to treasury without reallocating their shares.
`paused_policy = 'treasury'` withholds the whole affected board allocation; `preserve` retains it.
Disqualification is `exact_promotions.disqualified_at`, separate from ordinary supersession.

## 8. Reproduce a real local rehearsal

Use a database administrator URL with `CREATE DATABASE` permission on a development server:

```bash
PYTHONPATH=.:validator .venv/bin/python scripts/test-exact-workflow.py \
  --admin-url "$DEV_POSTGRES_ADMIN_URL" \
  --output data/exact-workflow/my-rehearsal
```

The command creates and migrates a uniquely named scratch database, queues a copy of the complete
incumbent, runs real proof checks and six board comparisons, audits any new champion, and computes a test
reward vector.
It saves source/config, logs and raw results, terminates its workers, restores the previous shared
submission source, and drops the scratch database. It never sends chain weights. Use `--candidate`
for another complete entry and `--minutes` for an overall deadline.

Short copy comparisons can produce misleading speed results on a noisy laptop. This is an
end-to-end plumbing test, not evidence that production admission thresholds are safe. Review the
saved decisions instead of treating a smoke-test takeover as a genuine optimization.

For PostgreSQL protocol/statistics/reward tests:

```bash
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=<dev-port> POSTGRES_PASSWORD=<dev-password> \
  .venv/bin/pytest -q tests/test_exact_competition.py tests/test_exact_audit.py tests/test_db_schema.py
```

Tests create/drop scratch databases. They cover claims, stale tokens, restricted worker grants,
independent promotions, configuration reconciliation, fairness and reward-vector integration.
Database tests skip when the server is unavailable; check the reported counts before relying on
results. Additional hardware deployment, calibration and central API work remain later stages.
