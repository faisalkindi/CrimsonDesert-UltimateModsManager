#!/usr/bin/env bash
# CDUMM Linux launcher. Provisions a Wine prefix on first run, then hands
# CDUMM3.exe off to Wine. Verified against Wine 11.0 + vcrun2022 +
# corefonts on Ubuntu 24.04.
#
# Usage:
#   ./cdumm-linux.sh                 Launch using the standalone prefix.
#   ./cdumm-linux.sh --proton        Launch inside Crimson Desert's own
#                                    Proton prefix via protontricks-launch
#                                    (requires protontricks installed).
#   ./cdumm-linux.sh --reset         Delete the standalone prefix and
#                                    re-provision on the next launch.
#   ./cdumm-linux.sh -- <args>       Pass args after -- through to CDUMM.
#
# Expects CDUMM3.exe next to this script or in the same directory the
# script is invoked from. Override with CDUMM_EXE=/path/to/CDUMM3.exe.
set -euo pipefail

readonly CD_STEAM_APPID="3321460"
readonly PREFIX_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/CDUMM/wineprefix"
readonly MARKER="${XDG_DATA_HOME:-$HOME/.local/share}/CDUMM/.provisioned"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

resolve_exe() {
  if [[ -n "${CDUMM_EXE:-}" && -f "$CDUMM_EXE" ]]; then
    printf '%s' "$CDUMM_EXE"; return
  fi
  local here; here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  for cand in "$here/CDUMM3.exe" "$here/../CDUMM3.exe" "$PWD/CDUMM3.exe"; do
    [[ -f "$cand" ]] && { printf '%s' "$cand"; return; }
  done
  die "CDUMM3.exe not found. Place it next to this script or set CDUMM_EXE=/path/to/CDUMM3.exe."
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 \
    || die "'$1' not found on PATH. $2"
}

provision_prefix() {
  require_cmd wine "Install WineHQ 11 stable: https://wiki.winehq.org/Ubuntu"
  require_cmd winetricks "Install from your package manager (apt/dnf/pacman)."
  require_cmd cabextract "Install from your package manager."

  local wine_ver
  wine_ver=$(wine --version 2>/dev/null || echo "unknown")
  log "Using $wine_ver"

  if [[ -f "$MARKER" && -d "$PREFIX_DIR/drive_c/windows" ]]; then
    return 0
  fi

  log "First-run provisioning. This takes ~3-5 minutes."
  mkdir -p "$(dirname "$PREFIX_DIR")"
  export WINEPREFIX="$PREFIX_DIR"
  export WINEDEBUG="-all"

  log "Bootstrapping Wine prefix at $PREFIX_DIR"
  wine wineboot --init >/dev/null 2>&1

  log "Installing vcrun2022 + corefonts via winetricks"
  winetricks -q vcrun2022 corefonts

  touch "$MARKER"
  log "Prefix ready."
}

run_standalone() {
  local exe; exe=$(resolve_exe)
  provision_prefix
  export WINEPREFIX="$PREFIX_DIR"
  export WINEDEBUG="-all"
  log "Launching CDUMM ($exe)"
  exec wine "$exe" "$@"
}

run_proton() {
  require_cmd protontricks-launch "Install Protontricks (Bazzite ships it; Flatpak version: https://flathub.org/apps/com.github.Matoking.protontricks)"
  local exe; exe=$(resolve_exe)
  log "Launching CDUMM via Proton prefix (AppID $CD_STEAM_APPID)"
  exec protontricks-launch --appid "$CD_STEAM_APPID" "$exe" "$@"
}

reset_prefix() {
  if [[ -d "$PREFIX_DIR" ]]; then
    warn "Removing $PREFIX_DIR"
    rm -rf -- "$PREFIX_DIR"
  fi
  rm -f -- "$MARKER"
  log "Prefix reset. Next launch will re-provision."
}

# ── Arg parsing ─────────────────────────────────────────────────────────
mode="standalone"
passthrough=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proton)   mode="proton"; shift ;;
    --reset)    mode="reset"; shift ;;
    -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
    --)         shift; passthrough=("$@"); break ;;
    *)          passthrough+=("$1"); shift ;;
  esac
done

case "$mode" in
  standalone) run_standalone "${passthrough[@]}" ;;
  proton)     run_proton "${passthrough[@]}" ;;
  reset)      reset_prefix ;;
esac
