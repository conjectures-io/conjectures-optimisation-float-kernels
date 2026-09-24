#!/usr/bin/env bash
# One-command setup for the FloatLib kernel competition: Lean toolchain, FloatLib + Mathlib, build, gate venv.
#   ./setup.sh           install what is missing, then build (about 15 minutes the first time)
#   ./setup.sh --check   report what is installed, change nothing
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
lean="$here/lean"
export PATH="$HOME/.elan/bin:$PATH"
log() { echo "[setup] $(date -u +%H:%M:%S) $*"; }
missing=0
have() { if command -v "$1" >/dev/null 2>&1; then log "ok       $1"; else log "MISSING  $1 - $2"; missing=1; fi; }

have python3 "apt install python3 python3-venv"
have curl "apt install curl"
have git "apt install git"
have bwrap "apt install bubblewrap, or run the gate with GATE_SANDBOX=0 (never on a validator)"
have taskset "apt install util-linux, or run the gate with BENCH_CPU= (noisier timing)"
have lake "installed below via elan"
if [ "${1:-}" = "--check" ]; then exit "$missing"; fi

# Lean: elan installs the toolchain pinned in lean/lean-toolchain on first use.
if ! command -v lake >/dev/null 2>&1; then
  log "installing elan"
  curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none
fi
cd "$lean"
log "toolchain $(cat lean-toolchain)"
log "fetching FloatLib (pinned in lakefile.lean) and the Mathlib build cache"
lake exe cache get
log "building FloatLib, the contract and the bench against the incumbent"
mkdir -p work
cp "$here/submissions/incumbent/Kernel.lean" work/Submission.lean
lake build Contract Verify bench 2>&1 | tail -3

# The gate's Python environment, pinned in requirements.txt.
cd "$here"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
log "done - test the environment: .venv/bin/python selftest.py (about 4 minutes)"
