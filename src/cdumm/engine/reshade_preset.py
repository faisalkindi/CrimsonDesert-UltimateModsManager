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
import re
import tempfile
from pathlib import Path

import psutil

from cdumm.engine.ini_line_editor import replace_key_in_section

logger = logging.getLogger(__name__)

GAME_EXE_DEFAULT = "CrimsonDesert.exe"


def _strip_quotes(value: str) -> str:
    """Strip matching leading/trailing quotes from an INI value."""
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


# ---- Path resolution ------------------------------------------------------

def resolve_preset_path(
    base_path: Path | None,
    bin64_dir: Path,
    value: str,
) -> Path:
    """Resolve a raw `PresetPath=` string to an absolute Path.

    Mirrors ReShade's own rule: absolute paths are returned as-is; relative
    paths resolve against `[INSTALL] BasePath=` (or `bin64_dir` if unset).
    Handles defensively-quoted values like `PresetPath="C:/presets/foo.ini"`.
    """
    stripped = _strip_quotes(value)
    p = Path(stripped)
    if p.is_absolute():
        return p
    base = base_path if base_path is not None else bin64_dir
    return base / p


# ---- Reading --------------------------------------------------------------

_PRESETPATH_LINE = re.compile(
    r"^\s*PresetPath\s*=\s*(?P<value>.*?)\s*(?:\r?\n|\r|$)",
    re.IGNORECASE | re.MULTILINE,
)
_GENERAL_HEADER = re.compile(r"^\s*\[\s*GENERAL\s*\]\s*$",
                              re.IGNORECASE | re.MULTILINE)
_ANY_HEADER = re.compile(r"^\s*\[[^\]]+\]\s*$", re.MULTILINE)


def read_active_preset_raw(ini_path: Path) -> str | None:
    """Return the raw text value of `[GENERAL] PresetPath=` exactly as written
    (trailing whitespace stripped, but internal characters preserved),
    or None if missing / file unreadable.

    We bypass configparser for this read so that line-precise fidelity is
    preserved — the value we return is what `set_active_preset` will write
    back during Revert, so it needs to be exact.
    """
    try:
        text = ini_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("read_active_preset_raw: read failed: %s", e)
        return None

    # Find the [GENERAL] section bounds.
    gen_header = _GENERAL_HEADER.search(text)
    if gen_header is None:
        return None
    section_start = gen_header.end()
    # Section ends at next [header] or EOF.
    next_header = _ANY_HEADER.search(text, pos=section_start)
    section_end = next_header.start() if next_header else len(text)
    section_text = text[section_start:section_end]

    match = _PRESETPATH_LINE.search(section_text)
    if match is None:
        return None
    return match.group("value")


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
    Atomic: writes to a sibling temp file and then renames over the target,
    so power loss mid-write can't leave a half-written ReShade.ini.
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
    _atomic_write_text(ini_path, new_text)
    logger.info("ReShade preset switched: %r -> %r (ini=%s)",
                previous, preset_value, ini_path)
    return previous


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file + rename.

    Steps:
      1. Write to `<path>.<random>.tmp` in the same directory (same filesystem
         guarantees the rename is atomic on Windows/NTFS and POSIX).
      2. Flush + fsync so the bytes hit the disk before we rename.
      3. `os.replace` to atomically swap the new file into place.

    If any step raises, the original file is untouched.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile with delete=False so we can close and rename it.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't critical; best-effort.
                pass
        os.replace(tmp_name, str(path))
    except Exception:
        # Clean up the temp file on any failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---- Running-game guard ---------------------------------------------------

def is_game_running(
    game_exe_name: str = GAME_EXE_DEFAULT,
    bin64_dir: Path | None = None,
) -> bool:
    """Return True if the game appears to be running.

    Matching is best-effort and supports three scenarios:
      1. If `bin64_dir` is given, any running process whose executable path
         starts with `bin64_dir` counts as "the game" — this catches both the
         Steam `CrimsonDesert.exe` and the Xbox Game Pass variant (which may
         use a different exe name inside `WindowsApps\\...\\bin64\\`).
      2. Fallback: case-insensitive name match against `game_exe_name`.
      3. If psutil itself fails (rare, some sandboxed environments), return
         False so the UI is lenient rather than blocking all writes.

    Tolerates per-process lookup failures — a process we can't inspect just
    means that one process wasn't readable, not that the scan should fail.
    """
    target_name = game_exe_name.lower()
    target_dir: str | None = None
    if bin64_dir is not None:
        try:
            target_dir = os.path.normcase(os.path.normpath(str(bin64_dir.resolve(strict=False))))
        except Exception:  # noqa: BLE001
            target_dir = None

    try:
        iterator = psutil.process_iter(["name", "exe"])
    except Exception as e:  # noqa: BLE001 — psutil edge cases on some systems
        logger.debug("is_game_running: process_iter failed: %s", e)
        return False

    for proc in iterator:
        try:
            info = proc.info if isinstance(proc.info, dict) else {}
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:  # noqa: BLE001
            continue

        name = info.get("name") or ""
        exe = info.get("exe") or ""

        # Preferred match: the process's exe lives inside bin64_dir.
        if target_dir and exe:
            try:
                exe_norm = os.path.normcase(os.path.normpath(exe))
                if exe_norm.startswith(target_dir + os.sep) or exe_norm == target_dir:
                    logger.debug("is_game_running: process in bin64: %s", exe)
                    return True
            except Exception:  # noqa: BLE001
                pass

        # Fallback match: name equality.
        if name and name.lower() == target_name:
            logger.debug("is_game_running: name match: %s", name)
            return True

    return False


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
