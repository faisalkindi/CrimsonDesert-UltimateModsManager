"""Crash-report generator hardening.

Covers the four improvements to the crash/bug-report path:

  1. Trace survival — main.py preserves the previous session's
     faulthandler dump (``crash_trace.txt`` -> ``crash_trace.prev.txt``)
     BEFORE truncating it on the next launch. Without this the whole
     "CRASH TRACE (previous session)" section was dead for real hard
     crashes: the relaunch wiped the evidence before the user could ever
     generate a report.
  2. Crash-type headline — the faulthandler fault class (e.g. "Windows
     fatal exception: access violation") is surfaced in the TL;DR.
  3. Native-vs-Python flag — native faults are labelled so triagers know
     the Python frames may be unrelated to the real C/C++ crash site
     (the misleading ``splash.py`` trace on the Qt3D crash).
  4. GPU / Qt render diagnostics — safe, registry-only adapter capture.
"""
from __future__ import annotations


# faulthandler dump samples (exact first-line formats CPython emits).
WIN_AV = (
    "Windows fatal exception: access violation\n"
    "\n"
    "Current thread 0x00001234 (most recent call first):\n"
    '  File "splash.py", line 76 in show_splash\n'
)
SEGFAULT = (
    "Fatal Python error: Segmentation fault\n"
    "\n"
    "Current thread 0x00007f00 (most recent call first):\n"
    '  File "viewer.py", line 12 in render\n'
)
PY_TRACEBACK = (
    "\n\n"
    "Traceback (most recent call last):\n"
    '  File "engine.py", line 40 in apply\n'
    "ValueError: boom\n"
)


# ── 1. Trace survival (cdumm.main._preserve_prior_crash_trace) ──────────


def test_preserve_prior_crash_trace_moves_nonempty(tmp_path):
    from cdumm import main as cdumm_main

    trace = tmp_path / "crash_trace.txt"
    trace.write_text(WIN_AV, encoding="utf-8")

    cdumm_main._preserve_prior_crash_trace(trace)

    prev = tmp_path / "crash_trace.prev.txt"
    assert prev.exists(), "non-empty trace must be preserved for the next session"
    assert "access violation" in prev.read_text(encoding="utf-8")
    assert not trace.exists(), "trace must be MOVED (renamed), not copied"


def test_preserve_prior_crash_trace_skips_empty(tmp_path):
    """An empty file left over from a clean run isn't a crash — don't
    preserve it (it must not later claim a crash happened)."""
    from cdumm import main as cdumm_main

    trace = tmp_path / "crash_trace.txt"
    trace.write_text("", encoding="utf-8")

    cdumm_main._preserve_prior_crash_trace(trace)

    assert not (tmp_path / "crash_trace.prev.txt").exists()
    assert trace.exists(), "empty trace is left in place, not moved"


def test_preserve_prior_crash_trace_missing_is_noop(tmp_path):
    from cdumm import main as cdumm_main

    trace = tmp_path / "crash_trace.txt"
    cdumm_main._preserve_prior_crash_trace(trace)  # must not raise

    assert not (tmp_path / "crash_trace.prev.txt").exists()


def test_preserve_prior_crash_trace_keeps_most_recent(tmp_path):
    """A second crash before the report is read overwrites the older
    preserved copy — we keep the most recent previous session."""
    from cdumm import main as cdumm_main

    prev = tmp_path / "crash_trace.prev.txt"
    prev.write_text("OLD trace from two sessions ago", encoding="utf-8")
    trace = tmp_path / "crash_trace.txt"
    trace.write_text("NEW trace from last session", encoding="utf-8")

    cdumm_main._preserve_prior_crash_trace(trace)

    assert prev.read_text(encoding="utf-8") == "NEW trace from last session"


# ── 2 + 3. Headline extraction / native-fault detection ─────────────────


def test_parse_headline_windows_access_violation():
    from cdumm.gui.bug_report import _parse_crash_headline

    headline, native = _parse_crash_headline(WIN_AV)
    assert headline == "Windows fatal exception: access violation"
    assert native is True


def test_parse_headline_segfault_is_native():
    from cdumm.gui.bug_report import _parse_crash_headline

    headline, native = _parse_crash_headline(SEGFAULT)
    assert "Segmentation fault" in headline
    assert native is True


def test_parse_headline_python_traceback_not_native():
    """A plain Python traceback (leading blank lines) yields the first
    non-empty line and is NOT flagged native."""
    from cdumm.gui.bug_report import _parse_crash_headline

    headline, native = _parse_crash_headline(PY_TRACEBACK)
    assert headline == "Traceback (most recent call last):"
    assert native is False


def test_parse_headline_empty():
    from cdumm.gui.bug_report import _parse_crash_headline

    assert _parse_crash_headline("   \n\n  ") == ("", False)


# ── 1 (reader). Candidate ordering: preserved trace first ───────────────


def test_crash_trace_candidates_prev_first(tmp_path):
    from cdumm.gui.bug_report import _crash_trace_candidates

    cands = _crash_trace_candidates(tmp_path)
    assert [p.name for _, p in cands] == [
        "crash_trace.prev.txt", "crash_trace.txt"]
    assert cands[0][0] == "previous session"
    assert cands[1][0] == "this session"


# ── 4. GPU / renderer diagnostics — safe, never raises ──────────────────


def test_windows_gpu_adapters_never_raises_and_shape():
    from cdumm.gui.bug_report import _windows_gpu_adapters

    out = _windows_gpu_adapters()
    assert isinstance(out, list)
    for item in out:
        assert isinstance(item, tuple) and len(item) == 2
        assert isinstance(item[0], str) and isinstance(item[1], str)


def test_renderer_diagnostics_returns_str_lines():
    from cdumm.gui.bug_report import _renderer_diagnostics

    out = _renderer_diagnostics()
    assert isinstance(out, list)
    assert all(isinstance(x, str) for x in out)


# ── Integration: the report wires it all together ───────────────────────


def test_report_includes_preserved_native_crash(tmp_path):
    """A preserved native crash surfaces with headline + native note, and
    the preserved (previous-session) trace wins over any same-session
    dump."""
    from cdumm.gui.bug_report import generate_bug_report

    (tmp_path / "crash_trace.prev.txt").write_text(WIN_AV, encoding="utf-8")
    # A same-session dump also exists; the preserved one must take priority.
    (tmp_path / "crash_trace.txt").write_text(
        "Fatal Python error: Segmentation fault\n(this-session dump)\n",
        encoding="utf-8")

    report = generate_bug_report(None, None, tmp_path)

    assert "--- CRASH TRACE (previous session) ---" in report
    assert "--- CRASH TRACE (this session) ---" not in report
    # Headline reaches the TL;DR (and the body first line).
    assert "Windows fatal exception: access violation" in report
    # Native-fault note is present.
    assert "native fault" in report
    # The lower-priority same-session content must not leak in.
    assert "this-session dump" not in report


def test_report_no_trace_no_crash_section(tmp_path):
    """With no trace files, the report must not claim a crash."""
    from cdumm.gui.bug_report import generate_bug_report

    report = generate_bug_report(None, None, tmp_path)
    assert "CRASH TRACE" not in report
    assert "Crash detected" not in report
