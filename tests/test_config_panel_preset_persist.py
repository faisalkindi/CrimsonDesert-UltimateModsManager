"""Tests for preset persistence (Task 1.4)."""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path):
    """In-memory style DB for the test."""
    from cdumm.storage.database import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    return db


def _patches():
    return [
        {"label": "[0%] alpha", "enabled": True},
        {"label": "[100%] alpha", "enabled": True},
    ]


def test_clicking_preset_saves_to_config(qtbot, app, db):
    from cdumm.gui.components.config_panel import ConfigPanel
    from cdumm.storage.config import Config

    panel = ConfigPanel()
    panel.set_db(db)
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=42, name="t", author="x", version="1",
        status="active", file_count=1, patches=_patches(), conflicts=[],
    )
    zero_radio = next(b for b in panel._preset_radio_group.buttons() if b.text() == "0%")
    zero_radio.click()
    cfg = Config(db)
    assert cfg.get("mod_42_preset") == "0%"


def test_panel_restores_saved_preset_on_reopen(qtbot, app, db):
    from cdumm.gui.components.config_panel import ConfigPanel
    from cdumm.storage.config import Config

    Config(db).set("mod_42_preset", "100%")
    panel = ConfigPanel()
    panel.set_db(db)
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=42, name="t", author="x", version="1",
        status="active", file_count=1, patches=_patches(), conflicts=[],
    )
    checked = [b for b in panel._preset_radio_group.buttons() if b.isChecked()]
    assert len(checked) == 1
    assert checked[0].text() == "100%"


def test_no_saved_preset_defaults_to_custom(qtbot, app, db):
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    panel.set_db(db)
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=99, name="t", author="x", version="1",
        status="active", file_count=1, patches=_patches(), conflicts=[],
    )
    checked = [b for b in panel._preset_radio_group.buttons() if b.isChecked()]
    assert len(checked) == 1
    assert checked[0].text() == "Custom"


def test_panel_works_without_db(qtbot, app):
    """Backwards compat: existing call sites that don't set_db()
    should still work; persistence is just a no-op."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=1, name="t", author="x", version="1",
        status="active", file_count=1, patches=_patches(), conflicts=[],
    )
    zero_radio = next(b for b in panel._preset_radio_group.buttons() if b.text() == "0%")
    zero_radio.click()
    # No assertion on persistence — just shouldn't crash.
