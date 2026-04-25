"""When a mod's local DB has no version stored, the card renders the
em-dash placeholder ``—``. The Nexus update check still confirms the
user is current via file_id match, so the pill correctly turns green —
but the visible text "—" alongside a green pill is confusing.

``fill_missing_version`` paints the Nexus-reported version into the
pill in that exact case. It must NOT overwrite a real local version
(the user kept what they imported).
"""
from __future__ import annotations

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _make_mod_card(qtbot, version: str):
    from cdumm.gui.components.mod_card import ModCard
    card = ModCard(
        mod_id=1, order=1, name="Test Mod", author="",
        version=version, status="Installed", file_count=1,
        has_config=False, has_notes=False, is_new=False,
        enabled=True,
    )
    qtbot.addWidget(card)
    return card


def test_fill_missing_version_replaces_em_dash(qtbot):
    card = _make_mod_card(qtbot, version="—")
    card.fill_missing_version("1.7.1")
    assert card._version_pill.text() == "1.7.1"


def test_fill_missing_version_truncates_long_versions(qtbot):
    card = _make_mod_card(qtbot, version="—")
    card.fill_missing_version("1.2.3.4.5-rc-build-9999")
    txt = card._version_pill.text()
    assert len(txt) <= 7
    assert txt.endswith("…")


def test_fill_missing_version_does_not_overwrite_real_version(qtbot):
    card = _make_mod_card(qtbot, version="1.5")
    card.fill_missing_version("1.5.2")
    assert card._version_pill.text() == "1.5"


def test_fill_missing_version_noop_with_empty_nexus_version(qtbot):
    card = _make_mod_card(qtbot, version="—")
    card.fill_missing_version("")
    assert card._version_pill.text() == "—"


def test_fill_missing_version_updates_stash_when_in_red_mode(qtbot):
    """When the card is in red 'Click To Update' mode, the displayed
    text reads 'Click To Update' and the original version is stashed in
    ``_version_pill_orig_text``. ``fill_missing_version`` must update
    the stash so the green-restore path picks up the real number, not
    the em-dash."""
    card = _make_mod_card(qtbot, version="—")
    card.set_update_available(True, "https://example.com",
                               nexus_mod_id=100, latest_file_id=42)
    assert card._version_pill_orig_text == "—"
    card.fill_missing_version("2.0")
    assert card._version_pill_orig_text == "2.0"
    # Flip back to green and confirm the new text shows.
    card.set_update_available(False)
    assert card._version_pill.text() == "2.0"


def test_fill_missing_version_noop_when_red_mode_already_has_real_stash(qtbot):
    card = _make_mod_card(qtbot, version="1.0")
    card.set_update_available(True, "https://example.com",
                               nexus_mod_id=100, latest_file_id=42)
    assert card._version_pill_orig_text == "1.0"
    card.fill_missing_version("2.0")
    assert card._version_pill_orig_text == "1.0"


def _make_asi_card(qtbot, version: str):
    from cdumm.asi.asi_manager import AsiPlugin
    from cdumm.gui.pages.asi_page import AsiCard
    from pathlib import Path
    plugin = AsiPlugin(name="TestPlugin", path=Path("TestPlugin.asi"),
                        enabled=True, ini_path=None, hook_targets=[])
    card = AsiCard(plugin=plugin, order=1, is_new=False, version=version)
    qtbot.addWidget(card)
    return card


def test_asi_card_fill_missing_version_replaces_em_dash(qtbot):
    card = _make_asi_card(qtbot, version="—")
    card.fill_missing_version("v3.1")
    assert card._version_pill.text() == "v3.1"


def test_asi_card_fill_missing_version_does_not_overwrite_real(qtbot):
    card = _make_asi_card(qtbot, version="v3.0")
    card.fill_missing_version("v3.1")
    assert card._version_pill.text() == "v3.0"
