"""falobos76 (#191): the apply "N patches skipped" summary and other
notification text must be recoverable — written to the app log so it lands
in a bug report, and selectable/copyable in the notification panel.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from qfluentwidgets import CaptionLabel

from cdumm.gui import notifications as N


def test_add_writes_the_notification_text_to_the_log(caplog):
    N.store().clear()
    with caplog.at_level(logging.INFO):
        N.store().add("warning", "Apply finished",
                      "12 patches skipped: iteminfo.pabgb")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Apply finished" in text
    assert "12 patches skipped: iteminfo.pabgb" in text


def test_error_notification_logs_at_error_level(caplog):
    N.store().clear()
    with caplog.at_level(logging.DEBUG):
        N.store().add("error", "Import failed", "path too long")
    recs = [r for r in caplog.records if "Import failed" in r.getMessage()]
    assert recs and recs[0].levelno == logging.ERROR


def test_panel_row_message_is_selectable(qtbot):
    N.store().clear()
    N.store().add("warning", "Apply finished", "12 patches skipped")
    panel = N.NotificationPanel()
    qtbot.addWidget(panel)
    panel.refresh()
    labels = [lbl for lbl in panel.findChildren(CaptionLabel)
              if "12 patches skipped" in lbl.text()]
    assert labels, "message label not found in the panel"
    flags = labels[0].textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
