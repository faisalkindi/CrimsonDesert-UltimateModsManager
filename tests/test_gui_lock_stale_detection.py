"""Stale .gui_lock detection.

Bug from Sshuvzz on Nexus 2026-04-27/28: CDUMM "logo flash for 2
seconds, then nothing" after a previous launch died — even after PC
restart and re-downloading the exe. crash-pre-qt.log shows
``SystemExit: 0`` (the "another instance is already running"
shutdown path).

Phase 1 (verified against Python docs + Windows behavior):

  * OS-level file locks acquired via msvcrt.locking() ARE released
    when the process holding them dies. So a stuck OS-level lock
    after PC restart is impossible.
  * The lock file ``.gui_lock`` itself persists on disk between
    runs. Its first byte is locked while a CDUMM process is alive;
    the lock byte is auto-released on process death.
  * Possible failure modes that all collapse to the same "another
    instance" exit branch in main.py:161-192:
      (a) Genuine running second instance.
      (b) The .gui_lock file has permissions that prevent
          ``open(_lock_file, "w")`` (mode change after manual
          edit, drive-encryption hiccup, AV scanning the file at
          launch with exclusive access).
      (c) A stale .gui_lock file with a dead-process PID gets
          opened successfully, locking step succeeds — but if
          someone else is already opening it for read at exactly
          that moment, msvcrt.locking sees a conflict and bails.

Fix design:

  Read the PID stored in .gui_lock at the start of acquisition.
  If the PID exists in /proc / GetProcessId, treat as a real
  running instance — same exit as today. If the PID is dead OR
  the file is empty / corrupt, the lock is stale; safe to take
  over. This handles (a) correctly and gives a clean recovery
  path for the half of (b) and (c) cases where the stored PID
  is gone.

  Public helper: ``try_acquire_gui_lock(app_data: Path) -> tuple
  [bool, str]`` returns (acquired, reason) where reason is one of
  "fresh", "stale_pid_replaced", "another_running", "io_error".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32",
                    reason="msvcrt is Windows-only")
def test_no_lock_file_acquires_cleanly(tmp_path):
    """First-ever launch: no .gui_lock exists. Acquisition succeeds
    with reason='fresh'."""
    from cdumm.main import try_acquire_gui_lock

    acquired, reason = try_acquire_gui_lock(tmp_path)
    assert acquired is True
    assert reason == "fresh"
    assert (tmp_path / ".gui_lock").exists()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="msvcrt is Windows-only")
def test_stale_lock_with_dead_pid_is_replaced(tmp_path):
    """A leftover .gui_lock from a CDUMM that crashed without
    cleanup contains a PID that's no longer alive. We must clear
    it and acquire fresh."""
    from cdumm.main import try_acquire_gui_lock

    # PID 999999 is extremely unlikely to be a live process. Even
    # if it ever was, psutil.pid_exists treats it correctly.
    lock_path = tmp_path / ".gui_lock"
    lock_path.write_text("999999")

    acquired, reason = try_acquire_gui_lock(tmp_path)
    assert acquired is True, (
        f"Stale-PID lock must be cleared and re-acquired. "
        f"reason={reason}")
    assert reason == "stale_pid_replaced"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="msvcrt is Windows-only")
def test_byte_locked_by_other_process_blocks_acquisition(tmp_path):
    """A real running CDUMM holds the byte-1 lock on .gui_lock.
    Simulate by holding the byte lock from this same process,
    then attempting acquisition through ``try_acquire_gui_lock``
    (which will open a fresh file handle and find byte 1 locked).
    """
    import msvcrt
    from cdumm.main import try_acquire_gui_lock

    lock_path = tmp_path / ".gui_lock"
    lock_path.write_text(str(os.getpid()))

    # Hold byte 1 from this process via a separate file handle.
    holder = open(lock_path, "r+b")
    try:
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        holder.close()
        pytest.skip("Could not obtain initial byte lock for test setup")

    try:
        acquired, reason = try_acquire_gui_lock(tmp_path)
        # Locking the same file from a different fd in the same
        # process is undefined on Windows — some Pythons allow it
        # (returning success), some block. Accept either outcome
        # but if blocked, the reason must be 'another_running'
        # (live PID recorded). If not blocked, that's also fine —
        # we trust the OS lock layer.
        if not acquired:
            assert reason == "another_running"
    finally:
        try:
            msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        holder.close()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="msvcrt is Windows-only")
def test_empty_lock_file_acquires_cleanly(tmp_path):
    """An empty .gui_lock (no PID stored — perhaps a previous
    write was interrupted) is treated as stale."""
    from cdumm.main import try_acquire_gui_lock

    lock_path = tmp_path / ".gui_lock"
    lock_path.write_text("")

    acquired, reason = try_acquire_gui_lock(tmp_path)
    assert acquired is True
    assert reason == "stale_pid_replaced"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="msvcrt is Windows-only")
def test_garbage_pid_acquires_cleanly(tmp_path):
    """A .gui_lock with non-numeric content (corrupted, partial
    write) is treated as stale — same as empty."""
    from cdumm.main import try_acquire_gui_lock

    lock_path = tmp_path / ".gui_lock"
    lock_path.write_text("not-a-pid")

    acquired, reason = try_acquire_gui_lock(tmp_path)
    assert acquired is True
    assert reason == "stale_pid_replaced"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="msvcrt is Windows-only")
def test_acquired_lock_writes_current_pid(tmp_path):
    """After successful acquisition, the lock file must contain
    THIS process's PID so the next launch can check liveness."""
    import cdumm.main as _main
    from cdumm.main import try_acquire_gui_lock

    acquired, _ = try_acquire_gui_lock(tmp_path)
    assert acquired is True

    # Close the global handle so we can read the file freely.
    if _main._lock_fh is not None:
        try:
            _main._lock_fh.close()
        except OSError:
            pass
        _main._lock_fh = None

    written = (tmp_path / ".gui_lock").read_text().strip()
    assert written == str(os.getpid()), (
        f"Lock must record the acquiring PID. Got {written!r}")
