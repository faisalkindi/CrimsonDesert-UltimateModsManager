"""Game version detection via Steam build ID + exe hash fingerprinting."""
import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _cached_version(game_dir_str: str, exe_mtime_ns: int, exe_size: int) -> str | None:
    """LRU-cached inner computation. Cache invalidates when the game
    exe changes (Steam update = new mtime + size)."""
    return _compute_game_version(Path(game_dir_str))


def detect_game_version(game_dir: Path) -> str | None:
    """Return a version fingerprint for the current game installation.

    Uses Steam's buildid (most reliable — changes with every update),
    plus game exe hash for definitive change detection.

    Cached per-process based on exe mtime+size; a batch import of 40
    mods pays the cost once instead of 40 times.
    """
    try:
        exe = game_dir / "bin64" / "CrimsonDesert.exe"
        if exe.exists():
            st = exe.stat()
            return _cached_version(str(game_dir), st.st_mtime_ns, st.st_size)
        # Xbox/custom install — no stable cache key, fall through to
        # direct call (still returns None cleanly if nothing detects).
        return _compute_game_version(game_dir)
    except Exception as e:
        logger.warning("Could not detect game version: %s", e)
        return None


def _compute_game_version(game_dir: Path) -> str | None:
    """Do the actual fingerprint work. Called once per (game_dir, exe) pair."""
    try:
        parts = []

        # Primary: Steam build ID from appmanifest
        build_id = _get_steam_build_id(game_dir)
        if build_id:
            parts.append(f"buildid:{build_id}")

        # Secondary: game exe SHA256 (first 64KB — fast, catches any update)
        exe = game_dir / "bin64" / "CrimsonDesert.exe"
        if exe.exists():
            parts.append(f"exe:{_hash_exe_fast(exe)}")

        # Tertiary: a few PAMT sizes (catches content updates)
        for d in ["0000", "0001", "0002"]:
            pamt = game_dir / d / "0.pamt"
            if pamt.exists():
                parts.append(f"{d}:{pamt.stat().st_size}")

        if not parts:
            return None
        combined = "|".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
    except Exception as e:
        logger.warning("Could not detect game version: %s", e)
        return None


def _hash_exe_fast(exe_path: Path) -> str:
    """Hash the first 64KB + last 64KB + file size of the exe.

    Fast (~1ms) but catches any update. Full SHA256 of a 500MB+
    Denuvo exe would take seconds.
    """
    size = exe_path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(exe_path, 'rb') as f:
        h.update(f.read(65536))
        if size > 65536:
            f.seek(-65536, 2)
            h.update(f.read(65536))
    return h.hexdigest()[:12]


def get_steam_build_id(game_dir: Path) -> str | None:
    """Read Steam build ID from appmanifest file."""
    try:
        # game_dir is like .../steamapps/common/Crimson Desert
        steamapps = game_dir.parent.parent
        for acf in steamapps.glob("appmanifest_*.acf"):
            text = acf.read_text(errors="replace")
            if "Crimson Desert" not in text:
                continue
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('"buildid"'):
                    return line.split('"')[-2]
    except Exception:
        pass
    return None


# Keep old name for internal use
_get_steam_build_id = get_steam_build_id
