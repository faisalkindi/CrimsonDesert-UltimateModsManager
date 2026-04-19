"""HIGH #14: atexit must NOT remove .running lock when the app crashed.

The .running sentinel is how the NEXT launch detects "previous session
crashed — offer to restore state". Python's atexit fires even after
uncaught exceptions, SIGTERM, and Windows shutdown handlers — not just
after a clean closeEvent. Blind unlink in atexit masked every crash.

The fix: closeEvent sets a `clean_shutdown` flag. The atexit hook only
removes the lock when that flag is truthy; otherwise it preserves it
so the next launch sees the crash marker.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.gui.running_lock import (
    install_lock,
    mark_clean_shutdown,
    _cleanup_running_lock,   # unit-test access to the registered hook
)


def test_clean_shutdown_removes_lock(tmp_path: Path):
    state = install_lock(tmp_path / ".running")
    assert state["lock_file"].exists()
    mark_clean_shutdown(state)
    _cleanup_running_lock(state)
    assert not state["lock_file"].exists(), (
        "after clean shutdown, atexit must remove the lock")


def test_crash_preserves_lock(tmp_path: Path):
    state = install_lock(tmp_path / ".running")
    assert state["lock_file"].exists()
    # Simulate atexit firing WITHOUT a closeEvent (e.g. crash).
    _cleanup_running_lock(state)
    assert state["lock_file"].exists(), (
        "crash path must leave .running in place so next launch sees it")


def test_install_records_timestamp(tmp_path: Path):
    state = install_lock(tmp_path / ".running")
    content = state["lock_file"].read_text(encoding="utf-8").strip()
    assert content, "lock file must carry a timestamp"
    # Accept isoformat or plain str representation of datetime.
    assert len(content) > 4


def test_stale_lock_existence_indicates_prior_crash(tmp_path: Path):
    """install_lock returns whether the lock existed before creation."""
    lock = tmp_path / ".running"
    lock.write_text("stale", encoding="utf-8")
    state = install_lock(lock)
    assert state["was_stale"] is True


def test_first_run_is_not_stale(tmp_path: Path):
    state = install_lock(tmp_path / ".running")
    assert state["was_stale"] is False
