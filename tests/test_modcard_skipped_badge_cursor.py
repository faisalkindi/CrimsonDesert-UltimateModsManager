"""H4: SKIPPED badge must NOT use the WhatsThisCursor.

Faisal flagged 2026-05-04: 'when I hover the mouse over it it shows
a ? but when I click on the pill nothing happens'. The "?" cursor
implies the badge is interactive; clicking does nothing because no
mousePressEvent is wired up. The badge is a passive status indicator
plus a tooltip , the cursor should match that, not promise interaction
that doesn't exist.

Fix: use the default ArrowCursor so the user knows the badge is just
a label.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from cdumm.gui.components.mod_card import ModCard


@pytest.fixture
def card_with_skips(qtbot):
    card = ModCard(
        mod_id=1,
        order=1,
        name="Test Mod",
        author="x",
        version="1",
        status="active",
        file_count=1,
        last_apply_skipped_count=2,
        last_apply_skip_summary='[{"label":"x","reason":"y","file":"z"}]',
    )
    qtbot.addWidget(card)
    return card


def test_skipped_badge_cursor_is_not_whats_this(card_with_skips):
    """ArrowCursor or PointingHandCursor are both acceptable; what
    we're forbidding is the question-mark cursor that lies about
    interactivity."""
    badges = [
        c for c in card_with_skips.findChildren(type(card_with_skips))
        if False
    ]
    # ModCard doesn't expose the badge directly; find by objectName
    from PySide6.QtWidgets import QLabel
    skipped = card_with_skips.findChild(QLabel, "skippedBadge")
    assert skipped is not None, (
        "SKIPPED badge must be present on a card with "
        "last_apply_skipped_count > 0")
    cursor_shape = skipped.cursor().shape()
    assert cursor_shape != Qt.CursorShape.WhatsThisCursor, (
        f"SKIPPED badge cursor shape is WhatsThisCursor (?), implying "
        f"the badge is clickable. It isn't. Use ArrowCursor or "
        f"PointingHandCursor with a real handler."
    )
