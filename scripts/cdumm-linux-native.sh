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
readonly DESKTOP_MARKER="$VENV_DIR/.cdumm-desktop-installed"

readonly XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
readonly DESKTOP_FILE="$XDG_DATA/applications/cdumm.desktop"
# Install the icon under the hicolor theme so waybar / wlr-taskbar /
# any GTK icon-theme lookup (which is what most Wayland taskbars use
# — they map app_id straight to an icon-theme name, NOT to the
# .desktop's Icon= path) finds it as ``cdumm``. The earlier flat
# ~/.local/share/icons/cdumm.png location wasn't in any theme path
# so the lookup returned nothing on Hyprland+waybar (PR #123).
readonly ICON_DEST="$XDG_DATA/icons/hicolor/512x512/apps/cdumm.png"
readonly ICON_DEST_LEGACY="$XDG_DATA/icons/cdumm.png"

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

# Install a freedesktop ``cdumm.desktop`` + PNG icon under
# ``$XDG_DATA_HOME/{applications,icons}``. Required for Wayland to
# show CDUMM's icon — the compositor matches the running window's
# app_id (set from main.py via QGuiApplication.setDesktopFileName)
# to the .desktop file's basename, then loads the Icon= field. Also
# fixes the post-launch icon revert and the missing-icon-on-
# hide-to-taskbar case RoGreat reported in PR #123.
#
# Idempotent — re-runs only when the marker is missing or --reinstall
# is passed. Both files live under ~/.local/share (user-local), so
# no sudo and nothing system-wide is touched.
install_desktop_entry() {
  if [[ -f "$DESKTOP_MARKER" && $REINSTALL -eq 0 ]]; then
    return 0
  fi
  local src_icon="$REPO_ROOT/assets/cdumm-icon-square.png"
  if [[ ! -f "$src_icon" ]]; then
    warn "Icon source $src_icon missing — skipping desktop entry install."
    return 0
  fi
  log "Installing cdumm.desktop + hicolor icon (Wayland taskbar)"
  mkdir -p "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_DEST")"
  cp -f "$src_icon" "$ICON_DEST"
  # Clean up the pre-fix flat icon location if present (left by an
  # earlier version of this script that put the PNG outside any
  # theme path). Harmless leftover otherwise.
  rm -f "$ICON_DEST_LEGACY"
  # Write the .desktop file. ``Icon=cdumm`` (theme name, NOT an
  # absolute path) so taskbars that route through the GTK icon-theme
  # API resolve it via the hicolor entry above. ``StartupWMClass``
  # must match the app_id Qt advertises (the "cdumm" string we pass
  # to setDesktopFileName in main.py).
  {
    echo "[Desktop Entry]"
    echo "Type=Application"
    echo "Name=CDUMM"
    echo "GenericName=Mod Manager"
    echo "Comment=Crimson Desert Ultimate Mods Manager (native Linux build)"
    echo "Exec=$REPO_ROOT/scripts/cdumm-linux-native.sh"
    echo "Icon=cdumm"
    echo "Terminal=false"
    echo "Categories=Game;Utility;"
    echo "StartupWMClass=cdumm"
    echo "StartupNotify=true"
  } > "$DESKTOP_FILE"
  # Best-effort cache refreshes. update-desktop-database is for the
  # applications/ side; gtk-update-icon-cache is what waybar and
  # GTK-icon-theme consumers re-read. Absence of either binary is
  # fine on minimal WMs.
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -t -f "$XDG_DATA/icons/hicolor" >/dev/null 2>&1 || true
  fi
  touch "$DESKTOP_MARKER"
}

# ── main ────────────────────────────────────────────────────────────

provision_venv
install_deps
build_native
install_desktop_entry

# Route Qt file dialogs through the XDG desktop portal.
#
# CDUMM forces the Fusion QStyle and themes its own UI via
# qfluentwidgets, but the QFileDialog convenience methods
# (getExistingDirectory etc.) are plain Qt widgets that don't
# inherit the qfluentwidgets dark palette — so on a setup where
# QT_QPA_PLATFORMTHEME=qt6ct (which doesn't delegate file dialogs
# to the portal), the picker pops up in light mode regardless of
# the app theme.
#
# Setting the platform theme to 'xdgdesktopportal' makes Qt use
# the portal's file chooser (the gtk backend), which renders with
# the system GTK theme — so the picker follows your desktop's
# light/dark preference instead of CDUMM's internal theme. Only
# affects this process; your global qt6ct setting is untouched.
#
# Override with CDUMM_QT_PLATFORMTHEME=... (or set it empty to
# fall back to whatever your environment already has) if the
# portal isn't available on a given machine.
export QT_QPA_PLATFORMTHEME="${CDUMM_QT_PLATFORMTHEME-xdgdesktopportal}"

log "Launching CDUMM"
exec "$VENV_DIR/bin/python" -m cdumm.main "${CDUMM_ARGS[@]+"${CDUMM_ARGS[@]}"}"
