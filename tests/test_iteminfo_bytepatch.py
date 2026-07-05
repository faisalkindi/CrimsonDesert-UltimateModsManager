"""Tests for the leading-field byte-patch fallback used when the parser
schema can't fully decode a game version (e.g. CD 1.13 shifted
prefab_data_list — GitHub #247).

Uses the committed vanilla110 fixture. The fixture IS schema-supported,
so we exercise the fallback writer directly (it is layout-independent
for the leading fields) and confirm the version probe classifies the
fixture as supported.
"""
from __future__ import annotations

import struct

import pytest

from tests.fixture_loaders import has_vanilla110, load_vanilla110

from cdumm.engine import iteminfo_writer as W
from cdumm.engine.format3_handler import Format3Intent


def test_canon_field_aliases():
    assert W._canon_field("max_stack_count") == "maxstackcount"
    assert W._canon_field("maxStackCount") == "maxstackcount"
    assert W._canon_field("_maxStackCount") == "maxstackcount"
    assert W._LEADING_BY_CANON["maxstackcount"] == "max_stack_count"


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="1.10 iteminfo fixtures absent")
def test_probe_reports_fixture_supported():
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    assert W._schema_supports_version(body, header) is True


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="1.10 iteminfo fixtures absent")
def test_bytepatch_sets_max_stack_count_byte_exact():
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    by_key, _ = W._iteminfo_record_starts(body, header)
    key = 2200  # Pyeonjeon_Arrow
    assert key in by_key

    intents = [Format3Intent(entry="", key=key, field="max_stack_count",
                             op="set", new=4321)]
    change = W._bytepatch_leading_fields(body, header, intents)
    assert change is not None
    patched = bytes.fromhex(change["patched"])
    assert len(patched) == len(body)

    # exactly the record's max_stack_count u64 is updated
    s = by_key[key]
    sklen = struct.unpack_from("<I", body, s + 4)[0]
    pos = s + 9 + sklen
    assert struct.unpack_from("<Q", patched, pos)[0] == 4321

    # every differing byte lies within that u64
    changed = [i for i in range(len(body)) if body[i] != patched[i]]
    assert changed, "expected some bytes to change"
    assert all(pos <= i < pos + 8 for i in changed)


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="1.10 iteminfo fixtures absent")
def test_bytepatch_skips_deep_fields_without_crashing():
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    intents = [
        Format3Intent(entry="", key=2200, field="max_stack_count",
                      op="set", new=999),
        Format3Intent(entry="", key=2200, field="enchant_data_list",
                      op="set", new=[]),          # deep -> skipped
    ]
    change = W._bytepatch_leading_fields(body, header, intents)
    assert change is not None
    assert "1 applied" in change["label"]
    assert "deep-field skipped" in change["label"]


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="1.10 iteminfo fixtures absent")
def test_bytepatch_unknown_key_returns_none():
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    intents = [Format3Intent(entry="", key=999999999, field="max_stack_count",
                             op="set", new=5)]
    assert W._bytepatch_leading_fields(body, header, intents) is None
