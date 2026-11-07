"""The other Copy Report buttons hide the Windows user name too.

The failed-imports dialog tells people to copy its report "when filing a bug
on NexusMods or GitHub", and the diagnostic, Find Culprit and Inspect reports
go to mod authors. They copied paths with the user name in them. Only the
copied text is scrubbed; the dialogs still show the reporter their own paths.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")

import qfluentwidgets
from PySide6.QtWidgets import QApplication, QTextEdit, QWidget
from qfluentwidgets import MessageBoxBase, PushButton

from cdumm.gui import fluent_window
from cdumm.gui.fluent_window import CdummWindow
from cdumm.gui.pages.tool_page import FindCulpritPage, InspectModPage
from cdumm.i18n import tr

NAME = "Finlay"
PATH = f"C:\\Users\\{NAME}\\Downloads\\BadMod.zip"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """InfoBar animates onto its parent; these tests only read the clipboard."""
    class _Bar:
        success = warning = error = info = staticmethod(lambda *a, **k: None)
    monkeypatch.setattr(fluent_window, "InfoBar", _Bar)
    # tool_page imports it inside the method, from qfluentwidgets.
    monkeypatch.setattr(qfluentwidgets, "InfoBar", _Bar)


def _click_copy(widget, label):
    buttons = [b for b in widget.findChildren(PushButton) if b.text() == label]
    assert len(buttons) == 1, [b.text() for b in widget.findChildren(PushButton)]
    buttons[0].click()


@pytest.fixture
def dialogs(monkeypatch):
    """Keep the main window's report dialogs from blocking on exec()."""
    shown = []
    monkeypatch.setattr(MessageBoxBase, "exec", lambda self: shown.append(self) or 0)
    return shown


def test_failed_imports_report(qapp, dialogs):
    parent = QWidget()
    CdummWindow._show_import_errors(parent, [f"{PATH}: not a mod archive"])
    (dlg,) = dialogs
    # The dialog still shows the reporter their own path...
    assert any(NAME in t.toPlainText() for t in dlg.findChildren(QTextEdit))
    # ...and the copy hides it.
    QApplication.clipboard().clear()
    _click_copy(dlg, tr("main.copy_report_btn"))
    text = QApplication.clipboard().text()
    assert "Downloads" in text and NAME not in text
    dlg.deleteLater()
    parent.deleteLater()


def test_diagnostic_report(qapp, dialogs):
    parent = QWidget()
    CdummWindow._show_diagnostic_report(parent, "BadMod", f"Source: {PATH}\n")
    (dlg,) = dialogs
    QApplication.clipboard().clear()
    _click_copy(dlg, tr("main.copy_report_btn"))
    text = QApplication.clipboard().text()
    assert "Downloads" in text and NAME not in text
    dlg.deleteLater()
    parent.deleteLater()


def test_find_culprit_report(qapp):
    class _Manager:
        def get_crash_report(self):
            return f"Suspect: {PATH}\n"

    page = FindCulpritPage()
    page._mod_manager = _Manager()
    QApplication.clipboard().clear()
    page._on_copy_report()
    text = QApplication.clipboard().text()
    assert "Downloads" in text and NAME not in text
    page.deleteLater()


def test_inspect_report(qapp):
    page = InspectModPage()
    page._add_diagnostic_card(f"=== FILES ===\n{PATH}\n", "BadMod.zip")
    QApplication.clipboard().clear()
    _click_copy(page, tr("tool.copy_report"))
    text = QApplication.clipboard().text()
    assert "Downloads" in text and NAME not in text
    page.deleteLater()
