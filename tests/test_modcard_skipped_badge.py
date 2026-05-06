"""Skipped-mod-badge plumbing, chunk 3: ModCard renders a yellow
'skipped' badge when last_apply_skipped_count > 0.

Click-to-fix: the badge tooltip shows which patches got skipped
on the most recent Apply. Right-click 'Reimport from source'
already exists in the context menu; the badge points users at it.
"""
from __future__ import annotations
import json

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _make_card(qtbot, **overrides):
    from cdumm.gui.components.mod_card import ModCard
    kwargs = dict(
        mod_id=1, order=1, name="Test Mod", author="",
        version="1.0", status="Installed", file_count=1,
        has_config=False, has_notes=False, is_new=False,
        enabled=True,
    )
    kwargs.update(overrides)
    card = ModCard(**kwargs)
    qtbot.addWidget(card)
    return card


def test_modcard_no_badge_when_skip_count_zero(qtbot):
    """Default: no skipped badge when count is 0 / unset."""
    from PySide6.QtWidgets import QLabel
    card = _make_card(qtbot, last_apply_skipped_count=0)
    labels = card.findChildren(QLabel)
    skipped_labels = [
        lbl for lbl in labels
        if "skipped" in (lbl.text() or "").lower()
        or "skipped" in (lbl.objectName() or "").lower()
    ]
    assert not skipped_labels, (
        f"Expected no skipped badge when count=0, found "
        f"{[lbl.text() for lbl in skipped_labels]!r}"
    )


def test_modcard_renders_badge_when_skip_count_positive(qtbot):
    """When last_apply_skipped_count > 0, a label with 'skipped' text
    or objectName must exist on the card."""
    from PySide6.QtWidgets import QLabel
    summary = json.dumps([
        {"label": "iteminfo entry 1", "reason": "byte mismatch",
         "file": "iteminfo.pabgb"},
        {"label": "iteminfo entry 2", "reason": "byte mismatch",
         "file": "iteminfo.pabgb"},
    ])
    card = _make_card(
        qtbot, last_apply_skipped_count=2,
        last_apply_skip_summary=summary)

    labels = card.findChildren(QLabel)
    skipped_labels = [
        lbl for lbl in labels
        if "skipped" in (lbl.text() or "").lower()
        or "skipped" in (lbl.objectName() or "").lower()
    ]
    assert skipped_labels, (
        "Expected a skipped badge on the card when "
        "last_apply_skipped_count=2. Existing labels: "
        f"{[lbl.text() for lbl in labels[:20]]!r}"
    )


def test_modcard_badge_tooltip_lists_skipped_entries(qtbot):
    """The badge tooltip must include something derived from the
    skip summary so users can see what failed without leaving the
    card."""
    from PySide6.QtWidgets import QLabel
    summary = json.dumps([
        {"label": "stamina_swim", "reason": "byte mismatch",
         "file": "skill.pabgb"},
    ])
    card = _make_card(
        qtbot, last_apply_skipped_count=1,
        last_apply_skip_summary=summary)

    labels = card.findChildren(QLabel)
    skipped_labels = [
        lbl for lbl in labels
        if "skipped" in (lbl.text() or "").lower()
        or "skipped" in (lbl.objectName() or "").lower()
    ]
    assert skipped_labels
    tooltip = skipped_labels[0].toolTip() or ""
    assert ("stamina_swim" in tooltip
            or "skill.pabgb" in tooltip
            or "1" in tooltip), (
        f"Skipped badge tooltip must surface the skip summary. "
        f"Got: {tooltip!r}"
    )
