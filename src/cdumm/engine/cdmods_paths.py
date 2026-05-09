"""Helper to resolve the CDMods/ root directory.

By default CDMods/ lives at game_dir/CDMods/ (next to the game install).
Users can override via the cdmods_path config key , useful when the
game is on a small drive but the user wants mod backups on a bigger
drive.

The helper falls back to the default in three cases:
  * No config provided AND no pointer file
  * Config key not set
  * Config key set but the path doesn't exist (silent self-heal so a
    deleted override location doesn't break apply)

Pointer file (bootstrap-time fallback)
--------------------------------------

The override config key lives INSIDE cdumm.db, which is itself stored
under the override path. This is a chicken-and-egg problem on launch:
to find the DB we need to know the override, but the override is
inside the DB. Without a hint, bootstrap silently creates an empty
CDMods/ at the default location and the user's library appears wiped.

Fix: a tiny pointer file at %LOCALAPPDATA%/cdumm/cdmods_path.txt that
shadows the config setting. Written alongside every config write,
read by main.py / cli.py before opening the DB. Same pattern as the
existing game_dir.txt at the same location.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdumm.storage.config import Config

logger = logging.getLogger(__name__)

# Mirror of main.APP_DATA_DIR. Resolved through ``cdumm.platform`` so
# the per-user state directory lands in the right place per platform
# (``~/AppData/Local/cdumm`` on Windows, ``~/Library/Application
# Support/cdumm`` on macOS, ``$XDG_DATA_HOME/cdumm`` on Linux). The
# helper avoids importing main.py because that would drag PySide6 in
# for headless cli.py callers; ``cdumm.platform`` is stdlib-only.
# Tests monkeypatch this attribute to redirect the pointer file.
from cdumm.platform import app_data_dir as _resolve_app_data_dir

_APP_DATA_DIR = _resolve_app_data_dir()
_POINTER_FILENAME = "cdmods_path.txt"


def _pointer_file() -> Path:
    return _APP_DATA_DIR / _POINTER_FILENAME


def write_cdmods_path_pointer(path: Path) -> None:
    """Persist ``path`` to %LOCALAPPDATA%/cdumm/cdmods_path.txt so the
    next CDUMM launch can find the override location BEFORE opening
    the DB. Called from settings_page after every config write.

    Failures are logged but never raised , the config DB is still the
    authoritative source; the pointer file is just a bootstrap hint.
    """
    pointer = _pointer_file()
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(path), encoding="utf-8")
    except OSError as e:
        logger.warning(
            "cdmods_path pointer write failed (%s); next launch may "
            "miss the override and create an empty CDMods at the "
            "default location", e)


def read_cdmods_path_pointer() -> Path | None:
    """Read the pointer file written by ``write_cdmods_path_pointer``.

    Returns None when the pointer is missing, empty, unreadable, or
    points at a path that no longer exists. Callers fall back to the
    default behavior in those cases , the pointer is only a hint, not
    a contract.
    """
    pointer = _pointer_file()
    if not pointer.exists():
        return None
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.debug("cdmods_path pointer read failed: %s", e)
        return None
    if not raw:
        return None
    p = Path(raw)
    if not p.exists() or not p.is_dir():
        logger.debug(
            "cdmods_path pointer %r is stale; ignoring", raw)
        return None
    return p


def get_cdmods_root(config: "Config | None", game_dir: Path) -> Path:
    """Return the directory CDUMM should use for sources / vanilla /
    deltas / cdumm.db.

    Resolution order:
      1. config['cdmods_path'] when set, non-empty, and pointing at
         an existing directory.
      2. When config is None: pointer file at
         %LOCALAPPDATA%/cdumm/cdmods_path.txt (bootstrap fallback).
      3. game_dir / 'CDMods' otherwise.
    """
    if config is None:
        pointer_target = read_cdmods_path_pointer()
        if pointer_target is not None:
            return pointer_target
        return Path(game_dir) / "CDMods"
    try:
        override = config.get("cdmods_path")
    except Exception as e:
        logger.debug("get_cdmods_root: config lookup failed: %s", e)
        return Path(game_dir) / "CDMods"
    if not override:
        return Path(game_dir) / "CDMods"
    p = Path(override)
    if not p.exists() or not p.is_dir():
        logger.warning(
            "cdmods_path override %r does not exist; falling back "
            "to default at %s/CDMods", str(p), game_dir,
        )
        return Path(game_dir) / "CDMods"
    return p
