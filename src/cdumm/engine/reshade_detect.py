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
    # List of ReShade add-on files found next to the proxy DLL. These are
    # `.addon32` / `.addon64` / `.addon` files — third-party extensions
    # like RenoDX, Ultra Limiter, Display Commander, etc. Their presence
    # means the user installed ReShade with Add-On Support (basic ReShade
    # won't load them).
    addons: list[Path] = field(default_factory=list)
    error: str | None = None

    @property
    def installed(self) -> bool:
        """Convenience for callers that don't care about error vs not_installed."""
        return self.state == "installed"

    @property
    def has_addon_support(self) -> bool:
        """True if the install has any ReShade add-on files present.

        Reliable signal (not a guess): basic ReShade (built without add-on
        support) doesn't load these files at all, so users who have them
        either:
          (a) installed ReShade via the Add-On Support setup exe, or
          (b) dropped the files in hoping they'd work on basic ReShade,
              in which case ReShade ignores them.
        Either way, surfacing "Add-ons detected" helps the user understand
        what's in their install.
        """
        return bool(self.addons)


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


def _enumerate_addons(bin64: Path) -> list[Path]:
    """Return sorted list of ReShade add-on files next to the DLL.

    Per the ReShade add-on docs (Marty's Mods "Manually Installing Addons"),
    addon files live in the same directory as the ReShade DLL and use the
    `.addon64` extension on 64-bit games (or `.addon32` / `.addon` for older
    or 32-bit installs). We match all three extensions.
    """
    if not bin64.is_dir():
        return []
    results: list[Path] = []
    try:
        for entry in sorted(bin64.iterdir()):
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext in (".addon64", ".addon32", ".addon"):
                results.append(entry)
    except OSError as e:
        logger.debug("_enumerate_addons: iterdir failed: %s", e)
        return []
    return results


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


def _enumerate_presets(search_dir: Path, ini_path: Path | None) -> list[Path]:
    """Return sorted list of preset .ini files directly inside `search_dir`.

    Flat scan only — intentionally does NOT recurse into subdirectories. On a
    user's real install, `bin64/` frequently contains extracted mod packs or
    tool output directories with hundreds of thousands of unrelated files;
    walking that on the UI thread blocks the main window for ~15 seconds.

    ReShade supports subfolder-organized preset packs, but the convention is
    to point ReShade's `[INSTALL] BasePath=` at the pack folder. When a user
    does that, `search_dir` already IS the pack folder and the flat scan
    picks up the pack's presets correctly.

    ReShade.ini (the main config) is excluded.
    """
    if not search_dir.is_dir():
        return []
    results: list[Path] = []
    ini_resolved = ini_path.resolve() if ini_path and ini_path.exists() else None

    try:
        entries = sorted(search_dir.glob("*.ini"))
    except OSError as e:
        logger.debug("_enumerate_presets: glob failed %s: %s", search_dir, e)
        return []

    for entry in entries:
        if ini_resolved is not None:
            try:
                if entry.resolve() == ini_resolved:
                    continue
            except OSError:
                pass
        if _is_preset_file(entry):
            results.append(entry)
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
        addons = _enumerate_addons(bin64)
        logger.debug("reshade_detect: %d preset(s), %d addon(s)",
                     len(presets), len(addons))

        return ReshadeInstall(
            state="installed",
            dll_path=dll, ini_path=ini_path, shaders_dir=shaders_dir,
            presets=presets, base_path=base_path, addons=addons, error=None)

    except Exception as e:  # noqa: BLE001 — intentional catch-all for UI safety
        msg = f"{type(e).__name__}: {e}"
        logger.warning("reshade_detect: IO failure: %s", msg)
        return ReshadeInstall(
            state="error",
            dll_path=None, ini_path=None, shaders_dir=None,
            presets=[], base_path=None, error=msg)
