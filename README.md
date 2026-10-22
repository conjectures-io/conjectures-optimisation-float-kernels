# Proved tensor kernels

One submission supplies all 12 neural-network kernels and a Lean proof that they equal the pinned
specification. The coordinator checks correctness once, then dispatches paired benchmark jobs to
hardware workers. Five model-family boards and an overall board compare complete implementations.
Each board pays its current and previous eligible champion using configurable allocations.

This branch replaces the former FloatLib workflow. It uses a **fresh competition database**
with migration revision `tensor_0001`. Central API/frontend support must be updated to the
[new schema contract](deploy/platform/CONTRACT.md) before public activation.

## Local setup

```bash
./setup.sh --service
export PYTHONPATH=.:validator
.venv/bin/python scripts/exact-config.py --quick --output competition.toml
docker compose up -d db
docker compose build migrate
docker compose run --rm migrate
```

Use `./setup.sh --chain` to add the Bittensor SDK. Setup installs the pinned Lean packages,
project-local bubblewrap, proof/benchmark targets and miner dependencies.
The sandbox needs user namespaces and a working user systemd session for memory limits.

For a benchmark-only host with a remote database, use `./setup.sh --benchmark-worker`.
It builds only `VerifyExact` and `benchExact` and installs the database client and timing
dependencies. It needs neither Docker nor PM2, miner signing libraries or Bittensor.
Configure remote DB credentials as described in [distributed workers](docs/EXACT-WORKERS.md).

For development, run `./setup.sh --dev`. This builds the full local toolset and installs
tests, database clients, Bittensor, lint and plotting dependencies into `.venv`.
Select `.venv/bin/python` as your IDE's Python interpreter (in VS Code:
**Python: Select Interpreter**). For bare imports such as `db`, add `validator` to your
language server's import paths; for VS Code/Pylance, set `python.analysis.extraPaths`
to `["./validator"]`. Development setup does not start services or create a database.

## Native macOS research

Run `./setup-macos.sh --dev` to build proofs, benchmark and audit executables with test/plotting
dependencies. It raises the process open-file limit for Mathlib extraction and preserves the
existing submission. Use `--source kernels/exact/cached-conv/Kernel.lean` to select another source.
The comparison runner works natively without CPU affinity and records that limitation.
Sandboxed competition workers require Linux.

## Run

```bash
pm2 start deploy/pm2.config.js --only tensor-coordinator,tensor-benchmark-worker
```

The config file selects boards, reference sources, hardware identities, seeds, timings, admission
and rewards. Hardware workers use the same code on each architecture and receive immutable jobs.
The weight setter reads the coordinator's compact publication; it defaults to dry run.

See [MANUAL.md](MANUAL.md) for verification, testing and operations,
[distributed workers](docs/EXACT-WORKERS.md), [specification](docs/EXACT-CONTRACT.md),
[mathematical proofs](docs/EXACT-PROOFS.md), and [independent audits](docs/EXACT-SPEC-AUDIT.md).

## Test

```bash
.venv/bin/pip install -r requirements/gate.txt -r requirements/test.txt # already included by --dev
.venv/bin/pytest -q -m 'not slow'
.venv/bin/python selftest.py
```

Database tests create and migrate a disposable database on the configured PostgreSQL server.
They require CREATE DATABASE and role-management privileges. Missing PostgreSQL causes skips;
inspect the reported counts. The real proof fixtures take several minutes.

See the [refactor validation report](docs/TENSOR-REFACTOR-VALIDATION.md) for test results and real worker rehearsals.
