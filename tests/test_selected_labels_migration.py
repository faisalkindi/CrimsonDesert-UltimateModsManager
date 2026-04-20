"""C-H9: old DBs predating the selected_labels column must migrate cleanly.

The column exists in CREATE TABLE at database.py:53 but there is no
ALTER migration like the one for custom_values. Users with DBs created
before the column was added hit 'no such column: selected_labels' at
runtime. Mirror the custom_values migration.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cdumm.storage.database import Database


def _make_old_db_without_column(db_path: Path) -> None:
    """Create a DB in the shape CDUMM used BEFORE the selected_labels
    column existed — just mod_config(mod_id, custom_values).
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE mods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, mod_type TEXT DEFAULT 'paz', priority INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE mod_config (
            mod_id INTEGER PRIMARY KEY REFERENCES mods(id),
            custom_values TEXT
        )
    """)
    conn.commit()
    conn.close()


def test_initialize_migrates_missing_selected_labels_column(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _make_old_db_without_column(db_path)

    db = Database(db_path)
    db.initialize()
    try:
        cols = {
            row[1] for row in
            db.connection.execute("PRAGMA table_info(mod_config)").fetchall()
        }
        assert "selected_labels" in cols, (
            f"migration did not add selected_labels column; got {cols}")
        # And writing to it works without errors:
        db.connection.execute(
            "INSERT INTO mods (id, name) VALUES (?, ?)", (1, "Test"))
        db.connection.execute(
            "INSERT INTO mod_config (mod_id, selected_labels) VALUES (?, ?)",
            (1, '{"variantA": ["Label"]}'))
        db.connection.commit()
    finally:
        db.close()


def test_initialize_preserves_existing_selected_labels_data(tmp_path: Path):
    """If the column ALREADY exists and has data, migration is a no-op."""
    db = Database(tmp_path / "fresh.db")
    db.initialize()
    try:
        db.connection.execute(
            "INSERT INTO mods (id, name, mod_type, priority) "
            "VALUES (1, 'x', 'paz', 1)")
        db.connection.execute(
            "INSERT INTO mod_config (mod_id, selected_labels) VALUES "
            "(1, '{\"v\": [\"keep_me\"]}')")
        db.connection.commit()
    finally:
        db.close()

    # Re-open (simulating restart) and confirm data survived.
    db2 = Database(tmp_path / "fresh.db")
    db2.initialize()
    try:
        row = db2.connection.execute(
            "SELECT selected_labels FROM mod_config WHERE mod_id = 1"
        ).fetchone()
        assert row is not None
        assert "keep_me" in row[0]
    finally:
        db2.close()
