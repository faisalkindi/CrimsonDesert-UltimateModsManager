"""Regression: 'Find Culprit' tool crashes on click in v3.2 because
``cdumm.gui.binary_search_dialog`` and ``_AutoBisectWorker`` were
deleted in the v3.0 Fluent rewrite but ``tool_page.py:1270`` still
imports them.

Empirical proof: Priston201 bug report (issue #45) shows
``ModuleNotFoundError: No module named 'cdumm.gui.binary_search_dialog'``
the moment the user clicks "Find Culprit". The whole feature is dead
in v3.2.

These tests pin the module + class + API surface tool_page.py uses
so the regression cannot recur.
"""
from __future__ import annotations

import importlib
import inspect
import queue


# ── Module + class exist ────────────────────────────────────────────


def test_binary_search_dialog_module_importable():
    mod = importlib.import_module("cdumm.gui.binary_search_dialog")
    assert hasattr(mod, "_AutoBisectWorker"), (
        "tool_page.py:1270 imports _AutoBisectWorker from this module")


# ── Constructor matches tool_page.py:1308 invocation ────────────────


def test_worker_constructor_signature_matches_tool_page_call():
    from cdumm.gui.binary_search_dialog import _AutoBisectWorker
    sig = inspect.signature(_AutoBisectWorker.__init__)
    params = list(sig.parameters)
    # Drop self
    assert params[:1] == ["self"]
    # tool_page.py:1308 calls:
    #   _AutoBisectWorker(session, mod_manager, game_dir,
    #                     vanilla_dir, db, asi_mods=asi_mods)
    # Five positional + asi_mods keyword
    expected = ["self", "session", "mm", "game_dir",
                "vanilla_dir", "db"]
    assert params[:6] == expected, (
        f"constructor signature drifted from tool_page.py:1308 call. "
        f"got {params}, expected first six: {expected}")
    assert "asi_mods" in params, (
        "tool_page.py:1308 passes asi_mods= as keyword arg")


# ── Public API tool_page.py reaches into ────────────────────────────


def test_worker_exposes_pause_resume_cancel_and_paused_flag():
    """tool_page.py:1185-1224 calls these methods on the worker.
    Without them the pause/resume/stop buttons crash."""
    from cdumm.gui.binary_search_dialog import _AutoBisectWorker
    assert callable(getattr(_AutoBisectWorker, "cancel", None))
    assert callable(getattr(_AutoBisectWorker, "pause", None))
    assert callable(getattr(_AutoBisectWorker, "resume", None))
    assert callable(getattr(_AutoBisectWorker, "run", None))


def test_worker_instance_has_paused_attribute_and_msg_queue_slot(
        tmp_path):
    """tool_page.py:1217 reads ``worker._paused`` (bool).
    tool_page.py:1313 sets ``worker.msg_queue`` after construction."""
    from cdumm.gui.binary_search_dialog import _AutoBisectWorker

    class _StubMM:
        def list_mods(self):
            return []

    w = _AutoBisectWorker(
        session=None, mm=_StubMM(),
        game_dir=tmp_path, vanilla_dir=tmp_path,
        db=None, asi_mods=None)
    assert hasattr(w, "_paused")
    assert isinstance(w._paused, bool)
    assert w._paused is False  # starts unpaused

    # tool_page.py:1313 attaches its own queue post-init
    w.msg_queue = queue.Queue()
    w.msg_queue.put(("smoke", None))
    assert w.msg_queue.get_nowait() == ("smoke", None)


def test_pause_resume_toggles_paused_flag():
    from cdumm.gui.binary_search_dialog import _AutoBisectWorker

    class _StubMM:
        def list_mods(self):
            return []

    w = _AutoBisectWorker(
        session=None, mm=_StubMM(),
        game_dir=None, vanilla_dir=None, db=None)
    assert w._paused is False
    w.pause()
    assert w._paused is True
    w.resume()
    assert w._paused is False


def test_cancel_sets_internal_cancel_flag():
    from cdumm.gui.binary_search_dialog import _AutoBisectWorker

    class _StubMM:
        def list_mods(self):
            return []

    w = _AutoBisectWorker(
        session=None, mm=_StubMM(),
        game_dir=None, vanilla_dir=None, db=None)
    # Internal flag is private but we just need cancel() to not raise
    w.cancel()
    # Cancelled worker should report itself as cancelled
    assert getattr(w, "_cancelled", None) is True


# ── Message queue protocol ──────────────────────────────────────────


def test_worker_emits_via_msg_queue_not_qt_signals(tmp_path):
    """The worker runs inside a plain ``threading.Thread`` (see
    tool_page.py:1316-1324), NOT inside Qt's thread pool. It must
    communicate via ``msg_queue.put(("log", str))`` etc. Qt signals
    fired from a non-Qt thread are silently dropped."""
    from cdumm.gui.binary_search_dialog import _AutoBisectWorker
    src = inspect.getsource(_AutoBisectWorker)
    assert "msg_queue.put" in src, (
        "worker must publish progress via the msg_queue Python "
        "queue tool_page.py polls at 200ms intervals")
