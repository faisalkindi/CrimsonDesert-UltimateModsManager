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
