"""Crash-detection lock for CDUMM.

The app writes a `.running` sentinel at start; a clean shutdown via
closeEvent removes it. If the sentinel is still present at the next
launch, the previous session did not exit cleanly — the user gets a
'Previous session crashed' notice and a chance to recover staging
state.

Previously, an atexit hook also removed the lock 'belt-and-suspenders'
in case closeEvent was skipped by a Qt teardown race. But atexit ALSO
fires after uncaught exceptions, SIGTERM, and Windows shutdown hooks
— exactly the cases we WANT to detect. Blind removal masked every
crash.

This module splits the two cases:
  * closeEvent calls mark_clean_shutdown(state)
  * atexit invokes _cleanup_running_lock(state) which only unlinks when
    the clean-shutdown flag was set.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def install_lock(lock_file: Path) -> dict:
    """Create the running-lock sentinel. Returns a state dict that
    closeEvent and atexit handlers use to coordinate.

    The returned dict has:
      * `lock_file`: path to the sentinel
      * `was_stale`: True if the sentinel already existed (prior crash)
      * `clean_shutdown`: False until mark_clean_shutdown() is called

    Concurrency note: a second CDUMM instance launched at exactly the
    same moment WOULD race on read/write of this sentinel. In
    practice main.py's .gui_lock (msvcrt.locking + fcntl.flock) gates
    single-instance enforcement before this is called, so the race
    doesn't manifest in normal use. BMAD B5 documented.
    """
    lock_file = Path(lock_file)
    was_stale = lock_file.exists()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(datetime.now().isoformat(), encoding="utf-8")
    return {
        "lock_file": lock_file,
        "was_stale": was_stale,
        "clean_shutdown": False,
    }


def mark_clean_shutdown(state: dict) -> None:
    """closeEvent calls this when Qt is shutting down normally. Only
    after this flag is set will the atexit handler remove the lock."""
    state["clean_shutdown"] = True


def _cleanup_running_lock(state: dict) -> None:
    """atexit entry point. Removes the sentinel ONLY if we got a clean
    shutdown. On crash paths, leaves the file in place so the next
    launch can detect the previous crash.
    """
    if not state.get("clean_shutdown"):
        return
    lock_file = state.get("lock_file")
    if lock_file is None:
        return
    try:
        Path(lock_file).unlink(missing_ok=True)
    except OSError:
        pass
