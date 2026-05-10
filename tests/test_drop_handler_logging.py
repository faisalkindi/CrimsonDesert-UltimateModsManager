"""Drag-drop diagnostic logging.

Bug #6 from CDUMM Nexus (RubyR47): "Crash whenever I drag a mod to a
folder, this problem NEVER happened in previous version". The submitted
crash report captured a *different* trace (splash.py:76 with COM error
0x8001010d), so the actual drag-drop crash trace never reached us.

Fix: every drag-drop entry point in ``mod_card.py`` is wrapped in a
``try/except`` that logs a full stack trace before re-raising. The
exception still propagates so we don't change visible behaviour, but
the next time a user reproduces the crash, the log file will have an
actionable trace.

This test exercises ``FolderGroup._handle_drop_batch`` (the most likely
crash site, since it owns the layout mutation) by forcing it to raise
and asserting:
  1. the ERROR log line is emitted with "drop-event crash" in it,
  2. the original exception still propagates.
"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_handle_drop_batch_logs_and_reraises(app, qtbot, caplog):
    from cdumm.gui.components.mod_card import FolderGroup

    group = FolderGroup(name="Test", group_id=1)
    qtbot.addWidget(group)

    # Force the very first statement of the body (``_drag_indicator
    # .setVisible(False)``) to raise. This guarantees the try/except
    # we just installed is the layer that catches the exception.
    class _Boom:
        def setVisible(self, _):
            raise RuntimeError("synthetic")

    group._drag_indicator = _Boom()

    with caplog.at_level(logging.ERROR, logger="cdumm.gui.components.mod_card"):
        with pytest.raises(RuntimeError, match="synthetic"):
            group._handle_drop_batch([42])

    # Assert: at least one ERROR record mentioning "drop-event crash"
    error_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "drop-event crash" in r.getMessage()
    ]
    assert error_records, (
        f"expected an ERROR log with 'drop-event crash', got: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # And the traceback was attached (exc_info=True).
    assert any(r.exc_info is not None for r in error_records), (
        "expected exc_info=True on the diagnostic log record"
    )
