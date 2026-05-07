"""Tests for ConfigPanel width persistence (Task 2.2)."""
from __future__ import annotations

import pytest


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path):
    from cdumm.storage.database import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    return db


def _show_simple_mod(panel):
    panel.show_mod(
        mod_id=1, name="t", author="x", version="1",
        status="active", file_count=1,
        patches=[{"label": "p", "enabled": True}], conflicts=[],
    )


def test_set_panel_width_saves_to_config_when_db_set(qtbot, app, db):
    from cdumm.gui.components.config_panel import ConfigPanel
    from cdumm.storage.config import Config

    panel = ConfigPanel()
    panel.set_db(db)
    qtbot.addWidget(panel)
    panel.set_panel_width(800)
    assert Config(db).get("config_panel_width") == "800"


def test_set_panel_width_no_db_no_crash(qtbot, app):
    """Backward compat: panels without set_db() shouldn't crash on resize."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(800)  # must not raise
    assert panel._PANEL_WIDTH == 800


def test_set_db_restores_saved_width(qtbot, app, db):
    """When set_db is called and the DB has a saved width, the
    panel should adopt that width."""
    from cdumm.gui.components.config_panel import ConfigPanel
    from cdumm.storage.config import Config

    Config(db).set("config_panel_width", "900")
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_db(db)
    assert panel._PANEL_WIDTH == 900


def test_saved_width_is_clamped_on_restore(qtbot, app, db):
    """A garbage / out-of-range saved value should be clamped, not used raw."""
    from cdumm.gui.components.config_panel import ConfigPanel
    from cdumm.storage.config import Config

    Config(db).set("config_panel_width", "5000")
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_db(db)
    assert panel._PANEL_WIDTH == 1200  # clamped to max


def test_invalid_saved_width_falls_back_to_default(qtbot, app, db):
    """Non-integer saved value falls back to default (640)."""
    from cdumm.gui.components.config_panel import ConfigPanel
    from cdumm.storage.config import Config

    Config(db).set("config_panel_width", "not_a_number")
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_db(db)
    assert panel._PANEL_WIDTH == 640  # default
