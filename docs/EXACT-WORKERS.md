# Tensor workers

The replacement workflow has four roles:

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

The only job mutation implementation is the frozen SQL protocol installed by tensor_0001.
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
The audit tables are included in the single fresh migration `tensor_0001`.

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

`[timing]` now requires `build_seconds` and `benchmark_seconds`, replacing `job_seconds`.
Default development settings are 480 seconds (8 minutes) for shared preparation and both
native builds, then a fresh 900 seconds (15 minutes) for calibration and all paired rounds.
`run_seconds = 300` also caps each binary invocation at the smaller of that value and the
measurement time remaining. Neither budget resets per executable, command or round.
A cached binary consumes only its lookup time within the build phase.

The parent supervisor allows build + measurement + 60 seconds (24 minutes by default).
Explicit phase failures start with `build-timeout` or `benchmark-timeout`; candidate failures
reject the evaluation, reference failures retain the reference/retry behavior, and shared
preparation failures remain infrastructure errors. Hitting the outer `supervisor-timeout`
safety net is an infrastructure error because it cannot reliably identify the responsible side.
Proof-only verification retains its separately configured `verification_seconds` limit.

Successful results include `stage_seconds` for build, calibration, rounds and total measurement;
`timing_seconds` remains the round timing for existing analysis tools. The benchmark fingerprint
covers the execution-budget helper. Deploy identical code on coordinator and workers and
regenerate configs using `scripts/exact-config.py`, or update the Ansible timing template.
Completed evidence remains stored; changing budgets changes immutable plan/request hashes.
A fully cold 8 GiB build confirmation remains necessary to validate the default headroom.
