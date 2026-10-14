#!/usr/bin/env bash
# Project-local overlay-capable sandbox; no replacement of the system bubblewrap.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
version=0.11.2
commit=1b80120ef26a28e065e67f89bfef873f13bdd317
target="$here/.venv/bin/bwrap"
log() { echo "[bubblewrap] $*"; }
compatible() {
  local help
  [ -x "$1" ] || return 1
  [ "$("$1" --version 2>/dev/null)" = "bubblewrap $version" ] || return 1
  help="$("$1" --help 2>/dev/null)" || return 1
  [[ "$help" == *--overlay-src* && "$help" == *--overlay* && "$help" == *--ro-overlay* ]]
}
case "${1:-}" in
  ""|--check) ;;
  *) log "usage: $0 [--check]"; exit 2 ;;
esac
if compatible "$target"; then
  log "ok $target ($version, overlay support)"
  exit 0
fi
if [ "${1:-}" = --check ]; then
  log "MISSING compatible project bubblewrap; run ./setup.sh or bash scripts/install-bubblewrap.sh"
  exit 1
fi

# Builds need libcap headers as well as the compiler and Meson/Ninja. Install only when needed.
need_build=0
for tool in cc meson ninja pkg-config git; do
  command -v "$tool" >/dev/null 2>&1 || need_build=1
done
if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists libcap; then
  need_build=1
fi
if [ "$need_build" = 1 ]; then
  packages=(build-essential meson ninja-build pkg-config libcap-dev git ca-certificates)
  if ! command -v apt-get >/dev/null 2>&1; then
    log "install a C compiler, Meson, Ninja, pkg-config, git and libcap development headers, then retry"
    exit 1
  fi
  apt=(apt-get)
  if [ "$EUID" != 0 ]; then
    if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
      log "build dependencies missing; run: sudo apt-get install ${packages[*]}"
      exit 1
    fi
    apt=(sudo -n apt-get)
  fi
  log "installing bubblewrap build dependencies"
  "${apt[@]}" update
  "${apt[@]}" install -y "${packages[@]}"
fi

[ -x "$here/.venv/bin/python" ] || python3 -m venv "$here/.venv"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/float-bubblewrap.XXXXXX")"
staged=""
trap 'rm -rf "$build_dir"; [ -z "$staged" ] || rm -f "$staged"' EXIT
log "building pinned bubblewrap $version"
git clone --quiet --depth 1 --branch "v$version" \
  https://github.com/containers/bubblewrap.git "$build_dir/src"
[ "$(git -C "$build_dir/src" rev-parse HEAD)" = "$commit" ] || {
  log "upstream tag does not match the pinned commit; refusing to build"
  exit 1
}
meson setup "$build_dir/build" "$build_dir/src" \
  -Dman=disabled -Dtests=false -Dsupport_setuid=false
ninja -C "$build_dir/build"
compatible "$build_dir/build/bwrap" || { log "built binary lacks required overlay support"; exit 1; }
staged="$(mktemp "$here/.venv/bin/.bwrap.XXXXXX")"
install -m 0755 "$build_dir/build/bwrap" "$staged"
mv -f "$staged" "$target"
staged=""
log "installed $target ($version, no setuid)"
