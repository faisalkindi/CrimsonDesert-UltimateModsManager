"""Tests for the crash-trace classifier + benign-splash filtering in the
bug report.

Regression: every launch, ``faulthandler`` dumps a *continuable* Windows
COM warning (``0x8001010d`` / ``RPC_E_CANTCALLOUT_ININPUTSYNCCALL``) raised
while the frameless translucent splash first composits. The app recovers
and starts normally, but the leftover ``crash_trace`` made every bug report
headline a phantom "previous session crashed". The classifier recognises
that benign splash-only dump and keeps it out of the red-flag list, while a
real fault (or a benign dump with a real fault stacked after it) still
headlines.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.gui.bug_report import classify_crash_trace, generate_bug_report


# The exact benign dump observed on the user's machine (splash 0x8001010d).
BENIGN = (
    "Windows fatal exception: code 0x8001010d\n"
    "\n"
    "Current thread 0x0000322c (most recent call first):\n"
    '  File "cdumm\\gui\\splash.py", line 113 in show_splash\n'
    '  File "main.py", line 448 in main\n'
    '  File "main.py", line 752 in <module>\n'
    "\n"
    "Current thread's C stack trace (most recent call first):\n"
    "  <cannot get C stack on this system>\n"
)

# A real native fault: access violation deep in the apply engine.
REAL_ACCESS_VIOLATION = (
    "Windows fatal exception: access violation\n"
    "\n"
    "Current thread 0x00001abc (most recent call first):\n"
    '  File "cdumm\\engine\\apply_engine.py", line 512 in _write_delta\n'
    '  File "cdumm\\engine\\apply_engine.py", line 480 in apply\n'
    '  File "main.py", line 448 in main\n'
)


def test_empty_is_empty():
    assert classify_crash_trace("") == "empty"
    assert classify_crash_trace("   \n\t  \n") == "empty"


def test_benign_splash_only():
    assert classify_crash_trace(BENIGN) == "benign"


def test_real_access_violation_is_crash():
    assert classify_crash_trace(REAL_ACCESS_VIOLATION) == "crash"


def test_fatal_python_error_is_crash():
    trace = (
        "Fatal Python error: Segmentation fault\n\n"
        "Current thread 0x0000dead (most recent call first):\n"
        '  File "cdumm\\engine\\crimson_rs_loader.py", line 40 in extract_file\n'
    )
    assert classify_crash_trace(trace) == "crash"


def test_benign_with_real_fault_stacked_is_crash():
    # The splash warning fired first, then a genuine fault later in the
    # same session — must NOT be classified benign.
    assert classify_crash_trace(BENIGN + "\n" + REAL_ACCESS_VIOLATION) == "crash"


def test_same_code_but_not_splash_is_crash():
    # 0x8001010d raised somewhere OTHER than the splash is not the known
    # benign case — fail safe toward "crash".
    trace = (
        "Windows fatal exception: code 0x8001010d\n"
        "\n"
        "Current thread 0x00004444 (most recent call first):\n"
        '  File "cdumm\\gui\\fluent_window.py", line 900 in showEvent\n'
        '  File "main.py", line 448 in main\n'
    )
    assert classify_crash_trace(trace) == "crash"


def test_unrecognised_nonempty_dump_is_crash():
    # Unknown format with content -> be safe, treat as a crash.
    assert classify_crash_trace("something went very wrong\nstack...\n") == "crash"


# ── Integration: the benign trace must not headline the bug report ──────

def test_bug_report_does_not_flag_benign_prev_trace(tmp_path: Path):
    (tmp_path / "crash_trace.prev.txt").write_text(BENIGN, encoding="utf-8")
    report = generate_bug_report(None, None, tmp_path)
    assert "Previous session crashed" not in report
    assert "0x8001010d" in report  # the benign OK note names the code
    assert "not a crash" in report


def test_bug_report_flags_real_prev_trace(tmp_path: Path):
    (tmp_path / "crash_trace.prev.txt").write_text(
        REAL_ACCESS_VIOLATION, encoding="utf-8")
    report = generate_bug_report(None, None, tmp_path)
    assert "Previous session crashed" in report
    assert "access violation" in report


def test_bug_report_prefers_preserved_prev_over_live(tmp_path: Path):
    # The live file holds the current run's benign splash dump; the
    # preserved file holds the previous session's real crash. The report
    # must surface the real previous crash, not the current benign one.
    (tmp_path / "crash_trace.txt").write_text(BENIGN, encoding="utf-8")
    (tmp_path / "crash_trace.prev.txt").write_text(
        REAL_ACCESS_VIOLATION, encoding="utf-8")
    report = generate_bug_report(None, None, tmp_path)
    assert "Previous session crashed" in report
    assert "access violation" in report
