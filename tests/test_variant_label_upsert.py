"""C-H8: saving selected_labels must NOT wipe custom_values in mod_config.

INSERT OR REPLACE deletes the entire row and recreates it with only
the named columns populated. If a variant mod also stores
editable custom values in the same row, those values silently vanish
on every Apply.

Codex P2 finding — confirmed as ship-stopping data loss.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdumm.storage.database import Database


def test_saving_labels_preserves_custom_values(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    try:
        # Pretend a variant mod already saved custom_values earlier.
        db.connection.execute(
            "INSERT INTO mods (id, name, mod_type, priority) "
            "VALUES (?, ?, 'paz', 1)",
            (42, "Test Variant Mod"))
        db.connection.execute(
            "INSERT INTO mod_config (mod_id, custom_values) VALUES (?, ?)",
            (42, json.dumps({"loot_multiplier": 3.5, "stamina_max": 999})))
        db.connection.commit()

        # Simulate the label-save path from update_variant_selection.
        from cdumm.engine.variant_handler import _persist_selected_labels
        _persist_selected_labels(
            db, 42, {"variantA": ["Label1", "Label2"]})

        # The custom_values saved earlier MUST still be there.
        row = db.connection.execute(
            "SELECT selected_labels, custom_values FROM mod_config "
            "WHERE mod_id = ?", (42,)).fetchone()
        assert row is not None, "mod_config row missing after label save"
        labels_stored = json.loads(row[0])
        assert labels_stored == {"variantA": ["Label1", "Label2"]}
        # This is the assertion that currently fails:
        cv_stored = json.loads(row[1]) if row[1] else None
        assert cv_stored == {"loot_multiplier": 3.5, "stamina_max": 999}, (
            f"custom_values was wiped by the label save! got {cv_stored}")
    finally:
        db.close()


def test_saving_labels_first_time_creates_row(tmp_path: Path):
    """When no mod_config row exists yet, saving labels must create it."""
    db = Database(tmp_path / "test.db")
    db.initialize()
    try:
        db.connection.execute(
            "INSERT INTO mods (id, name, mod_type, priority) "
            "VALUES (?, ?, 'paz', 1)",
            (42, "Test Variant Mod"))
        db.connection.commit()

        from cdumm.engine.variant_handler import _persist_selected_labels
        _persist_selected_labels(db, 42, {"v": ["L"]})

        row = db.connection.execute(
            "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
            (42,)).fetchone()
        assert row is not None
        assert json.loads(row[0]) == {"v": ["L"]}
    finally:
        db.close()


def test_saving_labels_twice_updates_not_appends(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    try:
        db.connection.execute(
            "INSERT INTO mods (id, name, mod_type, priority) "
            "VALUES (?, ?, 'paz', 1)",
            (42, "Test Variant Mod"))
        db.connection.commit()

        from cdumm.engine.variant_handler import _persist_selected_labels
        _persist_selected_labels(db, 42, {"v": ["L1"]})
        _persist_selected_labels(db, 42, {"v": ["L2"]})

        rows = db.connection.execute(
            "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
            (42,)).fetchall()
        assert len(rows) == 1, "a second save must UPDATE not INSERT"
        assert json.loads(rows[0][0]) == {"v": ["L2"]}
    finally:
        db.close()
