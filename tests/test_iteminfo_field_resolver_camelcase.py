"""Iteminfo field-name resolver must bridge camelCase intent names to
the parser's snake_case fields even when the mechanical word split does
not line up.

Bug 2026-06-13 (pinapana, GitHub #191): AbyssGearUnlock_v1.json carries
190 set intents on field `_equipAbleHash`. The resolver stripped the
underscore prefix and split camelCase to `equip_able_hash`, but the
parser exposes the field as the one-word `equipable_hash`. No match, so
all 190 intents skipped and the whole mod produced "0 byte changes".
This looked like the 1.11 parser break (which v3.3.22 fixed) but was a
separate resolver gap. The fix adds a separator-insensitive fallback
that meets both spellings at `equipablehash`, accepting only an
unambiguous single match.
"""
from __future__ import annotations

from cdumm.engine.iteminfo_writer import _resolve_field_name


def test_camelcase_word_boundary_mismatch_resolves_via_normalized_form():
    item = {"equipable_hash": 0, "max_stack_count": 1}
    assert _resolve_field_name("_equipAbleHash", item) == "equipable_hash"


def test_direct_and_stripped_matches_still_win_first():
    item = {"is_blocked": 0, "equipable_hash": 0}
    # exact match
    assert _resolve_field_name("is_blocked", item) == "is_blocked"
    # underscore-prefixed snake passes through the strip path
    assert _resolve_field_name("_is_blocked", item) == "is_blocked"


def test_clean_camelcase_split_still_resolves():
    item = {"is_blocked": 0}
    assert _resolve_field_name("_isBlocked", item) == "is_blocked"


def test_ambiguous_normalized_match_is_refused():
    # Two distinct fields collapse to the same normalized form; the
    # resolver must not guess between them.
    item = {"foo_bar": 0, "foobar": 1}
    assert _resolve_field_name("_fooBar", item) == "foo_bar"  # clean split wins
    assert _resolve_field_name("_foObAr", item) is None       # only normalized; ambiguous


def test_unknown_field_returns_none():
    item = {"equipable_hash": 0}
    assert _resolve_field_name("_totallyMadeUpField", item) is None
