#!/usr/bin/env bash
# Native Linux launcher for CDUMM. Provisions a Python virtualenv,
# installs CDUMM + dependencies via pip, optionally builds the Rust
# native component (cdumm_native) for the ~260x hashing speedup and
# 60% faster batch imports, then launches the GUI.
#
# This is the "no Wine" path: CDUMM runs as native Python on Linux
# and manages mods for Crimson Desert running under Proton/Steam.
# The game itself is still a Windows binary that Proton runs via
# Wine — only the manager goes native. For the Wine-based launcher
# (CDUMM3.exe under Wine), see scripts/cdumm-linux.sh — that path
# is still maintained for users who need ASI plugin support.
#
# Usage:
#   ./cdumm-linux-native.sh              Launch CDUMM. Provisions
#                                        deps on first run.
#   ./cdumm-linux-native.sh --reinstall  Force-reinstall deps + the
#                                        Rust native component.
#   ./cdumm-linux-native.sh --no-native  Skip the Rust native build
#                                        (pure-Python fallback works
#                                        but is slower).
#   ./cdumm-linux-native.sh -- <args>    Pass <args> through to CDUMM.
#   ./cdumm-linux-native.sh --help       Print this header.

set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VENV_DIR="$REPO_ROOT/.venv"
readonly DEPS_MARKER="$VENV_DIR/.cdumm-deps-installed"
readonly NATIVE_MARKER="$VENV_DIR/.cdumm-native-built"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

# ── argument parsing ────────────────────────────────────────────────

REINSTALL=0
BUILD_NATIVE=1
CDUMM_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reinstall) REINSTALL=1; shift ;;
    --no-native) BUILD_NATIVE=0; shift ;;
    --help|-h)
      # Print the docstring (lines 2..first blank) with the
      # leading '# ' stripped. Skips the shebang on line 1.
      sed -n '2,/^$/{ s/^# \?//; p; }' "$0"
      exit 0
      ;;
    --) shift; CDUMM_ARGS=("$@"); break ;;
    *) CDUMM_ARGS+=("$1"); shift ;;
  esac
done

# ── locate a usable Python interpreter ──────────────────────────────

# CDUMM's pyproject.toml requires Python >= 3.10. Some distros ship
# only ``python3`` without versioned aliases; iterate candidates in
# rough preference order (newest first) so the version check fast-
# paths to a known-good interpreter when one is available.
find_python() {
  local cand ver
  for cand in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver=$("$cand" -c \
        'import sys; print("ok" if sys.version_info >= (3,10) else "old")' \
        2>/dev/null) || continue
      if [[ "$ver" == "ok" ]]; then
        printf '%s' "$cand"
        return 0
      fi
    fi
  done
  return 1
}

# ── provisioning ────────────────────────────────────────────────────

provision_venv() {
  if [[ -d "$VENV_DIR" && $REINSTALL -eq 0 ]]; then
    return 0
  fi
  local python_bin
  python_bin=$(find_python) || die \
    "Need Python 3.10 or newer. Install via your package manager (e.g. \
'sudo pacman -S python', 'sudo apt install python3', \
'sudo dnf install python3') and try again."
  log "Using $($python_bin --version) ($python_bin)"
  if [[ -d "$VENV_DIR" ]]; then
    log "Removing existing venv at $VENV_DIR"
    rm -rf "$VENV_DIR"
  fi
  log "Creating venv at $VENV_DIR"
  "$python_bin" -m venv "$VENV_DIR"
}

install_deps() {
  if [[ -f "$DEPS_MARKER" && $REINSTALL -eq 0 ]]; then
    return 0
  fi
  log "Upgrading pip"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  log "Installing CDUMM + dependencies (PySide6 is ~200MB; allow a minute)"
  "$VENV_DIR/bin/pip" install --quiet -e "$REPO_ROOT"
  touch "$DEPS_MARKER"
}

# Build the Rust native component (cdumm_native). The pure-Python
# fallback paths handle every cdumm_native callsite via
# ``try: import cdumm_native; except ImportError: ...`` blocks, so
# this step is best-effort: if cargo isn't installed we just warn
# and let the user run CDUMM with the slower pure-Python paths.
build_native() {
  if [[ $BUILD_NATIVE -eq 0 ]]; then
    return 0
  fi
  if [[ -f "$NATIVE_MARKER" && $REINSTALL -eq 0 ]]; then
    return 0
  fi
  if ! command -v cargo >/dev/null 2>&1; then
    warn "cargo (Rust toolchain) not on PATH — skipping cdumm_native build."
    warn "CDUMM will fall back to pure-Python for hashing / batch import"
    warn "(slower but functional). Install Rust via https://rustup.rs and"
    warn "re-run with --reinstall to get the speedup."
    return 0
  fi
  log "Installing maturin (Rust ↔ Python build glue)"
  "$VENV_DIR/bin/pip" install --quiet maturin
  log "Building cdumm_native (release profile, this takes ~30s)"
  (cd "$REPO_ROOT/native" \
    && "$VENV_DIR/bin/maturin" develop --release --quiet)
  touch "$NATIVE_MARKER"
  log "cdumm_native built and installed in the venv"
}

# ── main ────────────────────────────────────────────────────────────

provision_venv
install_deps
build_native

log "Launching CDUMM"
exec "$VENV_DIR/bin/python" -m cdumm.main "${CDUMM_ARGS[@]+"${CDUMM_ARGS[@]}"}"
