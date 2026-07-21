"""D1: Apply must refuse to run while a detected game update is
outstanding (user hasn't run Rescan yet).

Today the flow is: main.py sets ``startup_context["game_updated"]
= True`` when the stored game_version_fingerprint doesn't match
the live game. fluent_window._check_game_updated shows a
MessageBox asking "Rescan now?" but the user can dismiss it. After
dismissal, _on_apply runs without any check — which sends users
straight into the stale-snapshot slow path that shows up as "stuck
at 2%" in their bug reports.

Fix: gate _on_apply on the flag, surface a sticky InfoBar.error
while blocked, clear the flag in _on_snapshot_finished so the gate
unlocks after a real rescan.
"""
from __future__ import annotations

import re
from pathlib import Path


# ── Pure-logic helper ────────────────────────────────────────────────

def test_helper_exists():
    from cdumm.gui import apply_watchdog
    assert hasattr(apply_watchdog, "is_apply_blocked_by_stale_snapshot")


def test_helper_returns_true_when_game_updated_flag_set():
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_stale_snapshot
    assert is_apply_blocked_by_stale_snapshot({"game_updated": True}) is True


def test_helper_returns_false_when_flag_missing():
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_stale_snapshot
    assert is_apply_blocked_by_stale_snapshot({}) is False


def test_helper_returns_false_when_flag_explicitly_false():
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_stale_snapshot
    assert is_apply_blocked_by_stale_snapshot(
        {"game_updated": False}) is False


def test_helper_accepts_none_startup_context():
    """Belt-and-braces: a caller passing None (e.g. during early
    startup) must not crash the helper."""
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_stale_snapshot
    assert is_apply_blocked_by_stale_snapshot(None) is False


# ── Wiring guards ────────────────────────────────────────────────────

def _fluent_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


def test_on_apply_gates_on_stale_snapshot():
    """_on_apply must check is_apply_blocked_by_stale_snapshot BEFORE
    _run_qprocess fires."""
    src = _fluent_src()
    anchor = src.find("def _on_apply(self)")
    assert anchor != -1
    # Scope: the body of _on_apply until the next def. Find next 'def '
    # to bound the search.
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 4000]
    assert "is_apply_blocked_by_stale_snapshot" in body, (
        "_on_apply must call is_apply_blocked_by_stale_snapshot to "
        "refuse apply when the game was updated but user hasn't "
        "rescanned yet")
    # Must surface an InfoBar.error so the user sees WHY apply is
    # blocked, not just a silent no-op.
    assert "InfoBar.error" in body, (
        "blocked apply must surface a user-visible error")
    # Gate must come BEFORE the _run_qprocess call, otherwise we
    # already started the stalled apply.
    gate_idx = body.find("is_apply_blocked_by_stale_snapshot")
    run_idx = body.find("_run_qprocess(")
    assert gate_idx != -1 and run_idx != -1
    assert gate_idx < run_idx, (
        "the stale-snapshot gate must be checked BEFORE "
        "_run_qprocess is called")


def test_snapshot_finished_clears_flag():
    """Successful rescan must clear startup_context["game_updated"]
    so Apply unlocks without requiring an app restart."""
    src = _fluent_src()
    anchor = src.find("def _on_snapshot_finished")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 4000]
    # Look for the flag being set to a falsy value.
    assert re.search(
        r"_startup_context\[[\"']game_updated[\"']\]\s*=\s*False",
        body), (
        "_on_snapshot_finished must clear the game_updated flag "
        "after successful rescan so Apply unlocks without a restart")


def test_check_game_updated_surfaces_sticky_banner_on_decline():
    """When user declines the Rescan prompt, the page must show a
    sticky InfoBar (duration=-1) so they see the locked state
    instead of a silent non-apply."""
    src = _fluent_src()
    anchor = src.find("def _check_game_updated")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 2000]
    # Either an InfoBar is created here, or the flag persists and
    # _on_apply's gate will trigger on the next click. The latter is
    # acceptable — just assert some sticky surface exists.
    assert ("InfoBar.error" in body or "InfoBar.warning" in body), (
        "when user declines the rescan prompt, _check_game_updated "
        "must show a sticky InfoBar explaining apply is now locked "
        "(not just silently close the dialog)")


# ── Live-fingerprint gate (mid-session game update, #307) ─────────────
#
# The startup game_updated flag is a one-shot computed in main.py at
# launch. A Steam auto-update that lands *while CDUMM is already open*
# leaves the flag False, so the stale-snapshot gate above passes and
# the user patches onto a stale vanilla baseline (crash on next launch,
# GitHub #307). _on_apply must therefore ALSO re-check the live game
# fingerprint against the snapshot's.

def test_live_helper_exists():
    from cdumm.gui import apply_watchdog
    assert hasattr(apply_watchdog, "is_apply_blocked_by_live_game_change")


def test_live_helper_blocks_on_fingerprint_mismatch():
    """A mid-session Steam update changes the live fingerprint; against
    a known, differing snapshot fingerprint, apply must be blocked."""
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_live_game_change
    assert is_apply_blocked_by_live_game_change(
        current_fingerprint="aaaa1111", snapshot_fingerprint="bbbb2222"
    ) is True


def test_live_helper_allows_on_fingerprint_match():
    """A clean apply/revert never changes the exe fingerprint, so a
    matching pair must NOT be treated as an update (no false positive)."""
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_live_game_change
    assert is_apply_blocked_by_live_game_change(
        current_fingerprint="aaaa1111", snapshot_fingerprint="aaaa1111"
    ) is False


def test_live_helper_no_false_positive_when_live_unknown():
    """detect_game_version returned None (Xbox/custom install, missing
    exe): a detection gap must never block a legitimate apply."""
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_live_game_change
    assert is_apply_blocked_by_live_game_change(
        current_fingerprint=None, snapshot_fingerprint="aaaa1111"
    ) is False


def test_live_helper_no_block_when_no_snapshot_fingerprint():
    """No stored snapshot fingerprint -> the separate missing-snapshot
    gate owns that case; this helper must stay out of it."""
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_live_game_change
    assert is_apply_blocked_by_live_game_change(
        current_fingerprint="aaaa1111", snapshot_fingerprint=None
    ) is False


def test_live_helper_both_none_is_false():
    from cdumm.gui.apply_watchdog import is_apply_blocked_by_live_game_change
    assert is_apply_blocked_by_live_game_change(
        current_fingerprint=None, snapshot_fingerprint=None
    ) is False


def test_on_apply_gates_on_live_game_change():
    """_on_apply must ALSO re-check the live game fingerprint (not just
    the one-shot startup flag) before _run_qprocess, so a mid-session
    Steam update is caught (#307)."""
    src = _fluent_src()
    anchor = src.find("def _on_apply(self)")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 4000]
    assert "is_apply_blocked_by_live_game_change" in body, (
        "_on_apply must re-check the live game fingerprint so a "
        "mid-session game update blocks apply (#307)")
    assert "detect_game_version" in body, (
        "the live re-check must use detect_game_version (the same "
        "detector the bug report and startup path use)")
    gate_idx = body.find("is_apply_blocked_by_live_game_change")
    run_idx = body.find("_run_qprocess(")
    assert gate_idx != -1 and run_idx != -1 and gate_idx < run_idx, (
        "the live game-change gate must be checked BEFORE _run_qprocess")
