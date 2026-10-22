#!/usr/bin/env bash
# Native exact-runtime development on macOS. The sandboxed competition gate runs on Linux.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.elan/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
log() { echo "[setup-macos] $*"; }
usage() {
  cat <<'EOF'
Usage: ./setup-macos.sh [--check] [--dev] [--source PATH] [--benchmark-worker]

Build the exact-runtime proofs, native model benchmark and audit executables.
  --benchmark-worker  Refuse unsafe worker setup; see docs/WORKER-DATABASE-ACCESS.md.
  --check        Report prerequisites without installing or building anything.
  --dev          Also install Python test, database-client and plotting dependencies.
  --source PATH  Build this Kernel.lean (or directory containing it).
                 Otherwise preserve lean/work/Submission.lean if it already exists,
                 or initialize it from kernels/exact/incumbent/Kernel.lean.

Requires Xcode Command Line Tools and Python 3.11+. PYTHON selects an interpreter.
Installs Elan if absent; the repository selects the pinned Lean toolchain.
The Linux sandbox, gate workers and comparison runner are separate from this setup.
EOF
}

check=0; dev=0; worker=0; source=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --benchmark-worker) worker=1; shift ;;
    --check) check=1; shift ;;
    --dev) dev=1; shift ;;
    --source)
      [ "$#" -ge 2 ] || { log "--source requires a path"; exit 2; }
      source="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) log "unknown option: $1"; usage; exit 2 ;;
  esac
done

[ "$(uname -s)" = Darwin ] || { log "macOS only; use ./setup.sh on Linux"; exit 1; }
if [ "$worker" = 1 ]; then
  log "Native macOS benchmark workers are unsupported: no verified aggregate memory/swap cap or process-tree containment."
  log "No dependencies installed or candidate launched. See docs/WORKER-DATABASE-ACCESS.md."
  exit 1
fi
missing=0
for tool in curl git; do
  if command -v "$tool" >/dev/null 2>&1; then log "ok $tool";
  else log "MISSING $tool"; missing=1; fi
done
if xcode-select -p >/dev/null 2>&1 && xcrun --find clang >/dev/null 2>&1; then
  log "ok Xcode Command Line Tools"
else
  log "MISSING compiler tools; run: xcode-select --install (then finish the installer)"
  missing=1
fi

modern_python() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; }
python="${PYTHON:-}"
if [ -n "$python" ]; then
  if ! modern_python "$python"; then
    log "PYTHON must select Python 3.11 or newer: $python"
    missing=1
  fi
else
  if [ -x "$here/.venv/bin/python" ] && modern_python "$here/.venv/bin/python"; then
    python="$here/.venv/bin/python"
  else
    for candidate in python3.12 python3.13 python3.11 python3.14 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && modern_python "$candidate"; then
        python="$(command -v "$candidate")"; break
      fi
    done
  fi
  if [ -z "$python" ]; then
    log "MISSING Python 3.11+; with Homebrew: brew install python@3.12"
    log "Then run: PYTHON=\"\$(brew --prefix python@3.12)/bin/python3.12\" ./setup-macos.sh"
    missing=1
  fi
fi
if [ -n "$python" ] && modern_python "$python"; then
  log "ok $python ($("$python" --version 2>&1))"
fi

if [ -e "$here/.venv" ]; then
  if [ ! -x "$here/.venv/bin/python" ] || ! modern_python "$here/.venv/bin/python"; then
    log "Existing .venv is unusable or older than Python 3.11; preserve it before retrying:"
    log "  mv .venv .venv-python39-backup"
    missing=1
  else
    log "ok existing .venv ($("$here/.venv/bin/python" --version 2>&1))"
  fi
fi

if [ -n "$source" ]; then
  [ ! -d "$source" ] || source="$source/Kernel.lean"
  [ -f "$source" ] || { log "source file not found: $source"; exit 2; }
  source="$(cd "$(dirname "$source")" && pwd)/$(basename "$source")"
fi
if command -v elan >/dev/null 2>&1 && command -v lake >/dev/null 2>&1; then
  log "ok Elan and Lake"
else
  log "Elan/Lake missing; a normal run installs them"
  if [ "$check" = 1 ]; then missing=1; fi
fi
[ "$check" = 0 ] || exit "$missing"
[ "$missing" = 0 ] || exit 1

# Mathlib's parallel archive extraction exceeds macOS's default soft limit (256).
# Change only this process and its children; preserve an already higher limit.
soft_files="$(ulimit -Sn)"
if [ "$soft_files" != unlimited ] && [ "$soft_files" -lt 65536 ]; then
  target_files=65536
  hard_files="$(ulimit -Hn)"
  kernel_files="$(sysctl -n kern.maxfilesperproc)"
  if [ "$hard_files" != unlimited ] && [ "$hard_files" -lt "$target_files" ]; then
    target_files="$hard_files"
  fi
  if [ "$kernel_files" -lt "$target_files" ]; then target_files="$kernel_files"; fi
  if [ "$target_files" -lt 4096 ] || ! ulimit -Sn "$target_files"; then
    log "Open-file limit is too low for Mathlib extraction (soft=$soft_files, hard=$hard_files)."
    log "Raise the shell's hard limit to at least 4096, then rerun setup."
    exit 1
  fi
  log "open-file limit raised from $soft_files to $(ulimit -Sn)"
fi

installer=""
cleanup() { [ -z "$installer" ] || rm -f "$installer"; }
trap cleanup EXIT
if ! command -v elan >/dev/null 2>&1 || ! command -v lake >/dev/null 2>&1; then
  log "installing Elan"
  installer="$(mktemp "${TMPDIR:-/tmp}/floatlib-elan.XXXXXX")"
  curl -sSf https://elan.lean-lang.org/elan-init.sh -o "$installer"
  sh "$installer" -y --default-toolchain none
fi

cd "$here"
if [ ! -x .venv/bin/python ]; then "$python" -m venv .venv; fi
.venv/bin/python -m pip install -r requirements/gate.txt
if [ "$dev" = 1 ]; then
  .venv/bin/python -m pip install -r requirements/test.txt ruff==0.16.9 matplotlib numpy
fi

log "fetching pinned dependencies"
.venv/bin/python scripts/fetch-pinned-deps.py
cd "$here/lean"
log "toolchain $(cat lean-toolchain)"
lake exe cache get
mkdir -p work
if [ -n "$source" ]; then
  if ! cmp -s "$source" work/Submission.lean; then cp "$source" work/Submission.lean; fi
elif [ ! -e work/Submission.lean ]; then
  cp "$here/kernels/exact/incumbent/Kernel.lean" work/Submission.lean
else
  log "preserving existing lean/work/Submission.lean"
fi
log "building exact-runtime proofs, benchmark and audits"
lake build ExactSuiteProofs VerifyExact benchExact exactDev validateExact exactSpecProbe \
  auditExactShapes auditExactWorkloads exactAudit

log "done; from the repository root:"
log "  lean/.lake/build/bin/exactAudit 8 971"
log "  BENCH_INNER=1 BENCH_SEED=42 lean/.lake/build/bin/benchExact"
log "  .venv/bin/python scripts/audit-exact-spec.py --native"
