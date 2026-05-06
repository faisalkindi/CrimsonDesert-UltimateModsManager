"""Bug found via /systematic-debugging: the SKIPPED-badge tooltip
formats each line as ``label OR file OR '?'``. Only one of the
three shows. After Bug D plumbed the target file into the skip
summary JSON, the tooltip still hid it , the user sees the entry
label and reason but no clue which game asset (iteminfo.pabgb,
skill.pabgb, etc.) actually failed.

Multi-target mods are the worst case: a mod patching three files
produces three skip lines, all named only by entry label. The user
can't tell the lines apart by asset , and "Reimport from source"
fixes everything blindly anyway, so the diagnostic value drops to
zero.

Fix: when the skip entry carries both ``label`` and ``file``, show
them together. Reason still trails on the same line.
"""
from __future__ import annotations
import json

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _make_card(qtbot, **overrides):
    from cdumm.gui.components.mod_card import ModCard
    kwargs = dict(
        mod_id=1, order=1, name="Multi Target Mod", author="",
        version="1.0", status="Installed", file_count=3,
        has_config=False, has_notes=False, is_new=False,
        enabled=True,
    )
    kwargs.update(overrides)
    card = ModCard(**kwargs)
    qtbot.addWidget(card)
    return card


def test_tooltip_shows_label_and_file_together(qtbot):
    """When a skip entry has both label and file, the tooltip line
    must surface both so users can map labels to game assets."""
    from PySide6.QtWidgets import QLabel
    summary = json.dumps([
        {"label": "stamina_swim", "reason": "byte mismatch",
         "file": "skill.pabgb"},
        {"label": "iteminfo_drop_42", "reason": "byte mismatch",
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
    assert skipped_labels, "Badge should be on a card with count > 0"
    tooltip = skipped_labels[0].toolTip() or ""

    assert "stamina_swim" in tooltip and "skill.pabgb" in tooltip, (
        f"Tooltip must show label AND file together for entry 1. "
        f"Got: {tooltip!r}"
    )
    assert "iteminfo_drop_42" in tooltip and "iteminfo.pabgb" in tooltip, (
        f"Tooltip must show label AND file together for entry 2. "
        f"Got: {tooltip!r}"
    )


def test_tooltip_falls_back_to_file_when_label_missing(qtbot):
    """When a skip entry has no label (older or unlabeled patches),
    the file alone is fine , but a present label must not hide the
    file. Pin the precedence."""
    from PySide6.QtWidgets import QLabel
    summary = json.dumps([
        {"label": "", "reason": "byte mismatch", "file": "skill.pabgb"},
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
    assert "skill.pabgb" in tooltip, (
        f"With empty label, tooltip must still surface file. "
        f"Got: {tooltip!r}"
    )
