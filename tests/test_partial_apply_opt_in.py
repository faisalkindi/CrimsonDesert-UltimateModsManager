"""Opt-in partial apply for .pabgb byte-patch mods.

Bug from Faisal 2026-04-27: XxDman10311xX reported 'Refinement Cost
Reforged' working in JMM but rejected by CDUMM with
"This mod is incompatible with the current game version. 11 byte
patches don't match". Diagnostic showed 7959/7976 patches verified
(99.65%) — only 28 mismatches scattered across cost/scalar fields.

Root cause: json_patch_handler.py:1599-1623 hard-rejects ANY
mismatch on a .pabgb data table. The strict rule was added after
Kliff Wears Damiane V2 crashed the game with 458/464 patches
applied (counts vs entries drifted, structural integrity broken).

Fix: add an opt-in `allow_partial_apply: true` flag at the top
level of the JSON patch manifest. When set, partial mismatches
produce a logged warning instead of aborting, and the verified
patches apply normally. Default behavior unchanged — the Kliff
case still gets rejected because that mod's manifest does NOT
set the flag. Authors of cost-only / scalar-only mods can opt in
and accept the risk.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_patch_data_with_mismatches() -> dict:
    """Synthesize a patch_data dict that will produce both verified
    and mismatched patches when applied. We force the mismatch by
    declaring `original` bytes that won't match the (zeroed) target
    file."""
    return {
        "patches": [{
            "game_file": "gamedata/multichangeinfo.pabgb",
            "changes": [
                # This one will verify (original matches what's in vanilla)
                {"offset": 0, "original": "00000000", "patched": "ff000000"},
                # This one will mismatch (vanilla has 00, mod expects f9)
                {"offset": 8, "original": "f9240100", "patched": "ff000000"},
            ],
        }],
        "game_version": "1.0",
    }


def test_partial_apply_flag_allows_mismatches_through(monkeypatch, tmp_path):
    """With `allow_partial_apply: true`, a .pabgb mod with some
    mismatched patches must NOT be rejected — the verified patches
    apply, the mismatched ones are skipped with a warning."""
    from cdumm.engine import json_patch_handler

    patch_data = _make_patch_data_with_mismatches()
    patch_data["allow_partial_apply"] = True

    # Stub _apply_byte_patches to simulate 1 applied + 1 mismatched
    # Return signature: (applied_count, mismatched_count, relocated_count)
    fake_apply = MagicMock(return_value=(1, 1, 0))
    monkeypatch.setattr(json_patch_handler, "_apply_byte_patches", fake_apply)

    # Helper under test: directly check the rejection decision
    decision = json_patch_handler._should_reject_partial_pabgb(
        game_file="gamedata/multichangeinfo.pabgb",
        applied=1, mismatched=1,
        patch_data=patch_data,
    )
    assert decision is False, (
        "allow_partial_apply=True must NOT reject partial mismatch on .pabgb")


def test_no_flag_still_rejects_partial_mismatch_on_pabgb():
    """Default behavior unchanged: any .pabgb mismatch without the
    opt-in flag rejects. Kliff Wears Damiane regression guard."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    patch_data = _make_patch_data_with_mismatches()
    # No allow_partial_apply key

    decision = _should_reject_partial_pabgb(
        game_file="gamedata/multichangeinfo.pabgb",
        applied=458, mismatched=6,
        patch_data=patch_data,
    )
    assert decision is True, (
        "Without the opt-in flag, partial mismatch on .pabgb must "
        "still reject. Kliff Wears Damiane crashed the game at 6/464.")


def test_no_flag_no_mismatches_does_not_reject():
    """Trivial: zero mismatches never rejects regardless of flag."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    decision = _should_reject_partial_pabgb(
        game_file="gamedata/multichangeinfo.pabgb",
        applied=100, mismatched=0,
        patch_data={},
    )
    assert decision is False


def test_flag_is_false_still_rejects():
    """Explicit `allow_partial_apply: false` behaves like the absent
    flag — strict rejection."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    decision = _should_reject_partial_pabgb(
        game_file="gamedata/multichangeinfo.pabgb",
        applied=1, mismatched=1,
        patch_data={"allow_partial_apply": False},
    )
    assert decision is True


def test_non_pabgb_file_not_subject_to_strict_rule():
    """Non-data-table files (e.g. .xml, .json) don't trigger the
    strict rule at all — the helper is .pabgb / .pabgh / .pamt only."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    decision = _should_reject_partial_pabgb(
        game_file="ui/something.xml",
        applied=1, mismatched=1,
        patch_data={},
    )
    assert decision is False


def test_pabgh_and_pamt_also_strict():
    """Companion data tables (.pabgh / .pamt) follow the same strict
    rule — they share the count/entry coupling that broke Kliff."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    for ext in ("pabgh", "pamt"):
        decision = _should_reject_partial_pabgb(
            game_file=f"gamedata/something.{ext}",
            applied=10, mismatched=1,
            patch_data={},
        )
        assert decision is True, f".{ext} must follow strict rule"


def test_modinfo_can_carry_flag(tmp_path):
    """Some mods declare metadata via modinfo.json instead of
    inlining it in the patch JSON. The flag must work either place."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    # Flag in patch_data top-level (most direct)
    decision = _should_reject_partial_pabgb(
        game_file="gamedata/multichangeinfo.pabgb",
        applied=1, mismatched=1,
        patch_data={"modinfo": {"allow_partial_apply": True}},
    )
    assert decision is False, (
        "Flag in modinfo.allow_partial_apply must also disable strict rule")
