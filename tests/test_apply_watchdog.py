"""A1: watchdog that aborts the apply QProcess if progress stalls.

Issue #30, #31, #35 all reduce to the same shape: apply never emits
a further progress message and the user watches the dialog forever.
Fix: a timer in ``_run_qprocess`` that kills the subprocess and
surfaces a clear error if no progress message arrives within a
threshold (default 180s).

Two classes of test:

1. **Pure-logic** — helper that decides "is this stalled?" without Qt.
   Tested here directly.
2. **Wiring guard** — grep the GUI source to prove the QTimer is
   created, its timeout is connected to a watchdog handler, and the
   watchdog kills the QProcess with a useful error.
"""
from __future__ import annotations

import re
from pathlib import Path


# ── Pure-logic helper ────────────────────────────────────────────────

def test_watchdog_helper_is_public():
    """The decide-stall helper must live at module scope so it can be
    unit-tested without standing up a Qt event loop."""
    from cdumm.gui import apply_watchdog  # new module
    assert hasattr(apply_watchdog, "is_apply_stalled")


def test_fresh_apply_is_not_stalled():
    from cdumm.gui.apply_watchdog import is_apply_stalled
    # Last progress 1s ago, threshold 180s → not stalled.
    assert not is_apply_stalled(now=1000.0, last_progress_ts=999.0,
                                threshold_s=180.0)


def test_apply_that_exceeds_threshold_is_stalled():
    from cdumm.gui.apply_watchdog import is_apply_stalled
    # Last progress 200s ago, threshold 180s → stalled.
    assert is_apply_stalled(now=1200.0, last_progress_ts=1000.0,
                            threshold_s=180.0)


def test_boundary_is_not_inclusive():
    """Exactly at threshold is NOT yet stalled. Avoid off-by-one kills
    when the last progress arrived exactly 180s ago."""
    from cdumm.gui.apply_watchdog import is_apply_stalled
    assert not is_apply_stalled(now=1180.0, last_progress_ts=1000.0,
                                threshold_s=180.0)
    # One microsecond over is stalled.
    assert is_apply_stalled(now=1180.000001, last_progress_ts=1000.0,
                            threshold_s=180.0)


def test_build_stall_message_names_last_file():
    """When the watchdog fires, the error message must name the last
    file the user saw progress for so they can diagnose which mod is
    the offender."""
    from cdumm.gui.apply_watchdog import build_stall_message
    msg = build_stall_message(
        phase="apply",
        last_progress_msg="Backing up vanilla files... (3/120) 0042/0.paz",
        threshold_s=180.0)
    assert "0042/0.paz" in msg
    assert "180" in msg or "3 minutes" in msg or "3 min" in msg
    # Must tell user what to do next.
    assert re.search(r"bug report", msg, re.IGNORECASE)


def test_build_stall_message_handles_missing_last_file():
    """If the watchdog fires before any progress message has been
    received, the message must degrade gracefully — no KeyError."""
    from cdumm.gui.apply_watchdog import build_stall_message
    msg = build_stall_message(phase="apply",
                              last_progress_msg=None,
                              threshold_s=180.0)
    assert msg  # non-empty
    assert re.search(r"stall|frozen|no progress", msg, re.IGNORECASE)


# ── Wiring guards (grep the GUI source) ───────────────────────────────

def _fluent_window_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


def test_run_qprocess_creates_watchdog_timer():
    src = _fluent_window_src()
    # Anchor: start of _run_qprocess.
    anchor = src.find("def _run_qprocess(")
    assert anchor != -1
    # Scope: next 3500 chars (body of the method).
    body = src[anchor:anchor + 3500]
    assert "QTimer" in body, (
        "_run_qprocess must create a QTimer to watch for progress "
        "stalls")
    assert "is_apply_stalled" in body or "apply_watchdog" in body, (
        "_run_qprocess must use the apply_watchdog helper")


def test_stdout_handler_updates_last_progress_timestamp():
    src = _fluent_window_src()
    anchor = src.find("def _run_qprocess(")
    assert anchor != -1
    body = src[anchor:anchor + 3500]
    # The _on_stdout inside must record the timestamp of the last
    # progress message so the watchdog can compare against it.
    assert re.search(r"last_progress_ts|_last_progress_time",
                     body), (
        "_on_stdout must record the timestamp of the last progress "
        "message so the watchdog can detect stalls")


def test_watchdog_kills_qprocess_and_shows_error():
    src = _fluent_window_src()
    anchor = src.find("def _run_qprocess(")
    assert anchor != -1
    body = src[anchor:anchor + 4500]
    # Somewhere in the watchdog callback: proc.kill() and an error
    # surfaced to the user.
    assert re.search(r"proc\.kill\(\)", body), (
        "watchdog must kill the stalled QProcess")
    assert re.search(r"InfoBar\.error|error_occurred|"
                     r"build_stall_message",
                     body), (
        "watchdog must surface an error to the user (InfoBar.error "
        "or similar) when it kills the process")
