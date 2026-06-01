"""Regression for GitHub #184 item 2 (devCKVargas): the About-page link
cards should be clickable on the whole row, not only the small "Open"
button on the right side.

_LinkCard now overrides mousePressEvent to call QDesktopServices.openUrl
when the user left-clicks anywhere on the card. The test monkey-patches
QDesktopServices.openUrl to record what URL was requested, then sends a
synthetic mousePress event to the centre of the card and asserts the
patched URL came through. Right-clicks are explicitly NOT routed through
this path (they should remain available for the platform context menu).
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt, QPointF, QUrl
from PySide6.QtGui import QMouseEvent, QDesktopServices
from PySide6.QtWidgets import QApplication

from qfluentwidgets import FluentIcon

from cdumm.gui.pages.about_page import _LinkCard


@pytest.fixture
def link_card(qtbot, monkeypatch):
    """Build a _LinkCard whose URL openUrl call is captured."""
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    card = _LinkCard(
        FluentIcon.GITHUB,
        "Test Link",
        "Test description",
        "https://example.com/test",
    )
    qtbot.addWidget(card)
    return card, opened


def test_left_click_anywhere_opens_url(link_card):
    card, opened = link_card
    pos = QPointF(card.rect().center())
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pos,
        card.mapToGlobal(pos.toPoint()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(card, ev)
    assert opened == ["https://example.com/test"]


def test_right_click_does_not_trigger_url(link_card):
    card, opened = link_card
    pos = QPointF(card.rect().center())
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pos,
        card.mapToGlobal(pos.toPoint()),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(card, ev)
    assert opened == []


def test_cursor_is_pointing_hand(link_card):
    """Hover affordance: the cursor flips to a pointing hand so the
    whole row reads as clickable without needing the user to discover
    the new behavior by accident."""
    card, _ = link_card
    assert card.cursor().shape() == Qt.CursorShape.PointingHandCursor
