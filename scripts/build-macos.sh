#!/usr/bin/env bash
# CDUMM macOS build script. Produces dist/CDUMM.app and
# dist/CDUMM-<version>-macos-arm64.dmg, ad-hoc signed.
#
# Mirrors .github/workflows/release-macos.yml so devs can reproduce a
# CI run locally before pushing a release tag. The CI workflow calls
# this same script — keep them in sync.
#
# Usage:
#   ./scripts/build-macos.sh           # full build (icns + wheel + .app + dmg)
#   ./scripts/build-macos.sh --no-dmg  # skip DMG packaging
#   ./scripts/build-macos.sh --no-wheel # skip Rust rebuild (use installed wheel)
#
# Ad-hoc signed .app caveat: macOS Gatekeeper will pop "App can't be
# opened because Apple cannot check it for malicious software" the
# first time a user runs CDUMM. Right-click → Open the first time, or
# run `xattr -dr com.apple.quarantine /Applications/CDUMM.app`.
# Notarisation would suppress the prompt but needs an Apple Developer
# account ($99/year) which the project doesn't have.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Sanity checks ────────────────────────────────────────────────
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "build-macos.sh is macOS-only. For Windows: python scripts/build.py" >&2
    exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "build-macos.sh requires Apple Silicon (arm64). Crimson Desert" >&2
    echo "macOS does not run on Intel; we don't ship an Intel build." >&2
    exit 1
fi

# Resolve a Python interpreter. CI uses ``setup-python`` which puts the
# requested Python on PATH; locally we accept whatever ``python3``
# resolves to (Homebrew Python 3.13+).
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "python3 not found. Install via 'brew install python@3.13'." >&2
    exit 1
}

# ── Parse args ───────────────────────────────────────────────────
SKIP_DMG=0
SKIP_WHEEL=0
for arg in "$@"; do
    case "$arg" in
        --no-dmg)   SKIP_DMG=1 ;;
        --no-wheel) SKIP_WHEEL=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }

# ── Resolve version from cdumm/__init__.py ───────────────────────
VERSION=$("$PYTHON" -c "import sys; sys.path.insert(0, 'src'); from cdumm import __version__; print(__version__)")
DMG_NAME="CDUMM-${VERSION}-macos-arm64.dmg"
log "Building CDUMM ${VERSION} for macOS arm64"

mkdir -p dist

# ── 1. Generate cdumm.icns from the 1024x1024 source PNG ─────────
# Apple's iconutil expects a .iconset folder with specific sizes.
# We have a 1024x1024 source at assets/cdumm-logo.png (RGBA) which
# is the largest size iconutil consumes (icon_512x512@2x).
log "Generating cdumm.icns from assets/cdumm-logo.png"
ICONSET_DIR="$(mktemp -d)/cdumm.iconset"
mkdir -p "$ICONSET_DIR"
SRC_ICON=assets/cdumm-logo.png
if [[ ! -f "$SRC_ICON" ]]; then
    echo "Source icon not found: $SRC_ICON" >&2
    exit 1
fi
# Apple's required sizes for a complete iconset:
#   16, 32, 64, 128, 256, 512 (1x)
#   16@2x=32, 32@2x=64, 128@2x=256, 256@2x=512, 512@2x=1024
# (The 1x and @2x for the same physical pixel size are the SAME image.)
sips -z 16   16   "$SRC_ICON" --out "$ICONSET_DIR/icon_16x16.png"     >/dev/null
sips -z 32   32   "$SRC_ICON" --out "$ICONSET_DIR/icon_16x16@2x.png"  >/dev/null
sips -z 32   32   "$SRC_ICON" --out "$ICONSET_DIR/icon_32x32.png"     >/dev/null
sips -z 64   64   "$SRC_ICON" --out "$ICONSET_DIR/icon_32x32@2x.png"  >/dev/null
sips -z 128  128  "$SRC_ICON" --out "$ICONSET_DIR/icon_128x128.png"   >/dev/null
sips -z 256  256  "$SRC_ICON" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 256  256  "$SRC_ICON" --out "$ICONSET_DIR/icon_256x256.png"   >/dev/null
sips -z 512  512  "$SRC_ICON" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
sips -z 512  512  "$SRC_ICON" --out "$ICONSET_DIR/icon_512x512.png"   >/dev/null
sips -z 1024 1024 "$SRC_ICON" --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$ICONSET_DIR" -o cdumm.icns

# ── 2. Build the cdumm_native Rust extension (arm64) ─────────────
# Skipped if the wheel is already installed AND --no-wheel was passed.
# This is a conservative ~30s rebuild on a fresh checkout — cargo
# caches make subsequent runs near-instant.
if [[ "$SKIP_WHEEL" -eq 0 ]]; then
    log "Building cdumm_native (Rust extension)"
    ( cd native && "$PYTHON" -m maturin build --release )
    log "Installing cdumm_native wheel into the build environment"
    "$PYTHON" -m pip install --force-reinstall \
        --no-deps native/target/wheels/cdumm_native-*-arm64.whl
else
    log "Skipping cdumm_native rebuild (--no-wheel)"
    if ! "$PYTHON" -c "import cdumm_native" 2>/dev/null; then
        warn "cdumm_native not importable; PyInstaller will fail to bundle it"
    fi
fi

# ── 3. Build the .app via PyInstaller ────────────────────────────
log "Running PyInstaller (cdumm-macos.spec)"
"$PYTHON" -m PyInstaller cdumm-macos.spec --clean --noconfirm
if [[ ! -d dist/CDUMM.app ]]; then
    echo "PyInstaller did not produce dist/CDUMM.app — see output above" >&2
    exit 1
fi

# ── 4. Ad-hoc codesign ───────────────────────────────────────────
# ``codesign --sign -`` is the ad-hoc identity (no certificate). This
# is enough to keep macOS happy for the user's own builds and lets the
# .app launch on the build machine without re-prompting Gatekeeper on
# every run. Other users still see "Apple cannot check it" the first
# time and need to right-click → Open. Notarisation would suppress
# that prompt but needs a paid Apple Developer ID.
#
# --deep recurses into Frameworks/ and embedded binaries (PySide6 is
# a forest of dylibs); --force re-signs anything PyInstaller pre-signed
# during build.
log "Ad-hoc signing dist/CDUMM.app"
codesign --force --deep --sign - dist/CDUMM.app

# Quick verify that the signature actually attached.
codesign --verify --verbose=2 dist/CDUMM.app 2>&1 | sed 's/^/    /'

# ── 5. Package the DMG ───────────────────────────────────────────
# UDZO = compressed (zlib). UDBZ would be smaller but slower to mount;
# zlib is the macOS default and keeps the user-perceived install fast.
if [[ "$SKIP_DMG" -eq 0 ]]; then
    log "Packaging $DMG_NAME"
    rm -f "dist/$DMG_NAME"
    hdiutil create \
        -volname "CDUMM ${VERSION}" \
        -srcfolder dist/CDUMM.app \
        -ov -format UDZO \
        "dist/$DMG_NAME" >/dev/null
    log "Verifying DMG"
    hdiutil verify "dist/$DMG_NAME" 2>&1 | tail -3 | sed 's/^/    /'
fi

# ── Summary ──────────────────────────────────────────────────────
APP_SIZE=$(du -sh dist/CDUMM.app | cut -f1)
log "Build complete:"
echo "    dist/CDUMM.app         (${APP_SIZE})"
if [[ "$SKIP_DMG" -eq 0 ]]; then
    DMG_SIZE=$(du -sh "dist/$DMG_NAME" | cut -f1)
    echo "    dist/$DMG_NAME  (${DMG_SIZE})"
fi
