"""E1: Program Files warning must show a sticky banner every session,
not just a one-time modal.

Issue #30 kai481: game installed under ``C:\\Program Files (x86)\\Steam``,
hit stuck-apply, bug reports didn't mention Program Files because
he'd already dismissed the one-time modal weeks earlier. The
one-time modal is too soft — by the time problems surface, the
warning is ancient history.

Fix: keep the one-time modal (full explanation for first-time users)
but ADD a per-session sticky InfoBar.warning (duration=-1) that
always fires when game_dir is under Program Files. User can close it
for the current session but it returns every launch.

Pure logic is in the detector; the banner firing is a wiring guard.
"""
from __future__ import annotations

import re
from pathlib import Path


# ── Pure-logic helper ────────────────────────────────────────────────

def test_detector_is_public():
    from cdumm.gui import apply_watchdog
    assert hasattr(apply_watchdog, "is_game_in_program_files")


def test_detects_c_program_files():
    from cdumm.gui.apply_watchdog import is_game_in_program_files
    assert is_game_in_program_files(
        r"C:\Program Files\Steam\steamapps\common\Crimson Desert")


def test_detects_c_program_files_x86():
    from cdumm.gui.apply_watchdog import is_game_in_program_files
    assert is_game_in_program_files(
        r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")


def test_detects_case_insensitive():
    """Windows paths can come back mixed-case depending on origin."""
    from cdumm.gui.apply_watchdog import is_game_in_program_files
    assert is_game_in_program_files(
        r"c:\program files (x86)\steam\steamapps\common\Crimson Desert")
    assert is_game_in_program_files(
        r"C:\PROGRAM FILES\Steam\steamapps\common\Crimson Desert")


def test_does_not_match_safe_paths():
    from cdumm.gui.apply_watchdog import is_game_in_program_files
    assert not is_game_in_program_files(
        r"D:\SteamLibrary\steamapps\common\Crimson Desert")
    assert not is_game_in_program_files(
        r"E:\Games\Steam\steamapps\common\Crimson Desert")
    assert not is_game_in_program_files("")
    assert not is_game_in_program_files(None)


def test_does_not_match_similar_directory_names():
    """A folder called 'Programs and Files' somewhere in the path must
    not match. Use segment-level matching, not substring."""
    from cdumm.gui.apply_watchdog import is_game_in_program_files
    assert not is_game_in_program_files(
        r"D:\My Programs\Files\Steam\Crimson Desert")
    assert not is_game_in_program_files(
        r"D:\Program\Files-Backup\Crimson Desert")


# ── Wiring guards ────────────────────────────────────────────────────

def _fluent_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


def test_check_program_files_fires_sticky_infobar_every_session():
    """_check_program_files_warning must always show a sticky
    InfoBar when the game is in Program Files — regardless of the
    one-time modal's dismissed state."""
    src = _fluent_src()
    anchor = src.find("def _check_program_files_warning")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 2500]
    # Must emit a sticky InfoBar.warning (duration=-1).
    assert "InfoBar.warning" in body, (
        "must show a sticky InfoBar.warning every session when game "
        "is in Program Files — the one-time modal is too soft")
    assert "duration=-1" in body, (
        "banner must be sticky (duration=-1) so users see it the "
        "whole session")


def test_banner_uses_shared_detector():
    """The banner path must use the same is_game_in_program_files
    helper as the pure-logic detector — no duplicate substring
    checks that could drift."""
    src = _fluent_src()
    anchor = src.find("def _check_program_files_warning")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 2500]
    assert "is_game_in_program_files" in body, (
        "_check_program_files_warning must call the shared detector "
        "so logic stays in one place")
