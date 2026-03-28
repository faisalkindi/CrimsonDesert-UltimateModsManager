"""Game version detection via PAPGT fingerprinting."""
import logging
from pathlib import Path

from cdumm.engine.snapshot_manager import hash_file

logger = logging.getLogger(__name__)


def detect_game_version(game_dir: Path) -> str | None:
    """Return a version fingerprint for the current game installation.

    Uses the hash of meta/0.papgt since it changes with every game update
    and no mod should ship a pre-built PAPGT (CDUMM rebuilds it).
    """
    papgt = game_dir / "meta" / "0.papgt"
    if not papgt.exists():
        return None
    try:
        h, _ = hash_file(papgt)
        return h[:16]  # short fingerprint
    except Exception as e:
        logger.warning("Could not detect game version: %s", e)
        return None
