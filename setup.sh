#!/usr/bin/env bash
# One-command setup for the FloatLib kernel competition: Lean toolchain, FloatLib + Mathlib, build, gate venv.
#   ./setup.sh             install what is missing, then build (about 15 minutes the first time)
#   ./setup.sh --check     report what is installed, change nothing
#   ./setup.sh --service   also the gate worker, store client and .env
#   ./setup.sh --chain     also the bittensor SDK the chain watcher and weight setter need
#   ./setup.sh --benchmark-worker  only local builds, timing and remote DB access
#   ./setup.sh --audit-worker      only proof/audit builds and remote DB access
#   ./setup.sh --dev       full build and all Python development dependencies
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
lean="$here/lean"
export PATH="$here/.venv/bin:$HOME/.elan/bin:$PATH"
log() { echo "[setup] $(date -u +%H:%M:%S) $*"; }
missing=0
have() { if command -v "$1" >/dev/null 2>&1; then log "ok       $1"; else log "MISSING  $1 - $2"; missing=1; fi; }

check=0; service=0; chain=0; benchmark_worker=0; audit_worker=0; dev=0
for arg in "$@"; do
  case "$arg" in
    --check) check=1 ;;
    --service) service=1 ;;
    --chain) service=1; chain=1 ;;
    --benchmark-worker) benchmark_worker=1 ;;
    --audit-worker) audit_worker=1 ;;
    --dev) dev=1 ;;
    *) log "unknown option $arg"; exit 2 ;;
  esac
done
if { [ "$benchmark_worker" = 1 ] || [ "$audit_worker" = 1 ]; } && { [ "$service" = 1 ] || [ "$dev" = 1 ] || { [ "$benchmark_worker" = 1 ] && [ "$audit_worker" = 1 ]; }; }; then
  log "worker setup is a minimal mode; choose one worker profile without --service, --chain or --dev"
  exit 2
fi

have python3 "apt install python3 python3-venv"
have curl "apt install curl"
have git "apt install git"
if [ "$check" = 1 ]; then
  bash "$here/scripts/install-bubblewrap.sh" --check || missing=1
fi
have taskset "apt install util-linux, or run the gate with BENCH_CPU= (noisier timing)"
have prlimit "apt install util-linux, or run the gate with GATE_FILE_MB=0 (no per-file disk cap)"
have setpriv "apt install util-linux (required for worker subprocess cleanup)"
have cc "apt install build-essential (required for native Lean binaries)"
have systemd-run "install systemd with a working user session for sandbox memory limits"
have lake "installed below via elan"
# The service's Postgres runs under docker compose (skip with a managed DATABASE_URL); pm2 runs its processes.
if [ "$service" = 1 ]; then
  have docker "install Docker Engine with the compose plugin, or point DATABASE_URL at a managed Postgres"
  have pm2 "npm install -g pm2"
fi
[ "$check" = 1 ] && exit "$missing"

# Check/install the sandbox before spending time fetching or compiling Lean dependencies.
[ -x "$here/.venv/bin/python" ] || python3 -m venv "$here/.venv"
bash "$here/scripts/install-bubblewrap.sh"

# Lean: elan installs the toolchain pinned in lean/lean-toolchain on first use.
if ! command -v lake >/dev/null 2>&1; then
  log "installing elan"
  curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none
fi
cd "$lean"
log "toolchain $(cat lean-toolchain)"
log "fetching FloatLib and TorchLean (pinned in lakefile.lean) and the Mathlib build cache"
python3 "$here/scripts/fetch-pinned-deps.py"
lake exe cache get
log "building FloatLib, the contract and the bench against the incumbent"
mkdir -p work
cp "$here/kernels/exact/incumbent/Kernel.lean" work/Submission.lean
if [ "$benchmark_worker" = 1 ]; then
  lake build VerifyExact benchExact 2>&1 | tail -3
elif [ "$audit_worker" = 1 ]; then
  lake build VerifyExact exactAudit 2>&1 | tail -3
else
  lake build ExactSuiteProofs VerifyExact benchExact exactDev validateExact exactSpecProbe auditExactShapes auditExactWorkloads exactAudit 2>&1 | tail -3
fi

# Python dependencies for the local gate and miner submission CLI.
cd "$here"
[ -x .venv/bin/python ] || python3 -m venv .venv
if [ "$benchmark_worker" = 1 ]; then
  .venv/bin/pip install -q -r requirements/benchmark-worker.txt
elif [ "$audit_worker" = 1 ]; then
  .venv/bin/pip install -q -r requirements/benchmark-worker.txt
elif [ "$dev" = 1 ]; then
  log "installing all Python development dependencies into .venv"
  .venv/bin/pip install -q -r requirements/dev.txt
else
  .venv/bin/pip install -q -r requirements/gate.txt -r requirements/miner.txt
fi
if [ "$service" = 1 ]; then
  log "installing the validator worker dependencies"
  .venv/bin/pip install -q -r requirements/service.txt
  [ -f .env ] || { cp .env.example .env; log "wrote .env from .env.example - set POSTGRES_PASSWORD"; }
fi
if [ "$chain" = 1 ]; then
  log "installing the bittensor SDK"
  .venv/bin/pip install -q -r requirements/chain.txt
fi
if [ "$benchmark_worker" = 1 ]; then
  log "done - configure remote DB access; run with PYTHONPATH=.:validator .venv/bin/python -m workers.benchmark_worker --environment NAME"
elif [ "$audit_worker" = 1 ]; then
  log "done - run PYTHONPATH=.:validator .venv/bin/python -m workers.exact_auditor --credentials FILE"
elif [ "$dev" = 1 ]; then
  log "done - select $here/.venv/bin/python as your IDE interpreter; test with .venv/bin/python -m pytest -m 'not slow'"
else
  log "done - test: .venv/bin/python selftest.py"
fi
