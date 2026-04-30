"""Language-mod badge regression: passing target_language to ModCard
used to crash with KeyError because the badge color dict shipped
{"bg", "fg"} keys but the shared _pill_qss helper reads
colors["text"] and colors["border"]. Any mod with a target_language
set in modinfo.json broke the entire mods-page render.
"""
from __future__ import annotations

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _make_card(qtbot, **overrides):
    from cdumm.gui.components.mod_card import ModCard
    kwargs = dict(
        mod_id=1, order=1, name="Korean Translation", author="",
        version="1.0", status="Installed", file_count=1,
        has_config=False, has_notes=False, is_new=False,
        enabled=True,
    )
    kwargs.update(overrides)
    card = ModCard(**kwargs)
    qtbot.addWidget(card)
    return card


def test_modcard_renders_with_language_code(qtbot):
    """ModCard with target_language set must construct without error."""
    card = _make_card(qtbot, target_language="ko")
    assert card is not None


def test_modcard_renders_with_no_language(qtbot):
    """Baseline: ModCard with target_language=None still renders."""
    card = _make_card(qtbot, target_language=None)
    assert card is not None


def test_modcard_renders_with_long_language_code(qtbot):
    """Long language codes (e.g. 'zh-CN') must not crash."""
    card = _make_card(qtbot, target_language="zh-CN")
    assert card is not None


def test_modcard_renders_override_badge(qtbot):
    """ModCard with conflict_mode='override' had the same KeyError
    bug as the language badge — its color dict shipped {bg, fg} keys
    while _pill_qss reads {bg, text, border}. Any mod declaring
    override in modinfo crashed the card."""
    card = _make_card(qtbot, conflict_mode="override")
    assert card is not None


def test_modcard_renders_override_plus_language(qtbot):
    """Both badges together (override + language) must coexist."""
    card = _make_card(
        qtbot, target_language="ko", conflict_mode="override")
    assert card is not None
