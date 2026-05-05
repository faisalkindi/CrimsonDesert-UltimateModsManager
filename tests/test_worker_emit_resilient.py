"""Nexus bug 'CDUMM crashing, NOT THE GAME' (zellmann21b, 2026-05-03):
the worker process crashed with::

    File "cdumm\\worker_process.py", line 23, in _emit
    OSError: [Errno 22] Invalid argument
    During handling of the above exception, another exception occurred:
    ...
    File "cdumm\\worker_process.py", line 799, in worker_main
    File "cdumm\\worker_process.py", line 23, in _emit
    OSError: [Errno 22] Invalid argument

The first OSError is from a stdout write inside _run_apply (parent
pipe gone away, encoding error, etc.). The outer except in
worker_main catches that and calls _emit AGAIN to report the error
upward, which raises the SAME error and propagates unhandled. The
worker process dies without writing a clean exit.

Fix: make _emit catch OSError and fall back to a stderr log line
so the function never raises, and so the outer error handler in
worker_main can complete its sys.exit(1) cleanup.
"""
from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest


def test_emit_swallows_oserror_einval(monkeypatch, capsys):
    """When stdout.write raises OSError [Errno 22] (the EINVAL the
    user reported), _emit must not propagate. The exit-1 fallback
    in worker_main needs _emit to be safe to call again."""
    from cdumm.worker_process import _emit

    class _BrokenStdout:
        def write(self, s):
            err = OSError(22, "Invalid argument")
            raise err

        def flush(self):
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(sys, "stdout", _BrokenStdout())

    # _emit must NOT raise. The function is the boundary that the
    # entire worker error-reporting path crosses; anything raising
    # here breaks the worker's clean exit.
    _emit({"type": "error", "msg": "test"})


def test_emit_swallows_oserror_epipe(monkeypatch):
    """Similar guard for OSError EPIPE (errno 32) which Windows can
    surface when the parent process closes the pipe abruptly."""
    from cdumm.worker_process import _emit

    class _BrokenPipe:
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", _BrokenPipe())
    _emit({"type": "progress", "value": 50})


def test_emit_swallows_typeerror_on_non_serializable(monkeypatch, capsys):
    """If a caller accidentally passes a non-serializable value
    (Path, set, custom object), _emit must not raise. The worker
    error-reporting boundary needs ALL exceptions swallowed, not
    just OSError. Same crash-resilience class as the original
    Nexus zellmann21b bug."""
    from pathlib import Path
    from cdumm.worker_process import _emit

    class _GoodStdout:
        def write(self, s): return len(s)
        def flush(self): pass

    monkeypatch.setattr(sys, "stdout", _GoodStdout())

    # A Path is not JSON-serializable. _emit must not raise.
    _emit({"type": "progress", "path": Path("/tmp/foo")})


def test_emit_normal_path_still_writes(monkeypatch):
    """Sanity: the resilient _emit must still write to stdout when
    stdout is healthy. Don't accidentally suppress all writes."""
    from cdumm.worker_process import _emit

    captured: list[str] = []

    class _GoodStdout:
        def write(self, s):
            captured.append(s)
            return len(s)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", _GoodStdout())
    _emit({"type": "progress", "pct": 25})
    # Should have written exactly one JSON line.
    assert len(captured) == 1
    assert '"pct"' in captured[0] or '"type"' in captured[0]
    assert captured[0].endswith("\n")
