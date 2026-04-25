"""Regression: when CDUMM is upgraded the patch notes dialog must
actually appear on first launch. Pre-fix the function had a TODO
comment instead of a Qt dialog show call and silently bumped the
saved version with no UI shown — users had no idea what changed in
a new release unless they manually opened Settings to view notes.
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py"
            ).read_text(encoding="utf-8")


def _function_body(src: str, fn_name: str) -> str:
    anchor = src.find(f"def {fn_name}(")
    assert anchor != -1, f"{fn_name} not found in fluent_window.py"
    next_def = src.find("\n    def ", anchor + 20)
    return src[anchor:next_def if next_def != -1 else anchor + 4000]


def test_check_show_update_notes_invokes_patch_notes_dialog():
    """The function must actually show PatchNotesDialog when
    last_seen_version differs from current __version__. A bare TODO
    comment is the bug we are guarding against."""
    body = _function_body(_src(), "_check_show_update_notes")
    assert "PatchNotesDialog" in body, (
        "_check_show_update_notes must construct PatchNotesDialog "
        "(was previously a TODO that did nothing)")
    # The constructed dialog must be invoked (Qt MessageBoxBase has
    # a Qt method to actually open it). Simply constructing the
    # dialog object without invoking it would not display anything.
    assert re.search(r"PatchNotesDialog\(.+?\)\.\w+\(\)", body), (
        "constructed PatchNotesDialog must be opened via a method "
        "call so the dialog actually appears on screen")


def test_check_show_update_notes_still_stamps_version():
    """Whether or not the dialog runs (skipped on fresh install),
    the saved version must be bumped so we do not keep firing on
    every subsequent launch."""
    body = _function_body(_src(), "_check_show_update_notes")
    assert 'config.set("last_seen_version"' in body, (
        "must persist the new version through Config.set so this "
        "function is idempotent across launches")


def test_check_show_update_notes_skips_dialog_on_fresh_install():
    """A first-time user has no reason to see 'what is new in v3.2'
    for a version they just installed. The function should still
    stamp the version (so it is quiet on subsequent launches) but
    the dialog stays hidden."""
    body = _function_body(_src(), "_check_show_update_notes")
    # Some condition must short-circuit the dialog when last_ver
    # is falsy (None / empty string).
    assert (re.search(r"if not last_ver", body)
            or re.search(r"last_ver is None", body)
            or re.search(r"last_ver == [\"']{2}", body)), (
        "must skip the dialog branch when there is no prior "
        "last_seen_version (fresh install case)")


def test_dialog_failure_does_not_crash_launch():
    """Wrap the dialog in a try/except so a Qt rendering issue or
    missing translation key does not prevent CDUMM from starting."""
    body = _function_body(_src(), "_check_show_update_notes")
    assert "try:" in body and "except" in body, (
        "dialog show must be guarded so startup never crashes "
        "because the patch notes dialog could not render")
