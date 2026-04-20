"""ReShade install detection for Crimson Desert.

Scans `<game_dir>/bin64/` for the signs of a ReShade install:
  - `dxgi.dll` OR `d3d12.dll`  (the proxy DLL — REQUIRED)
  - `ReShade.ini`              (config — optional on a very fresh install)
  - `reshade-shaders/`         (shader pack — optional)

Returns a `ReshadeInstall` dataclass with a three-state `state` field:
  - "installed"      — proxy DLL found
  - "not_installed"  — no proxy DLL (user hasn't installed ReShade yet)
  - "error"          — IO failure during scan (permissions, missing game dir,
                       antivirus). Distinct state so the UI can tell the user
                       to check access rather than wrongly saying "not
                       installed".

All verified ReShade behavior (PresetPath resolution against [INSTALL] BasePath=,
preset file disambiguation via [*.fx] or Techniques=) is sourced from crosire's
own documentation on the ReShade forum.

Pure-logic module; no Qt imports, no database, no network.
"""
from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DetectState = Literal["installed", "not_installed", "error"]


@dataclass(frozen=True)
class ReshadeInstall:
    state: DetectState
    dll_path: Path | None
    ini_path: Path | None
    shaders_dir: Path | None
    presets: list[Path] = field(default_factory=list)
    base_path: Path | None = None
    error: str | None = None

    @property
    def installed(self) -> bool:
        """Convenience for callers that don't care about error vs not_installed."""
        return self.state == "installed"


# --- internals -------------------------------------------------------------

_FX_SECTION = re.compile(r"^\s*\[[^\]]+\.fx\]", re.IGNORECASE | re.MULTILINE)
_TECHNIQUES_KEY = re.compile(r"^\s*Techniques\s*=", re.IGNORECASE | re.MULTILINE)


def _dll_path(bin64: Path) -> Path | None:
    """Return the ReShade proxy DLL path if present, else None.

    Wrapped in its own tiny helper so tests can patch it to simulate IO
    failures at the earliest detection step.
    """
    for name in ("dxgi.dll", "d3d12.dll"):
        candidate = bin64 / name
        if candidate.is_file():
            return candidate
    return None


def _read_base_path(ini_path: Path, bin64: Path) -> Path:
    """Read [INSTALL] BasePath= from ReShade.ini. Falls back to bin64.

    ReShade's documented fallback is the application exe directory — which
    for Crimson Desert is bin64/.
    """
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return bin64
    base = parser.get("INSTALL", "BasePath", fallback="").strip()
    if not base:
        return bin64
    return Path(base)


def _is_preset_file(p: Path) -> bool:
    """A file is a preset iff it contains Techniques= OR a [*.fx] section.

    ReShade.ini has [GENERAL] / [INPUT] / [OVERLAY] / [INSTALL] / [ADDON] etc.
    but never [*.fx] and never Techniques= at the top level — it's a config,
    not a preset.
    """
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if _FX_SECTION.search(text):
        return True
    if _TECHNIQUES_KEY.search(text):
        return True
    return False


def _enumerate_presets(search_dir: Path, ini_path: Path | None) -> list[Path]:
    """Return sorted list of preset .ini files in search_dir, excluding
    ReShade.ini itself."""
    if not search_dir.is_dir():
        return []
    results: list[Path] = []
    ini_resolved = ini_path.resolve() if ini_path and ini_path.exists() else None
    for candidate in sorted(search_dir.glob("*.ini")):
        # Skip the main config file even if it happens to be in the search dir.
        if ini_resolved is not None and candidate.resolve() == ini_resolved:
            continue
        if _is_preset_file(candidate):
            results.append(candidate)
    return results


# --- public API ------------------------------------------------------------

def detect_reshade_install(game_dir: Path) -> ReshadeInstall:
    """Scan `<game_dir>/bin64/` and report ReShade install state.

    Never raises — IO exceptions are captured into the `error` field so the
    GUI always gets a renderable state.
    """
    bin64 = Path(game_dir) / "bin64"
    logger.debug("reshade_detect: scanning %s", bin64)

    try:
        if not bin64.is_dir():
            logger.debug("reshade_detect: bin64 missing -> not_installed")
            return ReshadeInstall(
                state="not_installed",
                dll_path=None, ini_path=None, shaders_dir=None,
                presets=[], base_path=None, error=None)

        dll = _dll_path(bin64)
        if dll is None:
            logger.debug("reshade_detect: no dxgi/d3d12.dll -> not_installed")
            return ReshadeInstall(
                state="not_installed",
                dll_path=None, ini_path=None, shaders_dir=None,
                presets=[], base_path=bin64, error=None)

        ini = bin64 / "ReShade.ini"
        ini_path = ini if ini.is_file() else None
        shaders = bin64 / "reshade-shaders"
        shaders_dir = shaders if shaders.is_dir() else None

        base_path = _read_base_path(ini, bin64) if ini_path else bin64
        logger.debug("reshade_detect: base_path=%s", base_path)

        presets = _enumerate_presets(base_path, ini_path)
        logger.debug("reshade_detect: found %d preset(s)", len(presets))

        return ReshadeInstall(
            state="installed",
            dll_path=dll, ini_path=ini_path, shaders_dir=shaders_dir,
            presets=presets, base_path=base_path, error=None)

    except Exception as e:  # noqa: BLE001 — intentional catch-all for UI safety
        msg = f"{type(e).__name__}: {e}"
        logger.warning("reshade_detect: IO failure: %s", msg)
        return ReshadeInstall(
            state="error",
            dll_path=None, ini_path=None, shaders_dir=None,
            presets=[], base_path=None, error=msg)
