"""Tests for the cdmods_path settings UI (Task 3.3).

The Settings page must expose a "Mod storage location" section so users
can override where CDUMM keeps its CDMods/ directory (sources, vanilla
snapshots, deltas, cdumm.db). Useful when the game is on a small drive
but the user wants mod backups on a bigger one.

This task only persists the chosen path and updates the displayed label;
the actual on-disk migration of CDMods/ contents is Task 3.4.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_qt = pytest.importorskip("pytestqt")

from cdumm.i18n import load as load_translations

# tr() looks up strings in a module-level dict that starts empty. Load
# English once so UI labels come out as text, not raw keys.
load_translations("en")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_pointer_file(monkeypatch, tmp_path):
    """Redirect the cdmods_path pointer file to a temp dir so these
    tests cannot pollute %LOCALAPPDATA%/cdumm/cdmods_path.txt , a
    write there would leak into every other test that consults
    get_cdmods_root(None, ...) with no monkeypatch of its own."""
    from cdumm.engine import cdmods_paths
    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "_appdata_isolate")


def test_settings_page_has_cdmods_path_field(qtbot, app, db, tmp_path):
    """The settings page renders a 'Mod storage location' section with
    a label that shows the currently resolved CDMods/ path."""
    from cdumm.gui.pages.settings_page import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_managers(db=db, game_dir=tmp_path)

    assert hasattr(page, "_cdmods_path_label"), (
        "SettingsPage must expose _cdmods_path_label so the UI shows "
        "the user where their CDMods/ directory lives.")
    assert page._cdmods_path_label.text(), (
        "the label must be populated with the resolved path on load")


def test_setting_cdmods_path_persists_to_db(qtbot, app, db, tmp_path):
    """Calling _on_cdmods_path_changed writes the new override to the
    config table so subsequent get_cdmods_root() calls pick it up."""
    from cdumm.gui.pages.settings_page import SettingsPage
    from cdumm.storage.config import Config

    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_managers(db=db, game_dir=tmp_path)

    new_path = tmp_path / "new_cdmods"
    new_path.mkdir()
    page._on_cdmods_path_changed(new_path)

    assert Config(db).get("cdmods_path") == str(new_path), (
        "the chosen path must be persisted under the cdmods_path "
        "config key so get_cdmods_root() picks it up next launch.")


def test_label_updates_after_path_change(qtbot, app, db, tmp_path):
    """The displayed path label must reflect the newly chosen folder
    immediately, not require a page refresh."""
    from cdumm.gui.pages.settings_page import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_managers(db=db, game_dir=tmp_path)

    new_path = tmp_path / "new_cdmods"
    new_path.mkdir()
    page._on_cdmods_path_changed(new_path)

    assert str(new_path) in page._cdmods_path_label.text()


def test_settings_page_works_without_db(qtbot, app):
    """The page must construct cleanly even before set_managers() is
    called , main_window builds the page first, then wires it up."""
    from cdumm.gui.pages.settings_page import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)
    # No crash, label exists but may be empty until set_managers fires.
    assert hasattr(page, "_cdmods_path_label")


# ---------------------------------------------------------------------------
# C2 + C3: migration must close DB, reopen at new path, persist there
# ---------------------------------------------------------------------------


def _stub_messagebox_yes(monkeypatch):
    """Replace QMessageBox so the migration confirmation auto-accepts."""
    import PySide6.QtWidgets as qtw
    real_box = qtw.QMessageBox

    class _StubBox:
        Icon = real_box.Icon
        StandardButton = real_box.StandardButton

        def __init__(self, *a, **kw): pass
        def setIcon(self, *a, **kw): pass
        def setWindowTitle(self, *a, **kw): pass
        def setText(self, *a, **kw): pass
        def setInformativeText(self, *a, **kw): pass
        def setStandardButtons(self, *a, **kw): pass
        def setDefaultButton(self, *a, **kw): pass
        def exec(self): return real_box.StandardButton.Yes
        def exec_(self): return real_box.StandardButton.Yes

    monkeypatch.setattr(qtw, "QMessageBox", _StubBox)


def test_cdmods_path_change_emits_signal_when_migration_needed(
        qtbot, app, db, tmp_path, monkeypatch):
    """When the user picks a new cdmods_path with a non-empty old
    location, settings_page must hand the migration off to the parent
    window via cdmods_path_change_requested.

    The window owns the DB lifecycle and is the only place that can
    safely close + reopen the connection around the migration step.
    Without the signal, persist would write the override to the OLD
    DB (about to be deleted) and the next launch would not see it.
    """
    from cdumm.gui.pages.settings_page import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_managers(db=db, game_dir=tmp_path)

    # Seed: existing CDMods/ at the default location with a file in it
    old_root = tmp_path / "CDMods"
    old_root.mkdir()
    (old_root / "marker.txt").write_text("old", encoding="utf-8")

    new_root = tmp_path / "alt_storage"
    new_root.mkdir()

    assert hasattr(page, "cdmods_path_change_requested"), (
        "SettingsPage must expose cdmods_path_change_requested signal")

    captured: list = []
    page.cdmods_path_change_requested.connect(
        lambda old, new: captured.append((old, new)))

    _stub_messagebox_yes(monkeypatch)
    page._on_cdmods_path_changed(new_root)

    assert len(captured) == 1, (
        "settings page must emit cdmods_path_change_requested exactly "
        "once when migration is needed; got %r" % (captured,))
    emitted_old, emitted_new = captured[0]
    assert Path(emitted_old) == old_root
    assert Path(emitted_new) == new_root


def test_cdmods_path_no_migration_persists_directly(
        qtbot, app, db, tmp_path):
    """When the old path is empty / non-existent there's nothing to
    migrate, so the settings page persists the override directly
    against the open DB without involving the window."""
    from cdumm.gui.pages.settings_page import SettingsPage
    from cdumm.storage.config import Config

    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_managers(db=db, game_dir=tmp_path)

    new_root = tmp_path / "alt_storage"
    new_root.mkdir()

    captured: list = []
    page.cdmods_path_change_requested.connect(
        lambda old, new: captured.append((old, new)))

    page._on_cdmods_path_changed(new_root)

    assert captured == [], (
        "no migration needed -> no signal; persist directly")
    assert Config(db).get("cdmods_path") == str(new_root)


def test_settings_does_not_run_migrate_directly(
        qtbot, app, db, tmp_path, monkeypatch):
    """Regression: settings_page must NOT call migrate_cdmods while
    the DB connection is open. shutil.rmtree under an open SQLite
    handle raises WinError 32 on Windows. Hand off to the window."""
    from cdumm.gui.pages.settings_page import SettingsPage
    import cdumm.gui.pages.settings_page as sp_mod
    import cdumm.storage.cdmods_migration as migration_mod

    page = SettingsPage()
    qtbot.addWidget(page)
    page.set_managers(db=db, game_dir=tmp_path)

    old_root = tmp_path / "CDMods"
    old_root.mkdir()
    (old_root / "f.txt").write_text("x", encoding="utf-8")

    new_root = tmp_path / "alt_storage"
    new_root.mkdir()

    called: list = []

    def _spy(src, dst, **kw):
        called.append((src, dst))

    monkeypatch.setattr(migration_mod, "migrate_cdmods", _spy)
    if hasattr(sp_mod, "migrate_cdmods"):
        monkeypatch.setattr(sp_mod, "migrate_cdmods", _spy)

    _stub_messagebox_yes(monkeypatch)
    page._on_cdmods_path_changed(new_root)

    assert called == [], (
        "settings_page must NOT call migrate_cdmods directly")
