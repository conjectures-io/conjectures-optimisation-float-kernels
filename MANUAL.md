# Tensor competition manual

## Environment

Run `./setup.sh --service`; use `--chain` when running the chain watcher or weight setter.
Setup installs miner dependencies too (`requirements/miner.txt`).
Use `./setup.sh --benchmark-worker` on a timing host: it installs only the Python worker
dependencies and builds `VerifyExact` and `benchExact`, with the same pinned Lean packages
and sandbox. It does not require Docker, PM2 or chain dependencies.
Use `./setup.sh --dev` for the full build plus all Python dependencies, including pytest,
SQLAlchemy, the chain SDK, Ruff and plotting libraries. Select `.venv/bin/python` in your IDE.
Activate `.venv` or call its Python explicitly. Set `PYTHONPATH=.:validator` from the project root.
PM2 and Compose read `.env`; export its DB/config values before launching Python workers directly.

Bubblewrap is installed in `.venv/bin/bwrap`. Builds and timings run without network access,
with read-only operator code, a private writable build overlay and resource limits.
Ubuntu AppArmor may require a profile permitting user namespaces for that executable.
Check `./setup.sh --check` and run a real verification before enabling workers.

## Submit a kernel implementation

For the client alone, install `requirements/miner.txt` in a Python virtual environment.
No Lean installation or chain connection is needed to upload:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements/miner.txt
.venv/bin/python miner/submit.py submit path/to/entry \
  --hotkey ~/.bittensor/wallets/WALLET/hotkeys/HOTKEY
.venv/bin/python miner/submit.py status 123
```

The entry directory must contain one nonempty UTF-8 `Kernel.lean` (at most 256000 bytes).
The client signs the file for `torchlean`, uploads it to the central API, and prints its ID.
The default API origin is `https://conjectures.io`. Use `--url` on either command to select dev:

```bash
.venv/bin/python miner/submit.py submit kernels/exact/incumbent \
  --hotkey //Alice --url http://127.0.0.1:8000
.venv/bin/python miner/submit.py status 123 --url http://127.0.0.1:8000
```

`//Alice` is a public test identity. Dev uploads use the normal signed format; the dev API
must enable `TORCHLEAN_DEV_SKIP_REGISTRATION=1` with `APP_MODE=DEV` to accept unregistered
identities. There is no client flag to bypass registration. Duplicate uploads are still refused.

Status returns the proof state and recorded board evaluations. A proved submission may still
be waiting for benchmarks; it does not by itself mean promotion. The client currently exposes
`submit` and `status`; board and detailed report commands await their API endpoints.
These commands require the TorchLean API changes to be deployed and intake enabled.

## Fresh database

Configure the PostgreSQL values in `.env`, then:

```bash
docker compose up -d db
docker compose build migrate
docker compose run --rm migrate
```

The initial revision is `tensor_0001`. This schema replaces the old competition schema;
use a new database. Subsequent deployed changes should use incremental migrations.
Host workers use the published loopback port; containers on the database's Docker network use
`conjectures_tensor_db:5432`.

The central API requires coordinated changes described in
[the DB contract](deploy/platform/CONTRACT.md).
Apply its grants using `deploy/platform/grant.sh` after loading the relevant environment values.

## Coordinator configuration

```bash
.venv/bin/python scripts/exact-config.py --quick --output competition.toml
```

Quick mode is a workflow check: five rounds and 2 ms cells. It is not production calibration.
Without `--quick`, the generator uses 25 rounds, 100 ms cells and one fresh seed.
Review model, hardware and reward allocations and initial reference hashes.

`COMPETITION_CONFIG` names this TOML file. The coordinator owns configuration.
It stores resolved immutable plans in the DB; workers do not load the config file.
A new board starts with uploads received after activation. Removing/re-adding a board establishes
a new intake boundary. Changing an active board replans its pending challenger in original order.
Fresh seeds remain fixed across retries of that challenger.

## Processes

```bash
pm2 start deploy/pm2.config.js --only tensor-coordinator,tensor-benchmark-worker
pm2 start deploy/pm2.config.js --only tensor-chain-watcher
```

Only one coordinator leads. Multiple benchmark workers can run on different hosts using the same
code and an environment name matching the coordinator config. Each host serializes timings.
Configure an execute-only DB role with `deploy/platform/benchmark_worker.sql`.
Architecture-local builds are cached by source, benchmark identity and machine identity.

Proof failures reject an entry. Infrastructure failures retry the same entry and retain its spent
registration. Reference/checksum failures pause the affected comparison; other boards can proceed.
The reward pause policy either preserves the existing ranks or directs that board's share to treasury.
No automatic demotion occurs.

For operator disqualification while the coordinator is stopped:

```bash
.venv/bin/python -m tools.boards --config competition.toml disqualify 12 --reason 'audit failure'
```

A trial verification writes a report and does not change DB eligibility or rewards:

```bash
.venv/bin/python -m verification my-entry --verdict results/trial.json
```

## Weight setter

After checking the publication and stopping any other setter for the same validator wallet:

```bash
pm2 start deploy/pm2.config.js --only tensor-weight-setter
```

Keep `WEIGHT_DRY_RUN=1` for rehearsal. Set `WEIGHT_DRY_RUN=0` only for the actual cutover.
Reload the PM2 config with `--update-env` after edits. Save the final process list with `pm2 save`.
Idle chain polls write no rows; real and explicit dry-run attempts record their compact reward inputs.

## Tests and audits

```bash
.venv/bin/pytest -q -m 'not slow' -rs
.venv/bin/python selftest.py
.venv/bin/python scripts/check-exact-proofs.py --replay
.venv/bin/python scripts/audit-exact-spec.py --native --edge-cases --cases-per-kernel 200
.venv/bin/python scripts/audit-exact-models.py --random-seeds 4
```

Run workspace-mutating Lean tests sequentially. A real disposable-DB worker rehearsal:

```bash
.venv/bin/python scripts/test-exact-workflow.py \
  --admin-url postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/postgres \
  --output data/tensor-rehearsal --minutes 15
```

It creates/drops its own database, proves a source, runs every required job and saves raw timings,
decisions and a computed vector. It makes no chain calls.

The miner CLI keeps the signed-message protocol and `https://conjectures.io` default.
Public tensor uploads and board reads require the coordinated central API rollout.
API errors use problem bodies with `type`, `title`, `status`, `detail`, `reason_code`.
