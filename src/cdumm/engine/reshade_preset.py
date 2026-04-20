"""ReShade preset read/write + supporting helpers.

Functions:
  resolve_preset_path       — resolve a raw PresetPath= string to an absolute Path
  read_active_preset_raw    — return [GENERAL] PresetPath= as written (str | None)
  read_active_preset        — convenience wrapper returning the resolved Path
  set_active_preset         — line-surgical write; returns previous raw value
  read_preset_sections      — parse a preset file into {section: {key: value}}
  is_game_running           — name-match CrimsonDesert.exe via psutil
  same_preset               — Windows-safe path comparison (normcase + normpath + resolve)

All writes to ReShade.ini go through `engine/ini_line_editor.py` so user and
installer comments are preserved.
"""
from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

import psutil

from cdumm.engine.ini_line_editor import replace_key_in_section

logger = logging.getLogger(__name__)

GAME_EXE_DEFAULT = "CrimsonDesert.exe"


# ---- Path resolution ------------------------------------------------------

def resolve_preset_path(
    base_path: Path | None,
    bin64_dir: Path,
    value: str,
) -> Path:
    """Resolve a raw `PresetPath=` string to an absolute Path.

    Mirrors ReShade's own rule: absolute paths are returned as-is; relative
    paths resolve against `[INSTALL] BasePath=` (or `bin64_dir` if unset).
    """
    p = Path(value)
    if p.is_absolute():
        return p
    base = base_path if base_path is not None else bin64_dir
    return base / p


# ---- Reading --------------------------------------------------------------

def read_active_preset_raw(ini_path: Path) -> str | None:
    """Return the raw text value of `[GENERAL] PresetPath=` as configparser sees
    it (whitespace-trimmed), or None if missing / file unreadable."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, configparser.Error) as e:
        logger.debug("read_active_preset_raw: parse failed: %s", e)
        return None
    value = parser.get("GENERAL", "PresetPath", fallback=None)
    if value is None:
        return None
    return value


def read_active_preset(
    ini_path: Path,
    base_path: Path | None,
    bin64_dir: Path,
) -> Path | None:
    """Read the current `PresetPath=` and return it resolved to an absolute Path."""
    raw = read_active_preset_raw(ini_path)
    if raw is None or not raw.strip():
        return None
    return resolve_preset_path(base_path, bin64_dir, raw.strip())


def read_preset_sections(preset_path: Path) -> dict[str, dict[str, str]]:
    """Parse a preset file into {section_name: {key: value}}.

    Preset files are machine-written; comment preservation isn't required.
    """
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(preset_path, encoding="utf-8")
    except (OSError, configparser.Error) as e:
        logger.debug("read_preset_sections: parse failed: %s", e)
        return {}
    return {sec: dict(parser.items(sec)) for sec in parser.sections()}


# ---- Writing --------------------------------------------------------------

def set_active_preset(ini_path: Path, preset_value: str) -> str:
    """Rewrite `[GENERAL] PresetPath=` to `preset_value`.

    `preset_value` is written verbatim — caller chooses absolute or relative.
    Returns the previous raw string (empty string if the key didn't exist yet).

    Line-surgical: comments, blank lines, and other keys are preserved.
    Logs the transition at INFO level for bug-report traceability.
    """
    previous = read_active_preset_raw(ini_path) or ""

    try:
        text = ini_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError:
        raise  # propagate to caller (e.g. PermissionError on read-only ini)

    new_text = replace_key_in_section(text, "GENERAL", "PresetPath", preset_value)
    ini_path.write_text(new_text, encoding="utf-8")
    logger.info("ReShade preset switched: %r -> %r (ini=%s)",
                previous, preset_value, ini_path)
    return previous


# ---- Running-game guard ---------------------------------------------------

def is_game_running(game_exe_name: str = GAME_EXE_DEFAULT) -> bool:
    """Return True if any running process matches `game_exe_name` (case-insensitive).

    Tolerates per-process lookup failures (NoSuchProcess, AccessDenied) — those
    just mean that one process wasn't readable, not that the scan should fail.
    """
    target = game_exe_name.lower()
    running = False
    try:
        iterator = psutil.process_iter(["name"])
    except Exception as e:  # noqa: BLE001 — psutil edge cases on some systems
        logger.debug("is_game_running: process_iter failed: %s", e)
        return False

    for proc in iterator:
        try:
            name = proc.info.get("name", "") if isinstance(proc.info, dict) else ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:  # noqa: BLE001
            continue
        if name and name.lower() == target:
            running = True
            break
    if running:
        logger.debug("is_game_running: %s detected", game_exe_name)
    return running


# ---- Path comparison ------------------------------------------------------

def same_preset(a: Path, b: Path) -> bool:
    """Windows-safe path equality.

    Applies normcase (case-folding) + normpath (collapse `./` and mixed
    separators) + resolve(strict=False) (symlinks + absolutizing) so two
    references to the same on-disk file compare equal even if spelled differently.
    """
    def _canonical(p: Path) -> str:
        try:
            resolved = p.resolve(strict=False)
        except Exception:  # noqa: BLE001
            resolved = p
        return os.path.normcase(os.path.normpath(str(resolved)))
    return _canonical(a) == _canonical(b)
