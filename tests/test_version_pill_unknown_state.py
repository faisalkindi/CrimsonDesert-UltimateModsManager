"""Version pills must stay GREY for mods that were never checked.

``retranslate_version`` used to call ``set_update_available(False)``
whenever ``_has_update`` was unset, which painted the green "up to
date" pill on every card after a language switch even though no Nexus
check had run. The contract (three-state):

  - has_update=True   -> RED "Click To Update" pill
  - has_update=False  -> GREEN "up to date" pill
  - never checked     -> GREY neutral pill (unknown)

Covers both ModCard (PAZ page) and AsiCard (ASI page).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pytestqt")


def _grey_qss() -> str:
    from cdumm.gui.components.mod_card import (
        _VERSION_COLORS, _pill_qss, _theme_key)
    return _pill_qss(_VERSION_COLORS[_theme_key()])


def _make_mod_card(qtbot, version: str = "1.0"):
    from cdumm.gui.components.mod_card import ModCard
    card = ModCard(
        mod_id=1, order=1, name="Test Mod", author="",
        version=version, status="Installed", file_count=1,
        has_config=False, has_notes=False, is_new=False,
        enabled=True,
    )
    qtbot.addWidget(card)
    return card


def _make_asi_card(qtbot, tmp_path: Path):
    from cdumm.asi.asi_manager import AsiPlugin
    from cdumm.gui.pages.asi_page import AsiCard
    plugin = AsiPlugin(
        name="TestPlugin", path=tmp_path / "TestPlugin.asi",
        enabled=True, ini_path=None)
    card = AsiCard(plugin, order=1, version="1.0")
    qtbot.addWidget(card)
    return card


# -- ModCard --------------------------------------------------------------

def test_modcard_retranslate_keeps_grey_when_never_checked(qtbot):
    card = _make_mod_card(qtbot)
    assert not hasattr(card, "_has_update")

    card.retranslate_version()

    # Still unknown: no green/red state, neutral grey style, text and
    # _has_update untouched.
    assert not hasattr(card, "_has_update")
    assert card._version_pill.styleSheet() == _grey_qss()
    assert card._version_pill.text() == "1.0"


def test_modcard_retranslate_keeps_green_after_confirmed_current(qtbot):
    card = _make_mod_card(qtbot)
    card.set_update_available(False)
    green_qss = card._version_pill.styleSheet()
    assert green_qss != _grey_qss()

    card.retranslate_version()

    assert card._version_pill.styleSheet() == green_qss


def test_modcard_retranslate_keeps_red_after_update_found(qtbot):
    from cdumm.i18n import tr
    card = _make_mod_card(qtbot)
    card.set_update_available(True, "https://example.com",
                               nexus_mod_id=100, latest_file_id=42)

    card.retranslate_version()

    assert card._version_pill.text() == tr("mod_list.click_to_update")
    assert card._has_update is True


# -- AsiCard --------------------------------------------------------------

def test_asicard_retranslate_keeps_grey_when_never_checked(qtbot, tmp_path):
    card = _make_asi_card(qtbot, tmp_path)
    assert not hasattr(card, "_has_update")

    card.retranslate_version()

    assert not hasattr(card, "_has_update")
    assert card._version_pill.styleSheet() == _grey_qss()
    assert card._version_pill.text() == "1.0"


def test_asicard_retranslate_keeps_green_after_confirmed_current(qtbot, tmp_path):
    card = _make_asi_card(qtbot, tmp_path)
    card.set_update_available(False)
    green_qss = card._version_pill.styleSheet()
    assert green_qss != _grey_qss()

    card.retranslate_version()

    assert card._version_pill.styleSheet() == green_qss


def test_asicard_retranslate_keeps_red_after_update_found(qtbot, tmp_path):
    from cdumm.i18n import tr
    card = _make_asi_card(qtbot, tmp_path)
    card.set_update_available(True, "https://example.com",
                               nexus_mod_id=100, latest_file_id=42)

    card.retranslate_version()

    assert card._version_pill.text() == tr("mod_list.click_to_update")
    assert card._has_update is True
