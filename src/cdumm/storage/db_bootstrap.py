"""DB bootstrap + recovery helper.

Decides which file should back the CDUMM database, with rolling
backups so a torn write or game crash mid-flush cannot wipe the
user's mod list / theme / snapshot record.

unqltango Nexus 2026-05-10 (CDUMM v3.2.15) reported a Crimson
Desert crash followed by CDUMM coming up in "almost new state":
empty mod list, snapshot prompt, theme reset to light, only the
ASI mods (which live in ``bin64/`` outside the DB) survived.
Investigation found two compounding failure modes:

* A real install with a handful of mods is around 140 KB on disk,
  well below the old 200 KB ``FRESH_DB_THRESHOLD``. The bootstrap
  classified it as "empty", which either silently copied a stale
  legacy AppData DB on top, or fell through to SQLite which
  treats a 0-byte truncated file as a brand-new database.
* No backup. Once the live file was gone the user's data was gone.

Defensive contract now enforced:

1. Try to open the live DB. If it has any rows in ``mods``,
   ``config``, or ``snapshots``, treat it as valid regardless of
   file size and never clobber it.
2. If invalid (zero bytes, corrupt, empty schema), prefer the
   sibling rolling backup ``cdumm.db.bak1`` (then ``.bak2``) over
   the legacy AppData migration source.
3. After a successful resolution, always rotate
   ``cdumm.db.bak1`` -> ``cdumm.db.bak2`` and copy the live file
   to ``cdumm.db.bak1`` using SQLite's online ``.backup()`` API
   so an in-flight write is never captured mid-transaction.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Legacy migration size threshold from the v1.7 bump. Kept only for
# the AppData fallback branch; the live-DB validity check uses a
# real SQLite probe instead.
FRESH_DB_THRESHOLD = 200_000

# Sibling rolling-backup file names. ``.bak1`` is the most recent
# known-good copy; ``.bak2`` is the previous one.
_BAK1_SUFFIX = ".bak1"
_BAK2_SUFFIX = ".bak2"


def legacy_appdata_db_paths(app_data_dir: Path) -> list[Path]:
    """Legacy AppData DB locations checked during bootstrap."""
    return [
        app_data_dir / "cdumm.db",
        Path.home() / "AppData" / "Local" / "cdmm" / "cdumm.db",
    ]


@dataclass
class BootstrapResult:
    """Outcome of ``resolve_db_path``.

    ``db_path``        the path the caller should hand to ``Database``
    ``migrated_from``  set when a legacy AppData DB was copied forward
    ``recovered_from`` set when a sibling rolling backup healed a torn
                       or 0-byte live DB.
    """
    db_path: Path
    migrated_from: Optional[Path] = None
    recovered_from: Optional[Path] = None


def _live_db_has_data(path: Path) -> bool:
    """Return True when ``path`` is a SQLite file with at least one
    row in ``mods``, ``config``, or ``snapshots``. A freshly-
    initialised but empty schema returns False, as does a 0-byte
    file or anything SQLite refuses to open.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(path)
    except sqlite3.DatabaseError:
        return False
    try:
        for table in ("mods", "config", "snapshots"):
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()
            except sqlite3.DatabaseError:
                return False
            if row and row[0] > 0:
                return True
        return False
    finally:
        conn.close()


def _restore_from(src: Path, dst: Path) -> None:
    """Replace ``dst`` with ``src``'s bytes. ``copy2`` so mtimes
    propagate (helps the next bootstrap's diagnostics)."""
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)


def _write_rolling_backup(live: Path) -> None:
    """Rotate ``live.bak1 -> live.bak2`` and copy live -> bak1.

    Uses SQLite's online ``.backup()`` API rather than a raw file
    copy so an in-flight write is never captured mid-transaction.
    Best-effort: any exception is logged and swallowed because a
    backup failure must not block the user from launching CDUMM.
    """
    bak1 = live.with_suffix(live.suffix + _BAK1_SUFFIX)
    bak2 = live.with_suffix(live.suffix + _BAK2_SUFFIX)
    try:
        if bak1.exists():
            if bak2.exists():
                bak2.unlink()
            shutil.copy2(bak1, bak2)
        src = sqlite3.connect(live)
        try:
            dst = sqlite3.connect(bak1)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning(
            "rolling backup of %s failed: %s; previous backup retained",
            live, e)


def resolve_db_path(
    cdmods_dir: Path,
    app_data_dir: Path,
) -> BootstrapResult:
    """Pick the file that should back the CDUMM database.

    Resolution order:

    1. Live ``CDMods/cdumm.db`` if it has any user data.
    2. Sibling ``cdumm.db.bak1`` if the live DB is missing/torn.
    3. Sibling ``cdumm.db.bak2`` if ``.bak1`` is also unusable.
    4. Legacy AppData copy via the v1.7 migration path.
    5. Otherwise, fresh path returned for SQLite to initialise.

    On success a rolling backup is written next to the live DB so
    the next launch can recover from a torn write.
    """
    cdmods_dir.mkdir(parents=True, exist_ok=True)
    new_db = cdmods_dir / "cdumm.db"

    if _live_db_has_data(new_db):
        _write_rolling_backup(new_db)
        return BootstrapResult(db_path=new_db)

    # Live DB is missing, zero, corrupt, or freshly-empty.
    bak1 = new_db.with_suffix(new_db.suffix + _BAK1_SUFFIX)
    bak2 = new_db.with_suffix(new_db.suffix + _BAK2_SUFFIX)
    for candidate in (bak1, bak2):
        if _live_db_has_data(candidate):
            logger.info(
                "Live DB at %s unusable; recovering from sibling "
                "backup %s", new_db, candidate)
            _restore_from(candidate, new_db)
            _write_rolling_backup(new_db)
            return BootstrapResult(
                db_path=new_db, recovered_from=candidate)

    # Legacy AppData fallback (pre-v1.7 installs). Only triggers
    # when neither rolling backup survives — covers users who have
    # never run a CDUMM build with the rolling-backup feature.
    for old_db in legacy_appdata_db_paths(app_data_dir):
        if (old_db.exists()
                and old_db.stat().st_size > FRESH_DB_THRESHOLD):
            if new_db.exists():
                new_db.unlink()
            shutil.copy2(old_db, new_db)
            logger.info(
                "Migrated database from %s to %s", old_db, new_db)
            _write_rolling_backup(new_db)
            return BootstrapResult(
                db_path=new_db, migrated_from=old_db)

    return BootstrapResult(db_path=new_db)
