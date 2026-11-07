"""Every way out of the Bug Report page and dialog hides the Windows user name.

The issue forms tell people the report hides it, and they click Copy to
Clipboard. Only the upload and Report on GitHub used to scrub, so a copied or
saved report posted the real name on a public tracker. The preview now shows
the same text, so what a reporter checks is what gets shared.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")

from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from cdumm.gui import bug_report
from cdumm.gui.bug_report import BugReportDialog, generate_bug_report
from cdumm.gui.pages import bug_report_page
from cdumm.gui.pages.bug_report_page import BugReportPage

NAME = "Finlay"
RAW = (
    f"App Data: C:\\Users\\{NAME}\\AppData\\Local\\cdumm\n"
    f"OSError: 'C:\\\\Users\\\\{NAME}\\\\AppData\\\\x.log'\n"
    f"USERPROFILE=C:\\Users\\{NAME}\n"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_infobar(monkeypatch):
    """InfoBar animates onto its parent; these tests only read the output."""
    class _Bar:
        success = warning = error = info = staticmethod(lambda *a, **k: None)
    monkeypatch.setattr(bug_report, "InfoBar", _Bar)
    monkeypatch.setattr(bug_report_page, "InfoBar", _Bar)


def _save_to(monkeypatch, path):
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(path), "")))


@pytest.fixture
def page(qapp, no_infobar):
    p = BugReportPage()
    p._base_report = RAW
    p._desc_edit.setPlainText(f"It broke in C:\\Users\\{NAME}\\Games")
    yield p
    p.deleteLater()


@pytest.fixture
def dialog(qapp, no_infobar):
    parent = QWidget()
    d = BugReportDialog(RAW, parent=parent)
    d._desc_edit.setPlainText(f"It broke in C:\\Users\\{NAME}\\Games")
    yield d
    d.deleteLater()
    parent.deleteLater()


def test_page_copy_hides_the_name(page, qapp):
    page._copy_report()
    text = qapp.clipboard().text()
    assert NAME not in text
    assert "AppData" in text


def test_page_save_hides_the_name(page, tmp_path, monkeypatch):
    out = tmp_path / "report.txt"
    _save_to(monkeypatch, out)
    page._save_report()
    text = out.read_text(encoding="utf-8")
    assert NAME not in text
    assert "AppData" in text


def test_page_preview_shows_what_is_shared(page):
    page._refresh_preview()
    assert NAME not in page._preview.toPlainText()


def test_dialog_copy_hides_the_name(dialog, qapp):
    dialog._copy()
    text = qapp.clipboard().text()
    assert NAME not in text
    assert "AppData" in text


def test_dialog_save_hides_the_name(dialog, tmp_path, monkeypatch):
    out = tmp_path / "report.txt"
    _save_to(monkeypatch, out)
    dialog._save()
    text = out.read_text(encoding="utf-8")
    assert NAME not in text
    assert "AppData" in text


def test_dialog_preview_hides_the_name_before_and_after_typing(qapp, no_infobar):
    parent = QWidget()
    d = BugReportDialog(RAW, parent=parent)
    assert NAME not in d._text_edit.toPlainText()
    d._desc_edit.setPlainText("typed")
    assert NAME not in d._text_edit.toPlainText()
    assert "typed" in d._text_edit.toPlainText()
    d.deleteLater()
    parent.deleteLater()


def test_page_rebuilds_the_report_each_time_it_opens(qapp, no_infobar, monkeypatch):
    """It was built once with the main window, so a bug hit later went out
    with the start-up log unless the reporter pressed Refresh Report."""
    built = []

    def fake_report(db, game_dir, app_data_dir):
        built.append(1)
        return f"log as of build {len(built)}\n"

    monkeypatch.setattr(bug_report, "generate_bug_report", fake_report)
    p = BugReportPage()
    p.show()
    qapp.processEvents()
    assert "log as of build 1" in p._preview.toPlainText()
    p._desc_edit.setPlainText("typed before leaving")
    p.hide()
    p.show()
    qapp.processEvents()
    assert len(built) == 2
    text = p._compose_report()
    assert "log as of build 2" in text and "typed before leaving" in text
    p.deleteLater()


@pytest.mark.parametrize("home,flagged", [
    ("C:\\Users\\Finlay", False),
    ("C:\\Users\\Fi\u00f1lay", True),
    ("C:\\Users\\\u0641\u064a\u0635\u0644", True),
])
def test_report_says_whether_the_name_is_non_ascii_but_not_what_it_is(
        qapp, monkeypatch, home, flagged):
    """Scrubbing hides a non-ASCII name, which can be the cause of a path bug,
    so the report keeps that one fact."""
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("HOME", home)
    report = generate_bug_report(None, None, None)
    lines = [ln for ln in report.splitlines() if ln.startswith("User name:")]
    assert lines == (["User name: has non-ASCII characters"] if flagged else [])
