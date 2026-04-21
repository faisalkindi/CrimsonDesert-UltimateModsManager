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


def _strip_quotes(value: str) -> str:
    """Strip matching leading/trailing quotes from an INI value."""
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _read_base_path(ini_path: Path, bin64: Path) -> Path:
    """Read [INSTALL] BasePath= from ReShade.ini. Falls back to bin64.

    ReShade's documented fallback is the application exe directory — which
    for Crimson Desert is bin64/. Relative BasePath values are resolved
    against bin64 (ReShade's own convention for relative paths is the exe
    directory as the base).
    """
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return bin64
    base = _strip_quotes(parser.get("INSTALL", "BasePath", fallback=""))
    if not base:
        return bin64
    base_path = Path(base)
    if not base_path.is_absolute():
        base_path = (bin64 / base_path).resolve(strict=False)
    return base_path


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


_PRESET_RECURSE_MAX_DEPTH = 4  # sensible cap; users don't nest deeper


def _enumerate_presets(search_dir: Path, ini_path: Path | None) -> list[Path]:
    """Return sorted list of preset .ini files under `search_dir`, recursing
    into subdirectories up to `_PRESET_RECURSE_MAX_DEPTH` levels deep.

    ReShade supports subfolder-organized preset packs (verified via crosire's
    own tutorial on sub-folder presets). Flat-scan-only would miss them.

    ReShade.ini (the main config) is excluded wherever it appears in the tree.
    Presets are sorted by path so the display order is stable.
    """
    if not search_dir.is_dir():
        return []
    results: list[Path] = []
    ini_resolved = ini_path.resolve() if ini_path and ini_path.exists() else None

    def _walk(directory: Path, depth: int) -> None:
        if depth > _PRESET_RECURSE_MAX_DEPTH:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError as e:
            logger.debug("_enumerate_presets: iterdir failed %s: %s", directory, e)
            return
        for entry in entries:
            if entry.is_dir():
                # Skip ReShade's own shader directory to avoid scanning thousands
                # of .ini files users don't care about as "presets".
                if entry.name.lower() in ("reshade-shaders", "reshade-addons"):
                    continue
                _walk(entry, depth + 1)
                continue
            if entry.suffix.lower() != ".ini":
                continue
            if ini_resolved is not None:
                try:
                    if entry.resolve() == ini_resolved:
                        continue
                except OSError:
                    pass
            if _is_preset_file(entry):
                results.append(entry)

    _walk(search_dir, 0)
    results.sort()
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
