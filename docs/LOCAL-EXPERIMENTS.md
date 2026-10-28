# Local development and experiments

For miner setup and quick native timings, start with the [manual](../MANUAL.md).
All commands below run from the repository root unless they explicitly change directory.

## Development environment

```bash
./setup.sh --dev
export PYTHONPATH=.:validator
.venv/bin/pytest -q -m 'not slow' -rs
```

This installs Python test, database, chain, lint and plotting dependencies into `.venv` and builds
the local Lean targets. Choose `.venv/bin/python` as your IDE interpreter; Pylance can use
`python.analysis.extraPaths: ["./validator"]` for the worker modules' bare imports.
`./setup-macos.sh --dev --source my-entry/Kernel.lean` supplies native macOS development tools.

Database tests use disposable PostgreSQL databases and need creation/role privileges. Inspect
skip counts when PostgreSQL is unavailable. `selftest.py` runs real proof/security fixtures and
takes minutes. Run workspace-mutating Lean checks sequentially.

## Check one entry

```bash
.venv/bin/python -m verification my-entry --verdict results/trial.json
GATE_AUDIT_CASES=20 .venv/bin/python -m audit my-entry --verdict results/audit.json
```

These do not change eligibility or rewards. See [audits](SPEC-AUDIT.md) for the independent oracle.

## Linux setup troubleshooting

`./setup.sh --check` lists missing tools. Install the reported OS dependencies, then rerun setup.
Bubblewrap is project-local at `.venv/bin/bwrap`; Lean/Elan toolchains are installed under the
user's `~/.elan`, while fetched packages and build products are under `lean/.lake`.

The full gate requires permitted unprivileged user namespaces and a working user systemd
session. On Ubuntu, an AppArmor rule may be needed for this checkout's bubblewrap executable;
have the host administrator configure it. A basic namespace check is:

```bash
.venv/bin/bwrap --ro-bind / / --unshare-all -- /usr/bin/true
systemd-run --user --wait --quiet /usr/bin/true
```

Failure here is a host configuration problem, not evidence that your Lean proof is incorrect.
On macOS, setup raises the process open-file limit for Mathlib cache extraction; finish any
reported OS limit adjustment and rerun setup if extraction fails. Native macOS is for trusted
local experiments; the supported competition workers use Linux isolation.

## Full local rehearsal

For contributors testing the service, [test-exact-workflow.py](../scripts/test-exact-workflow.py)
creates/drops its own database and runs proof, measurement, admission and reward calculation:

```bash
.venv/bin/python scripts/test-exact-workflow.py \
  --admin-url postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/postgres \
  --candidate my-entry/Kernel.lean --output data/workflow-check --minutes 15
```

Use disposable database admin credentials. This bypasses HTTP intake and real subnet registration;
it makes no chain calls. See [operations](OPERATIONS.md) for running a persistent instance.

## Save evidence

Keep sources, seeds, machine/compiler identities, calibration counts and raw paired observations.
Store raw data, summaries and visualizations separately under `data/`; this directory is Git-ignored,
so archive it separately when results matter. A copied source or frozen executable is more useful
than an unrecorded claim about which branch was tested. See [timing studies](TIMING.md).
