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
Load the remote database credentials for the execute-only role from
`deploy/platform/benchmark_worker.sql`, then run:

```bash
PYTHONPATH=.:validator .venv/bin/python -m workers.benchmark_worker --environment NAME
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
