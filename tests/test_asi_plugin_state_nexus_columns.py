"""Bug #25 + #26: ``asi_plugin_state`` lacks ``nexus_real_file_id``
and ``nexus_last_checked_at`` columns. Consequences:

- nxm:// download of an ASI plugin can't persist the actual Nexus
  file_id, so the next update check can't walk the file_updates
  chain for that plugin — name-match forever.
- Feed-skip optimisation never applies to ASI plugins (hardcoded to
  last_checked=0 in the call sites), wasting API quota.

This test pins the schema migration.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


def test_asi_plugin_state_has_nexus_real_file_id_column(tmp_path):
    """Fresh DB + initialize() must produce an asi_plugin_state table
    that carries nexus_real_file_id (INTEGER)."""
    from cdumm.storage.database import Database
    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    cols = [
        r[1] for r in db.connection.execute(
            "PRAGMA table_info(asi_plugin_state)").fetchall()
    ]
    assert "nexus_real_file_id" in cols, (
        f"asi_plugin_state must carry nexus_real_file_id; got {cols}")


def test_asi_plugin_state_has_nexus_last_checked_at_column(tmp_path):
    from cdumm.storage.database import Database
    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    cols = [
        r[1] for r in db.connection.execute(
            "PRAGMA table_info(asi_plugin_state)").fetchall()
    ]
    assert "nexus_last_checked_at" in cols, (
        f"asi_plugin_state must carry nexus_last_checked_at; "
        f"got {cols}")


def test_migration_is_idempotent(tmp_path):
    """Running initialize twice must not error (ALTER TABLE ADD
    COLUMN fails if the column already exists)."""
    from cdumm.storage.database import Database
    db1 = Database(tmp_path / "cdumm.db")
    db1.initialize()
    db1.close()
    # Second initialise on the same file path.
    db2 = Database(tmp_path / "cdumm.db")
    db2.initialize()  # must not raise
