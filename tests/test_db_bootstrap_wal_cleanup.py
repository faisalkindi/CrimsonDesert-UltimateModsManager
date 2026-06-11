"""Restoring a DB backup must clear stale -wal / -shm sidecars.

The CDUMM database runs in WAL mode. When ``_restore_from`` heals a
torn ``cdumm.db`` from a rolling backup, any leftover ``cdumm.db-wal``
/ ``cdumm.db-shm`` belongs to the OLD (corrupt) file; SQLite would
replay that WAL on top of the freshly restored database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cdumm.storage.db_bootstrap import _restore_from, resolve_db_path


def test_restore_from_deletes_stale_sidecars(tmp_path: Path) -> None:
    src = tmp_path / "cdumm.db.bak1"
    src.write_bytes(b"backup-bytes")
    dst = tmp_path / "cdumm.db"
    dst.write_bytes(b"torn")
    wal = tmp_path / "cdumm.db-wal"
    shm = tmp_path / "cdumm.db-shm"
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    _restore_from(src, dst)

    assert dst.read_bytes() == b"backup-bytes"
    assert not wal.exists(), "stale -wal would be replayed onto the restore"
    assert not shm.exists(), "stale -shm must go with the -wal"


def _seed_backup_with_data(path: Path) -> None:
    """Minimal DB that passes _live_db_has_data (needs the mods,
    config, and snapshots tables; at least one row in one of them)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, file_path TEXT)")
        conn.execute("INSERT INTO config VALUES ('theme', 'dark')")
        conn.commit()
    finally:
        conn.close()


def test_resolve_db_path_recovery_clears_sidecars(tmp_path: Path) -> None:
    cdmods = tmp_path / "CDMods"
    cdmods.mkdir()
    live = cdmods / "cdumm.db"
    live.write_bytes(b"corrupt garbage, not sqlite")
    (cdmods / "cdumm.db-wal").write_bytes(b"stale wal")
    (cdmods / "cdumm.db-shm").write_bytes(b"stale shm")
    _seed_backup_with_data(cdmods / "cdumm.db.bak1")

    result = resolve_db_path(cdmods, tmp_path / "appdata")

    assert result.recovered_from is not None
    assert not (cdmods / "cdumm.db-wal").exists()
    assert not (cdmods / "cdumm.db-shm").exists()
    # Restored DB must actually open with the backup's data.
    conn = sqlite3.connect(result.db_path)
    try:
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'theme'").fetchone()
    finally:
        conn.close()
    assert row == ("dark",)
