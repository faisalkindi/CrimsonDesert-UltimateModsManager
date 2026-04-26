"""Defensive: _check_for_updates must survive a stale QThread wrapper.

Bug from priston201 issue #47 (2026-04-25): user crashed with
'Internal C++ object (PySide6.QtCore.QThread) already deleted'
in _check_for_updates. The QThread C++ object was destroyed
(probably by parent widget teardown) before our cleanup lambda
reset self._update_thread to None. Next call to _check_for_updates
hit the stale wrapper and isRunning() raised RuntimeError.

Fix: catch RuntimeError around isRunning() so a stale wrapper is
treated as 'thread not running' and a new check can proceed.
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "cdumm"
            / "gui" / "fluent_window.py").read_text(encoding="utf-8")


def test_check_for_updates_guards_isRunning_against_deleted_thread(
) -> None:
    """The function must wrap the isRunning() call in a try/except
    so a deleted-C++-object RuntimeError doesn't crash the app."""
    src = _src()
    func_start = src.find("def _check_for_updates(self)")
    assert func_start != -1, "_check_for_updates not found"
    # Look at the first ~30 lines of the function
    func_body = src[func_start:func_start + 1500]
    # The fix should catch RuntimeError around isRunning()
    assert "RuntimeError" in func_body, (
        "_check_for_updates must catch RuntimeError around the "
        "isRunning() check — a deleted QThread C++ object raises "
        "RuntimeError when accessed via the Python wrapper")
    # And reset the stored thread reference to None on the
    # exception path so subsequent calls succeed
    assert "_update_thread = None" in func_body, (
        "After catching the deleted-thread RuntimeError, the stale "
        "wrapper must be cleared (self._update_thread = None) so "
        "the next call doesn't hit the same dead reference")
