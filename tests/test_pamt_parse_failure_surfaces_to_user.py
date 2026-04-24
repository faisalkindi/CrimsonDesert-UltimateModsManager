"""A2: a corrupt mod archive must produce a user-visible warning, not
a silent DEBUG log.

Before v3.1.7 the flow was:

  collect_paz_dir_overrides → parse_pamt raises → logger.debug(skip) →
  apply continues as if nothing happened → user sees "stuck at 2%" for
  7+ minutes (issue #35, UMANLE's Axiom Of Excellence Slim Lacking).

Now the skip must feed the same soft-warning + warning.emit path we
already use for mount-time fallbacks and the overlay-dedup pass. The
GUI's InfoBar picks it up.
"""
from __future__ import annotations

import re
from pathlib import Path


def _apply_engine_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "apply_engine.py").read_text(
                encoding="utf-8")


def test_collect_paz_dir_overrides_accepts_warnings_out():
    """The helper must have an outbound warning channel so the apply
    worker can forward parse failures to the user."""
    src = _apply_engine_src()
    sig_match = re.search(
        r"def collect_paz_dir_overrides\([^)]*\)",
        src, re.DOTALL)
    assert sig_match, "collect_paz_dir_overrides signature not found"
    assert "warnings_out" in sig_match.group(0), (
        "collect_paz_dir_overrides must accept a warnings_out "
        "parameter so the apply worker can forward pamt parse "
        "failures to _soft_warnings / warning.emit")


def test_parse_failure_populates_warnings_out():
    """Inside the except branch, the skip log must also append a
    user-facing message to warnings_out."""
    src = _apply_engine_src()
    # Anchor on the existing skip log.
    anchor = src.find("collect_paz_dir_overrides: skip mod")
    assert anchor != -1, "skip-log anchor not found"
    # Scan a window AROUND the anchor — the append may precede or
    # follow the logger.debug call.
    scope = src[max(0, anchor - 400):anchor + 1200]
    assert "warnings_out" in scope, (
        "except branch must populate warnings_out with a user-facing "
        "message when pamt parse fails")
    assert "append" in scope, (
        "expected warnings_out.append(...) in the except branch")


def test_apply_wires_soft_warnings_and_emits_signal():
    """_apply() must pass self._soft_warnings as warnings_out AND fire
    self.warning.emit for each appended message so the GUI InfoBar
    catches them."""
    src = _apply_engine_src()
    # Anchor on the _apply-internal call to collect_paz_dir_overrides
    # (line ~910, the full-init call site — NOT the resolver fallback).
    call = src.find(
        "self._paz_dir_overrides = collect_paz_dir_overrides(")
    assert call != -1, "primary collect_paz_dir_overrides call not found"
    # Scope: the full call line plus ~1500 chars after.
    scope = src[call:call + 2500]
    assert "warnings_out" in scope, (
        "primary call site must pass warnings_out")
    assert "_soft_warnings" in scope, (
        "warnings must flow into self._soft_warnings")
    assert "self.warning.emit" in scope, (
        "each parse-failure message must fire self.warning.emit so "
        "the GUI InfoBar surfaces it")


def test_user_message_mentions_reimport():
    """The user-facing message must tell the user what to do next:
    re-import the mod from the original archive."""
    src = _apply_engine_src()
    anchor = src.find("collect_paz_dir_overrides: skip mod")
    assert anchor != -1
    scope = src[max(0, anchor - 400):anchor + 1500]
    # Some form of "re-import" or "reimport" guidance must be present.
    assert re.search(r"re-?import", scope, re.IGNORECASE), (
        "parse-failure message must tell the user to re-import the "
        "mod from the original archive")
