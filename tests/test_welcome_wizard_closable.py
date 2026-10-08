"""The welcome wizard overrode closeEvent/keyPressEvent to swallow
ALT+F4, the taskbar close command and Escape outright, with no close
button of its own (frameless window). A user who opened it without
wanting to complete first-run setup had no way out short of killing
the process (#442, reported by Ahplla).

The dialog should behave like any other QDialog: closing it rejects
it instead of leaving it stuck open.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QDialog

from cdumm.gui.welcome_wizard import WelcomeWizard


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_close_rejects_instead_of_staying_open():
    wizard = WelcomeWizard()
    wizard.show()
    wizard.close()
    assert wizard.isVisible() is False
    assert wizard.result() == QDialog.DialogCode.Rejected


def test_escape_rejects_instead_of_staying_open():
    wizard = WelcomeWizard()
    wizard.show()
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    wizard.keyPressEvent(event)
    assert wizard.isVisible() is False
    assert wizard.result() == QDialog.DialogCode.Rejected
