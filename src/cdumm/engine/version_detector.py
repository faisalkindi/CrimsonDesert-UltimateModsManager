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
    """Do the actual fingerprint work. Called once per (game_dir, exe) pair.

    The fingerprint must be STABLE across apply/revert cycles. Only
    inputs that change when the GAME itself is updated (Steam patch,
    file verify, manual replacement) may contribute. That means no
    PAZ/PAMT/PAPGT file measurements — those change every time
    CDUMM stages mods.

    Old versions included PAMT sizes for dirs 0000-0002, which
    flipped the fingerprint every apply/revert and made mod version
    tracking falsely report "imported on a different game version"
    for every mod. Removed in v3.1.7.
    """
    try:
        parts = []

        # Primary: Steam build ID from appmanifest — authoritative
        # "what version of the game is this" signal.
        build_id = _get_steam_build_id(game_dir)
        if build_id:
            parts.append(f"buildid:{build_id}")

        # Secondary: game exe SHA256 (first 64KB + last 64KB + size).
        # Catches patches where Steam shipped a new exe but didn't
        # bump build ID (rare but possible).
        exe = game_dir / "bin64" / "CrimsonDesert.exe"
        if exe.exists():
            parts.append(f"exe:{_hash_exe_fast(exe)}")

        if not parts:
            return None
        combined = "|".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
    except Exception as e:
        logger.warning("Could not detect game version: %s", e)
        return None


def stamp_enabled_mods_as_current(db, game_dir: Path) -> bool:
    """Stamp every ENABLED mod's ``game_version_hash`` with the
    current fingerprint.

    Called from the GUI after a successful apply (no errors). The
    logic is: if apply succeeded, the enabled mods by definition
    produced patches that landed on the current game bytes — so
    they're "last known-good" on this version. Stamp them.

    Leaves DISABLED mods alone: their hash should keep pointing at
    the last version they were confirmed-working on, not be
    overwritten by a random apply the user ran while they were off.

    No-ops silently if the detector can't produce a fingerprint
    (missing exe, etc.) so callers don't have to guard.

    Returns True if any row was updated.
    """
    try:
        current = detect_game_version(game_dir)
        if not current:
            return False
        cursor = db.connection.execute(
            "UPDATE mods SET game_version_hash = ? WHERE enabled = 1 "
            "AND game_version_hash IS NOT NULL "
            "AND game_version_hash != ?", (current, current))
        db.connection.commit()
        changed = cursor.rowcount
        if changed:
            logger.info(
                "stamp_enabled_mods_as_current: %d mod(s) stamped "
                "with %s", changed, current)
        return changed > 0
    except Exception as e:
        logger.warning("stamp_enabled_mods_as_current failed: %s", e)
        return False


def backfill_stored_fingerprints(db, game_dir: Path) -> bool:
    """One-time migration: overwrite the stored game_version_fingerprint
    and every mod's game_version_hash with the new-algorithm output.

    Without this, every install upgraded from a pre-v3.1.7 CDUMM
    would see its Post-Apply Verification dialog flag every single
    mod as "imported on a different game version" forever — the
    stored hashes reflect the OLD algorithm (which mixed in PAMT
    sizes), and there's no way to reproduce those values once the
    game state moves.

    Guarded by config key ``version_detector_v2``. Runs once per
    install and silently no-ops after that.

    Returns ``True`` if migration actually ran this call, ``False``
    if it was already done.
    """
    try:
        from cdumm.storage.config import Config
        cfg = Config(db)
        if cfg.get("version_detector_v2") == "1":
            return False

        new_fp = detect_game_version(game_dir)
        if not new_fp:
            # Can't compute a fingerprint (e.g. game_dir missing);
            # leave the flag unset so next launch can retry.
            logger.info(
                "backfill_stored_fingerprints: detector returned "
                "None, deferring migration")
            return False

        db.connection.execute(
            "UPDATE mods SET game_version_hash = ? "
            "WHERE game_version_hash IS NOT NULL", (new_fp,))
        cfg.set("game_version_fingerprint", new_fp)
        cfg.set("version_detector_v2", "1")
        db.connection.commit()
        logger.info(
            "backfill_stored_fingerprints: migrated to new-algorithm "
            "fingerprint %s", new_fp)
        return True
    except Exception as e:
        logger.warning(
            "backfill_stored_fingerprints failed: %s", e)
        return False


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
