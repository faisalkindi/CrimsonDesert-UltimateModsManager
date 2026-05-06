"""Skipped-mod-badge plumbing, chunk 2A: after Apply collects
patch_skips with `_source_mod_id` (chunk 1 work), the apply
pipeline must persist per-mod skip counts and a JSON summary on
the mods table so the UI can render a persistent badge after the
toast dismisses.

The helper takes a patch_skips list and the set of mods that
participated in this apply, and writes:
- mods.last_apply_skipped_count = count for that mod (0 if no skips)
- mods.last_apply_skip_summary = JSON list of {file, label, reason}
                                 or NULL when count == 0

Mods that participated AND had skips get their state set.
Mods that participated AND had NO skips get reset to 0/NULL —
this clears the badge when the user fixes the underlying issue.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest


def _seed_two_mods(db):
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "json_source) VALUES (1, 'Mod A', 'paz', 1, 1, '/fake/a.json')"
    )
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "json_source) VALUES (2, 'Mod B', 'paz', 1, 2, '/fake/b.json')"
    )
    db.connection.commit()


def test_persist_skip_summary_writes_per_mod_counts(tmp_path: Path):
    from cdumm.engine.apply_engine import persist_skip_summary
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_two_mods(db)

    patch_skips = [
        {"_source_mod_id": 1, "label": "A1", "reason": "byte mismatch",
         "_target_file": "iteminfo.pabgb"},
        {"_source_mod_id": 1, "label": "A2", "reason": "byte mismatch",
         "_target_file": "iteminfo.pabgb"},
        {"_source_mod_id": 2, "label": "B1", "reason": "stale signature",
         "_target_file": "skill.pabgb"},
    ]
    participating = {1, 2}

    persist_skip_summary(db.connection, patch_skips, participating)

    row1 = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id=1").fetchone()
    assert row1[0] == 2, f"Mod 1 should have count 2, got {row1[0]}"
    summary1 = json.loads(row1[1])
    assert len(summary1) == 2
    assert all(s["label"] in ("A1", "A2") for s in summary1)

    row2 = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id=2").fetchone()
    assert row2[0] == 1
    summary2 = json.loads(row2[1])
    assert summary2[0]["label"] == "B1"

    db.close()


def test_persist_skip_summary_resets_clean_mods(tmp_path: Path):
    """When mod 1 had skips on the previous apply (count=5) but this
    apply produced ZERO skips for mod 1, its row must be reset to
    count=0, summary=NULL — the badge should clear."""
    from cdumm.engine.apply_engine import persist_skip_summary
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_two_mods(db)
    # Pre-existing stale state
    db.connection.execute(
        "UPDATE mods SET last_apply_skipped_count=5, "
        "last_apply_skip_summary='[{\"label\":\"old\"}]' WHERE id=1")
    db.connection.commit()

    # New apply: no skips at all
    persist_skip_summary(db.connection, patch_skips=[],
                         participating_mod_ids={1, 2})

    row = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id=1").fetchone()
    assert row[0] == 0, f"Stale count must reset, got {row[0]}"
    assert row[1] is None, f"Stale summary must clear, got {row[1]!r}"

    db.close()


def test_persist_skip_summary_ignores_non_participating_mods(tmp_path: Path):
    """Mods that didn't participate in this apply (e.g. disabled
    PAZ-only mods) must not have their skip state touched. The badge
    should reflect their LAST participating apply."""
    from cdumm.engine.apply_engine import persist_skip_summary
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_two_mods(db)
    # Mod 2 had skips from a previous run, isn't enabled this time
    db.connection.execute(
        "UPDATE mods SET last_apply_skipped_count=3, "
        "last_apply_skip_summary='[{\"label\":\"prev\"}]' WHERE id=2")
    db.connection.commit()

    # Only mod 1 participated this apply, with 1 skip
    persist_skip_summary(db.connection,
                         patch_skips=[{"_source_mod_id": 1,
                                       "label": "X", "reason": "byte"}],
                         participating_mod_ids={1})

    # Mod 1 reflects new state
    row1 = db.connection.execute(
        "SELECT last_apply_skipped_count FROM mods WHERE id=1").fetchone()
    assert row1[0] == 1

    # Mod 2 untouched
    row2 = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id=2").fetchone()
    assert row2[0] == 3, f"Non-participating mod stale state must persist"
    assert "prev" in row2[1]

    db.close()
