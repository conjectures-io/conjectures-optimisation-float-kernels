#!/usr/bin/env bash
# One-command setup for the FloatLib kernel competition: Lean toolchain, FloatLib + Mathlib, build, gate venv.
#   ./setup.sh             install what is missing, then build (about 15 minutes the first time)
#   ./setup.sh --check     report what is installed, change nothing
#   ./setup.sh --service   also the gate worker, store client and .env
#   ./setup.sh --chain     also the bittensor SDK the chain watcher and weight setter need
#   ./setup.sh --exact     build the local exact-runtime kernels, benchmark and validation tools
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
lean="$here/lean"
export PATH="$here/.venv/bin:$HOME/.elan/bin:$PATH"
log() { echo "[setup] $(date -u +%H:%M:%S) $*"; }
missing=0
have() { if command -v "$1" >/dev/null 2>&1; then log "ok       $1"; else log "MISSING  $1 - $2"; missing=1; fi; }

check=0; service=0; chain=0; exact=0
for arg in "$@"; do
  case "$arg" in
    --check) check=1 ;;
    --service) service=1 ;;
    --chain) service=1; chain=1 ;;
    --exact) exact=1 ;;
    *) log "unknown option $arg"; exit 2 ;;
  esac
done

have python3 "apt install python3 python3-venv"
have curl "apt install curl"
have git "apt install git"
if [ "$check" = 1 ]; then
  bash "$here/scripts/install-bubblewrap.sh" --check || missing=1
fi
have taskset "apt install util-linux, or run the gate with BENCH_CPU= (noisier timing)"
have prlimit "apt install util-linux, or run the gate with GATE_FILE_MB=0 (no per-file disk cap)"
have setpriv "apt install util-linux (required for worker subprocess cleanup)"
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
if [ "$exact" = 1 ]; then
  cp "$here/kernels/exact/incumbent/Kernel.lean" work/Submission.lean
  lake build ExactSuiteProofs VerifyExact benchExact exactDev validateExact exactSpecProbe auditExactShapes auditExactWorkloads exactAudit 2>&1 | tail -3
else
  cp "$here/kernels/add/incumbent/Kernel.lean" work/Submission.lean
  lake build Contract Verify bench 2>&1 | tail -3
fi

# Python dependencies for the local gate and miner submission CLI.
cd "$here"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements/gate.txt -r requirements/miner.txt
if [ "$service" = 1 ]; then
  log "installing the validator worker dependencies"
  .venv/bin/pip install -q -r requirements/service.txt
  [ -f .env ] || { cp .env.example .env; log "wrote .env from .env.example - set POSTGRES_PASSWORD"; }
fi
if [ "$chain" = 1 ]; then
  log "installing the bittensor SDK"
  .venv/bin/pip install -q -r requirements/chain.txt
fi
if [ "$exact" = 1 ]; then
  log "done - validate: .venv/bin/python scripts/audit-exact-spec.py --native"
else
  log "done - test the environment: .venv/bin/python selftest.py (about 15 minutes)"
fi
