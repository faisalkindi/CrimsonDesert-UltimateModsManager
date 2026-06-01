"""Regression for GitHub #184 item 3 (devCKVargas): double-clicking a
mod card toggles its enabled state.

The expected behavior is that a left-button double-click anywhere on
the card body flips the checkbox, which fires the existing
``toggled`` signal with the new value. Any user that already has logic
hooked into ``toggled`` keeps working; this just adds a second hit
target alongside the checkbox click.

Two tests pin both directions of the toggle plus the signal emission
shape (mod_id + new enabled value).
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from cdumm.gui.components.mod_card import ModCard


@pytest.fixture
def enabled_card(qtbot):
    card = ModCard(
        mod_id=42,
        order=1,
        name="Test Mod",
        author="someone",
        version="1.0",
        status="active",
        file_count=1,
        enabled=True,
    )
    qtbot.addWidget(card)
    return card


@pytest.fixture
def disabled_card(qtbot):
    card = ModCard(
        mod_id=43,
        order=2,
        name="Other Mod",
        author="someone",
        version="1.0",
        status="active",
        file_count=1,
        enabled=False,
    )
    qtbot.addWidget(card)
    return card


def _double_click(card):
    """Synthesise a left-button double-click on the centre of the card."""
    pos = QPointF(card.rect().center())
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonDblClick,
        pos,
        card.mapToGlobal(pos.toPoint()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(card, ev)
    # Process the queued toggled signal so the test observer fires.
    QApplication.processEvents()


def test_double_click_disables_an_enabled_card(qtbot, enabled_card):
    received = []
    enabled_card.toggled.connect(
        lambda mod_id, on: received.append((mod_id, on)))
    _double_click(enabled_card)
    assert received == [(42, False)]


def test_double_click_enables_a_disabled_card(qtbot, disabled_card):
    received = []
    disabled_card.toggled.connect(
        lambda mod_id, on: received.append((mod_id, on)))
    _double_click(disabled_card)
    assert received == [(43, True)]
