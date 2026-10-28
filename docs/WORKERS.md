# Competition workers

The main workflow has four roles; the independent auditor is optional:

- `python -m workers.coordinator`: proof verification, immutable plans, aggregation, promotions,
  compact publication. Requires COMPETITION_CONFIG.
- `python -m workers.benchmark_worker --environment local`: claim/heartbeat/complete/fail through
  the execute-only SQL protocol; locally build both sources and collect paired observations.
- `python -m workers.chain_watcher`: registration history and archive timestamp resolution.
- `python -m workers.weight_setter`: published board/rank rewards, chain scheduling, audited attempts.

Set PYTHONPATH=.:validator. PM2 supplies this automatically. See [the manual](../MANUAL.md).

On a benchmark-only host, run `./setup.sh --benchmark-worker`. It installs the local
Lean/native build and sandbox tools plus Python database/timing dependencies, without
the miner CLI, chain SDK, migrations or audit executables. System packages reported as
missing must be installed separately. A working user systemd session and permitted
unprivileged user namespaces are still required; setup checks tool presence, not those
runtime permissions. Use `./setup.sh --benchmark-worker --check` to check installed tools.
Provision an individual login using [the worker access guide](WORKER-DATABASE-ACCESS.md), then run:

```bash
PYTHONPATH=.:validator .venv/bin/python -m workers.benchmark_worker --credentials /secure/benchmark.json --environment NAME
```

Benchmark job mutations go through the SQL protocol installed by the competition migrations.
Leases and retries come from coordinator-created job columns. A worker needs credentials,
environment identity and local execution settings, not another copy of competition policy.

Proof and benchmark identities are separate. Scoring weights and rewards are not measurement inputs.
Evaluations hold scoring instructions; measurement requests hold source hashes, cells, timing,
environment and seed. Reuse links an existing completed job, without copying timings.

Jobs are fenced by owner/token/lease. Coordinator writes are fenced by leader token and a singleton
lock. Finalization checks the current reference and active config and publishes rewards atomically.
Only complete observations reach admission. Reference/infrastructure failures retry with bounded
backoff; a failed board cannot consume another board's place in its own FIFO queue.

## Independent champion auditor

The optional auditor runs separately, preferably on another Linux machine. It discovers all
promotions, fetches immutable sources from the competition database, and tests their outputs
against the pinned specification in a sandbox. Stopping it or finding a mismatch does not change
champions, submission processing or rewards. Findings require operator investigation.

Prepare the toolchain with `./setup.sh --audit-worker`. Provision an auditor login as described
in [the worker access guide](WORKER-DATABASE-ACCESS.md). Its permission role can read sources
and history and write only `audits`, `audit_cursors` and `audit_attempts`. Then run:

```bash
export PYTHONPATH=.:validator
.venv/bin/python -m workers.exact_auditor --credentials /secure/auditor.json --cases 50 --output /var/lib/tensor-audits
```

Use a private database connection, URL-encoded credentials and appropriate TLS settings.
The audit host needs no local database or competition configuration.

Discovery resumes from a durable cursor and includes superseded champions. The same source
promoted on several boards shares one audit per proof version and audit profile. Coverage and
harness hashes define that profile; changing them backfills another audit of historical sources.
The coordinator serializes promotion commits, so promotion IDs define discovery order.

Tasks retain the original proof/benchmark identities and the promotion's immutable specification,
toolchain, dependency and verifier-setting provenance. A newer auditor can run if its specification
identity matches. Tasks without that provenance require the original proof fingerprint.
Incompatible tasks wait and produce age warnings; the worker never installs an arbitrary release.

Claims have expiring leases, heartbeats and attempt tokens. Infrastructure errors retry with
backoff; stale workers cannot overwrite a reclaimed task. Source, request, log, verdict and report
are retained for each attempt. The defaults are:

```text
--cases 50 --seconds 900 --lease 120 --retry 300 --poll 10 --alert-seconds 900
```

`--once` discovers one batch and runs at most one compatible task. `--discover-only` discovers a
batch without execution. Route error and pending-task warnings to your monitoring system.
An active worker stops on source/profile drift. On the coordinator host it also shares the build
lock; use separate hardware to avoid CPU and memory interference.

The auditor rebuilds the source; it does not test the original benchmark executable. It shares
Lean/FloatLib with the reference and complements proof checks and the external specification oracle.
The audit tables are part of the competition schema.

### Operator source restrictions

After investigation, an operator with competition-write credentials can restrict a source across
all boards and publish the reward change atomically:

```bash
PYTHONPATH=.:validator .venv/bin/python -m tools.exact_sources disqualify SOURCE_SHA256 \
  --config competition.toml --actor operator-name --reason 'investigated finding'
PYTHONPATH=.:validator .venv/bin/python -m tools.exact_sources restore SOURCE_SHA256 \
  --config competition.toml --actor operator-name --reason 'root cause resolved'
```

The configuration must match the active coordinator. Restrictions are checked for proof claims,
queue planning, reference selection, finalization and payout. A blocked source cannot delay other
queued entries or become champion on a later-finishing board. Actions are recorded in
`source_disqualifications.history`. Restoration clears the source restriction; it does not rewrite
completed evaluations or promotion-level disqualification history.

## Independent build and measurement budgets

Defaults keep proof verification separate (30 minutes), then give native preparation and
measurement their own clocks:

| Phase | amd64 | ARM64 |
|---|---:|---:|
| Cold preparation: shared modules + reference/candidate binaries | 5 min | 16 min |
| Cached preparation: validate/load both binaries | 2 min | 3 min |
| Overall measurement, including calibration | 20 min | 20 min |
| Single-model-family measurement, including calibration | 6 min | 6 min |

The generated config records the environment's `architecture` (`x86_64`/`amd64` or
`aarch64`/`arm64`). An environment without an architecture uses the conservative ARM64
build defaults. One-model boards default to six minutes; multi-model boards to twenty.
Rounds, inner measurements and cell targets remain 11 / 3 / 50 ms.

Override only the values that need changing:

```toml
[environments.dev-amd64.timing]
build_seconds = 300
cached_build_seconds = 120

[boards.mlp.timing]
benchmark_seconds = 360
```

Environment overrides affect only preparation limits; board overrides affect only measurement
and per-invocation limits. Explicit global `[timing]` values still override the defaults for all
boards/environments, unless a more specific override is supplied. Existing configs with explicit
960/1200-second limits therefore keep them: regenerate/review the policy to adopt these defaults.
Resolved per-environment limits are frozen in evaluation strata and measurement requests.
`run_seconds = 300` caps one invocation, additionally bounded by the measurement time remaining.
No deadline resets per executable, command or round.

A binary cache is keyed by source, benchmark identity and measured execution identity, with a
manifest checking its size, executable permission and SHA-256. Cold preparation publishes these
artifacts atomically. Later family jobs with both valid binaries skip shared/native rebuilding.
The cache is reusable across boards/seeds, not across different hardware identities. Missing or
corrupt artifacts select cold preparation; loss after selecting a cached job is an infrastructure
retry, never a miner rejection. Cache eviction or a changed reference can require cold work again.

The parent freezes the preparation mode for its child while holding the checkout lock and uses
that mode's build allowance + measurement allowance + 60 seconds for its watchdog. A warm
family job therefore has a 9-minute amd64 or 10-minute ARM64 watchdog; a cold overall job has
26 or 37 minutes respectively. These are ceilings, not expected durations.
Explicit phase failures start with `build-timeout` or `benchmark-timeout`; candidate failures
reject the evaluation, reference failures retain the reference/retry behavior, and shared
preparation failures remain infrastructure errors. Hitting the outer `supervisor-timeout`
safety net is an infrastructure error because it cannot reliably identify the responsible side.
Proof-only verification retains its separately configured `verification_seconds` limit.

Successful results record `preparation.mode` and its budget, plus `stage_seconds` for build, calibration, rounds and total measurement;
`timing_seconds` remains the round timing for existing analysis tools. The benchmark fingerprint
covers the execution-budget helper. Deploy identical code on coordinator and workers and
regenerate or review configs using `scripts/exact-config.py`.
Completed evidence remains stored; changing budgets changes immutable plan/request hashes.
Calibrate cold-build headroom on each supported architecture before setting production budgets.
