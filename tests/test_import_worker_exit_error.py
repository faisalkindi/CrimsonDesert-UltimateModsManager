"""GitHub #193 (RoGreat, Linux native / NixOS): an nxm download lands a
file in /tmp/cdumm_nxm_*, the progress UI shows, but nothing imports and
there is "no obvious error". Tracing the import-worker completion handler
turned up a false-success hole: _on_finished caught QProcess.CrashExit
but NOT a NormalExit with a non-zero exit code and no emitted JSON
done/error line. A worker that sys.exit(1)s after a caught failure (or
exits non-zero for any reason) without printing a result was treated as
a successful import.

_import_worker_exit_error closes that hole. These tests pin the four
exit shapes so a silently-failing worker is always surfaced with its
exit code instead of a fake "import succeeded".
"""
from __future__ import annotations

from cdumm.gui.fluent_window import _import_worker_exit_error


def test_clean_exit_with_result_is_success():
    """exit 0, no crash, worker emitted a done/error JSON line -> trust
    the worker, no synthetic error."""
    assert _import_worker_exit_error(0, is_crash=False, had_result=True) is None


def test_clean_exit_no_result_is_not_an_error():
    """exit 0 with no emitted result is the legacy success path (some
    importers emit nothing on a no-op). Keep it non-error so existing
    behaviour is unchanged for clean exits."""
    assert _import_worker_exit_error(0, is_crash=False, had_result=False) is None


def test_crash_exit_without_result_is_error():
    msg = _import_worker_exit_error(139, is_crash=True, had_result=False)
    assert msg is not None
    assert "139" in msg


def test_crash_exit_with_result_trusts_the_worker():
    """If the worker managed to emit a JSON error before crashing, the
    existing error handling already has it — don't double-report."""
    assert _import_worker_exit_error(139, is_crash=True, had_result=True) is None


def test_nonzero_normal_exit_without_result_is_error():
    """THE #193 hole: NormalExit, exit code 1, no JSON result. Was
    treated as success; must now surface with the exit code."""
    msg = _import_worker_exit_error(1, is_crash=False, had_result=False)
    assert msg is not None
    assert "1" in msg


def test_nonzero_normal_exit_with_result_trusts_the_worker():
    """A worker that printed a JSON error AND exited non-zero is already
    covered by the error path — don't override its message."""
    assert _import_worker_exit_error(1, is_crash=False, had_result=True) is None
