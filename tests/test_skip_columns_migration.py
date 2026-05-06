"""Skipped-mod-badge plumbing, chunk 1C: the mods table needs two
new columns to persist per-mod skip results across sessions:

- last_apply_skipped_count INTEGER DEFAULT 0
- last_apply_skip_summary TEXT  (JSON: list of {file, label, reason})

These let the mod card render a persistent yellow badge when a
recent apply skipped patches from this mod, surviving the toast
dismissal that's the current only signal.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_mods_table_has_skip_columns(tmp_path: Path):
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()

    cols = {c[1] for c in db.connection.execute(
        "PRAGMA table_info(mods)").fetchall()}

    assert "last_apply_skipped_count" in cols, (
        "mods table missing last_apply_skipped_count column. "
        "This persists how many JSON patches from this mod were "
        "skipped on the most recent Apply, used to render the "
        "post-apply skipped-mod badge."
    )
    assert "last_apply_skip_summary" in cols, (
        "mods table missing last_apply_skip_summary column. "
        "This stores a JSON list of {file, label, reason} entries "
        "for the badge tooltip."
    )

    db.close()


def test_default_values_for_new_mod(tmp_path: Path):
    """A freshly-inserted mod row must default to 0 / NULL for the
    new columns so existing import code paths don't need updating."""
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority) "
        "VALUES (1, 'X', 'paz', 1)"
    )
    db.connection.commit()

    row = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id=1"
    ).fetchone()
    assert row[0] == 0, f"last_apply_skipped_count default should be 0, got {row[0]!r}"
    assert row[1] is None, f"last_apply_skip_summary default should be NULL, got {row[1]!r}"

    db.close()


def test_migration_idempotent_on_existing_db(tmp_path: Path):
    """Running initialize() on a DB that already has the columns must
    not error (PRAGMA table_info gate)."""
    from cdumm.storage.database import Database

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()
    db.close()

    # Re-open and re-initialize
    db2 = Database(db_path)
    db2.initialize()
    cols = {c[1] for c in db2.connection.execute(
        "PRAGMA table_info(mods)").fetchall()}
    assert "last_apply_skipped_count" in cols
    db2.close()
