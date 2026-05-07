"""Helper to resolve the CDMods/ root directory.

By default CDMods/ lives at game_dir/CDMods/ (next to the game install).
Users can override via the cdmods_path config key , useful when the
game is on a small drive but the user wants mod backups on a bigger
drive.

The helper falls back to the default in three cases:
  * No config provided
  * Config key not set
  * Config key set but the path doesn't exist (silent self-heal so a
    deleted override location doesn't break apply)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdumm.storage.config import Config

logger = logging.getLogger(__name__)


def get_cdmods_root(config: "Config | None", game_dir: Path) -> Path:
    """Return the directory CDUMM should use for sources / vanilla /
    deltas / cdumm.db.

    Resolution order:
      1. config['cdmods_path'] when set, non-empty, and pointing at
         an existing directory.
      2. game_dir / 'CDMods' otherwise.
    """
    if config is None:
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
