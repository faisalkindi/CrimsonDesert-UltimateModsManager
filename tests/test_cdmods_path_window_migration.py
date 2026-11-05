"""Tests for the window-level cdmods_path migration handler (C2 + C3).

The settings page emits ``cdmods_path_change_requested(old, new)``; the
parent window handles it by closing the live DB, running
``migrate_cdmods``, reopening at the new path, and persisting the
override to the NEW DB. Without that ordering:

  C2: shutil.rmtree inside migrate_cdmods fails with WinError 32
      because the live SQLite handle keeps cdumm.db locked.
  C3: ``Config.set('cdmods_path', new)`` would write to the OLD DB
      (about to be deleted), so the next launch wouldn't see the
      override and would create an empty CDMods/ at the default.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_qt = pytest.importorskip("pytestqt")

from cdumm.i18n import load as load_translations

load_translations("en")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _make_seeded_cdmods(root: Path) -> Path:
    """Create a CDMods/ tree with cdumm.db + a marker file."""
    root.mkdir(parents=True, exist_ok=True)
    from cdumm.storage.database import Database

    db = Database(root / "cdumm.db")
    db.initialize()
    db.close()

    (root / "marker.txt").write_text("seeded", encoding="utf-8")
    return root


class _StubWindow:
    """Minimal window stub exercising the C2/C3 sequence directly,
    without instantiating the heavy FluentWindow.

    The real handler lives in fluent_window.py; this stub copies the
    sequence so we can drive it with synthetic state in a unit test.
    Keeps the test fast (no Qt window construction) and surgical (we
    assert on the exact ordering we care about).
    """

    def __init__(self, db, cdmods_dir):
        self._db = db
        self._cdmods_dir = cdmods_dir

    def run_migration(self, old_path: Path, new_path: Path) -> None:
        # Mirrors fluent_window._on_cdmods_path_change_requested but
        # without managers/UI re-wiring. We only care about the
        # close -> migrate -> reopen -> persist ordering.
        from cdumm.storage.cdmods_migration import migrate_cdmods
        from cdumm.storage.database import Database
        from cdumm.storage.config import Config
        from cdumm.engine.cdmods_paths import write_cdmods_path_pointer

        # 1. Close the live DB so the OS lock releases.
        self._db.close()
        self._db = None

        # 2. Migrate (verifies + deletes old tree).
        migrate_cdmods(old_path, new_path)

        # 3. Reopen at the new location.
        self._cdmods_dir = new_path
        self._db = Database(new_path / "cdumm.db")
        self._db.initialize()

        # 3b. Rewrite stored absolute paths that still name the OLD
        # root (#477), mirroring fluent_window._on_cdmods_path_
        # change_requested.
        old_prefix = str(old_path)
        new_prefix = str(new_path)
        like_pattern = old_prefix + "%"
        cur = self._db.connection
        for table, column in (
            ("mod_deltas", "delta_path"),
            ("mods", "json_source"),
            ("mods", "source_path"),
        ):
            cur.execute(
                f"UPDATE {table} SET {column} = "
                f"REPLACE({column}, ?, ?) WHERE {column} LIKE ?",
                (old_prefix, new_prefix, like_pattern),
            )
        cur.commit()

        # 4. Persist override to the NEW DB (C3 fix).
        Config(self._db).set("cdmods_path", str(new_path))

        # 5. Pointer file for next-launch bootstrap (C1).
        write_cdmods_path_pointer(new_path)


def test_window_migration_close_then_migrate_then_reopen_persists(
        tmp_path, monkeypatch):
    """End-to-end window-level migration: open DB at old path, run
    migration, verify NEW DB has the override persisted (proving the
    write didn't hit the old/deleted DB)."""
    from cdumm.storage.database import Database
    from cdumm.storage.config import Config
    from cdumm.engine import cdmods_paths

    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "appdata")

    old_path = tmp_path / "CDMods"
    _make_seeded_cdmods(old_path)
    new_path = tmp_path / "alt_storage"
    new_path.mkdir()

    db = Database(old_path / "cdumm.db")
    db.initialize()

    win = _StubWindow(db, old_path)
    win.run_migration(old_path, new_path)

    # Old tree gone (rmtree succeeded because we closed the DB).
    assert not old_path.exists(), (
        "old CDMods tree should be gone; if this fails on Windows, "
        "the close-before-rmtree ordering is broken (C2)")

    # New DB carries the override (C3).
    assert (new_path / "cdumm.db").exists()
    assert Config(win._db).get("cdmods_path") == str(new_path)

    # Pointer file written (C1 follow-up).
    pointer = cdmods_paths.read_cdmods_path_pointer()
    assert pointer == new_path


def test_window_migration_fails_when_db_still_open(tmp_path):
    """Sanity: if we DON'T close the DB before migrate, the rmtree
    inside migrate_cdmods raises (the test we're protecting against
    with C2). On non-Windows hosts the lock semantics differ and
    rmtree may succeed, so this is best-effort.

    Skipped on non-Windows because POSIX file semantics let rmtree
    proceed even on open files , the regression we need to guard is
    Windows-specific.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip(
            "POSIX rmtree tolerates open files; the bug we guard "
            "against is Windows-only")

    from cdumm.storage.database import Database
    from cdumm.storage.cdmods_migration import (
        migrate_cdmods, MigrationError,
    )

    old_path = tmp_path / "CDMods"
    _make_seeded_cdmods(old_path)
    new_path = tmp_path / "alt_storage"

    db = Database(old_path / "cdumm.db")
    db.initialize()
    # initialize() already opened a real connection; the SQLite handle
    # holds an OS-level lock on the .db file on Windows.

    try:
        with pytest.raises((MigrationError, OSError, PermissionError)):
            migrate_cdmods(old_path, new_path)
    finally:
        db.close()


def test_window_migration_rewrites_stored_paths_so_next_launch_keeps_rows(
        tmp_path, monkeypatch):
    """Regression for #477: mod_deltas.delta_path and mods.json_source/
    source_path are stored as absolute paths. Without rewriting them
    to the new root after a move, cleanup_orphaned_deltas (run on
    every launch) finds them "missing" and deletes the rows, even
    though the real files moved along with everything else."""
    from cdumm.storage.database import Database
    from cdumm.engine import cdmods_paths
    from cdumm.engine.mod_manager import ModManager

    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "appdata")

    old_path = tmp_path / "CDMods"
    _make_seeded_cdmods(old_path)
    new_path = tmp_path / "alt_storage"
    new_path.mkdir()

    db = Database(old_path / "cdumm.db")
    db.initialize()

    # Seed one enabled mod with a delta file, and one disabled mod
    # with a json_source and a source_path, both under old_path --
    # exactly the shape the real importers write.
    (old_path / "deltas" / "1").mkdir(parents=True)
    delta_file = old_path / "deltas" / "1" / "a.bsdiff"
    delta_file.write_bytes(b"delta")
    (old_path / "mods").mkdir()
    json_source = old_path / "mods" / "mod.json"
    json_source.write_text("{}", encoding="utf-8")

    cur = db.connection
    cur.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, source_path) "
        "VALUES (1, 'Entr Mod', 'paz', 1, ?)", (str(json_source),))
    cur.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
        "VALUES (1, '0008/0.paz', ?)", (str(delta_file),))
    cur.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, json_source) "
        "VALUES (2, 'Json Mod', 'paz', 0, ?)", (str(json_source),))
    cur.commit()

    win = _StubWindow(db, old_path)
    win.run_migration(old_path, new_path)

    new_delta_file = new_path / "deltas" / "1" / "a.bsdiff"
    new_json_source = new_path / "mods" / "mod.json"
    assert new_delta_file.exists()
    assert new_json_source.exists()

    rows = dict(win._db.connection.execute(
        "SELECT mod_id, delta_path FROM mod_deltas").fetchall())
    assert rows[1] == str(new_delta_file), (
        "delta_path still names the old (deleted) root")

    sources = dict(win._db.connection.execute(
        "SELECT id, source_path FROM mods WHERE id = 1").fetchall())
    assert sources[1] == str(new_json_source)

    json_sources = dict(win._db.connection.execute(
        "SELECT id, json_source FROM mods WHERE id = 2").fetchall())
    assert json_sources[2] == str(new_json_source)

    # The real regression: the next-launch sweep must not treat these
    # rows as orphaned now that their paths resolve under the new root.
    mgr = ModManager(win._db, new_path / "deltas")
    mgr.cleanup_orphaned_deltas()

    remaining_deltas = win._db.connection.execute(
        "SELECT COUNT(*) FROM mod_deltas").fetchone()[0]
    remaining_mods = win._db.connection.execute(
        "SELECT COUNT(*) FROM mods").fetchone()[0]
    assert remaining_deltas == 1, (
        "mod_deltas row was dropped as orphaned after the move")
    assert remaining_mods == 2, (
        "a mod was removed as a zombie after the move")
