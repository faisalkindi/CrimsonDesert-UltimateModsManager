"""Skipped-mod-badge plumbing, chunk 2B: stamp_enabled_mods_as_current
must exclude mods that have active skips (last_apply_skipped_count > 0)
from the 'known-good for this game version' stamping.

Without this, after a partial-skip apply the success-stamp logic
would mark the skipped mod as known-good for the current version
even though half its patches didn't actually apply. The Post-Apply
Verifier wouldn't flag it, and the orange 'outdated' badge that
exists today would never fire — the only persistent signal users
have for the skipped state is the new yellow badge from this work.
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import patch

import pytest


def _seed_two_enabled_mods_with_old_hash(db, old_hash: str = "OLD"):
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "game_version_hash) VALUES (1, 'Clean Mod', 'paz', 1, 1, ?)",
        (old_hash,))
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "game_version_hash, last_apply_skipped_count) "
        "VALUES (2, 'Skipped Mod', 'paz', 1, 2, ?, 3)",
        (old_hash,))
    db.connection.commit()


def test_stamp_excludes_mods_with_skip_count(tmp_path: Path):
    from cdumm.engine.version_detector import stamp_enabled_mods_as_current
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_two_enabled_mods_with_old_hash(db, old_hash="OLD")

    # Stub detect_game_version to return a deterministic new hash
    with patch("cdumm.engine.version_detector.detect_game_version",
               return_value="NEW"):
        stamp_enabled_mods_as_current(db, tmp_path / "game")

    row1 = db.connection.execute(
        "SELECT game_version_hash FROM mods WHERE id=1").fetchone()
    row2 = db.connection.execute(
        "SELECT game_version_hash FROM mods WHERE id=2").fetchone()

    assert row1[0] == "NEW", (
        f"Mod 1 (clean, no skips) should be stamped to current: {row1[0]!r}"
    )
    assert row2[0] == "OLD", (
        f"Mod 2 (last_apply_skipped_count=3) must NOT be stamped — "
        f"its last apply produced skips so it's NOT known-good for "
        f"this game version. Got {row2[0]!r}, expected 'OLD'."
    )

    db.close()
