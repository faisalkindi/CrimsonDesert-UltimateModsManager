"""Format 3 `match` selectors may address nested fields by dotted path.

    {"match": {"drop_default_data.use_socket": 1}, ...}

Each segment resolves with the same four name shapes flat fields already
use. Lists are indexed explicitly; there is deliberately no "any element"
traversal (see ``test_misses_return_none_not_an_exception``).
"""
from __future__ import annotations

from tests.fixture_loaders import load_vanilla113

from cdumm.engine.format3_apply import (
    _decode_records_for_match,
    _lookup_record_field,
    _match_record_keys,
)

REC = {
    "_key": 1,
    "_name": "Some_Helm",
    "equip_type_info": 4242,
    "drop_default_data": {
        "drop_enchant_level": 11,
        "use_socket": 1,
        "socket_valid_count": 0,
        "add_socket_material_item_list": [
            {"item": 1, "value": 200},
            {"item": 2, "value": 400},
        ],
    },
    "enchant_data_list": [
        {"level": 0, "item_effect_info": 7},
        {"level": 1, "item_effect_info": 9},
    ],
}


def test_flat_field_still_works():
    assert _lookup_record_field(REC, "equip_type_info") == 4242


def test_nested_struct_field():
    assert _lookup_record_field(REC, "drop_default_data.use_socket") == 1
    assert _lookup_record_field(
        REC, "drop_default_data.drop_enchant_level") == 11


def test_nested_list_index():
    assert _lookup_record_field(
        REC, "drop_default_data.add_socket_material_item_list.0.item") == 1
    assert _lookup_record_field(
        REC, "drop_default_data.add_socket_material_item_list.1.value") == 400
    assert _lookup_record_field(REC, "enchant_data_list.1.level") == 1


def test_negative_index_counts_from_the_end():
    assert _lookup_record_field(REC, "enchant_data_list.-1.level") == 1


def test_snake_case_segments_resolve_camel_case_record_fields():
    rec = {"dropDefaultData": {"useSocket": 1}}
    assert _lookup_record_field(rec, "drop_default_data.use_socket") == 1


def test_camel_case_segments_resolve_snake_case_record_fields():
    """The other direction. A `match` key must accept every spelling a
    `field` key accepts, or a mod half-works: the write resolves and the
    selector doesn't, so the intent silently applies to nothing."""
    assert _lookup_record_field(REC, "dropDefaultData.useSocket") == 1
    assert _lookup_record_field(REC, "equipTypeInfo") == 4242


def test_underscore_prefixed_camel_case_resolves():
    """The schema/DMM dialect spelling."""
    assert _lookup_record_field(REC, "_equipTypeInfo") == 4242
    assert _lookup_record_field(REC, "_dropDefaultData._useSocket") == 1


def test_separator_insensitive_fallback_still_applies():
    """camelCase word boundaries don't always line up with the parser's
    snake_case: `_equipAbleHash` must reach `equipable_hash` (GitHub #191).
    Shared with the writer's resolver so the two can't drift."""
    rec = {"equipable_hash": 77}
    assert _lookup_record_field(rec, "_equipAbleHash") == 77


def test_exact_camel_to_snake_wins_before_the_fuzzy_fallback():
    """`_aB` maps deterministically to `a_b`; the separator-insensitive
    fallback is only ever reached when that precise rule misses."""
    assert _lookup_record_field({"a_b": 1, "ab": 2}, "_aB") == 1


def test_ambiguous_normalised_match_is_refused():
    """When the precise rule misses, the fuzzy fallback must refuse rather
    than guess. `_equipAbleHash` -> `equip_able_hash` (absent), and the
    normalised form `equipablehash` hits two keys -> no match."""
    rec = {"equipable_hash": 1, "equipablehash": 2}
    assert _lookup_record_field(rec, "_equipAbleHash") is None


def test_underscore_prefixed_segments_resolve():
    rec = {"_drop_default_data": {"_use_socket": 3}}
    assert _lookup_record_field(rec, "drop_default_data.use_socket") == 3


def test_misses_return_none_not_an_exception():
    assert _lookup_record_field(REC, "drop_default_data.nope") is None
    assert _lookup_record_field(REC, "nope.use_socket") is None
    # index off the end of the list
    assert _lookup_record_field(REC, "enchant_data_list.9.level") is None
    # a scalar with path left to walk
    assert _lookup_record_field(REC, "equip_type_info.use_socket") is None
    # non-integer segment on a list -- explicitly NOT "any element"
    assert _lookup_record_field(
        REC, "drop_default_data.add_socket_material_item_list.item") is None


def test_a_real_field_containing_a_dot_wins_over_path_traversal():
    rec = {"a.b": 5, "a": {"b": 9}}
    assert _lookup_record_field(rec, "a.b") == 5


def test_none_valued_field_is_not_confused_with_a_missing_one():
    """A field that genuinely holds None must not fall through to path
    traversal and pick up something else."""
    rec = {"default_sub_item": {"type_id": 17, "value": None}}
    assert _lookup_record_field(rec, "default_sub_item.value") is None


def test_match_on_a_nested_path():
    records = {1: REC, 2: dict(REC, _key=2, drop_default_data={
        "use_socket": 0, "add_socket_material_item_list": []})}
    got = _match_record_keys(records, {"drop_default_data.use_socket": 1})
    assert got == [1]


def test_nested_path_combines_with_any_of_and_with_flat_fields():
    r1 = REC
    r2 = dict(REC, _key=2, equip_type_info=9999)
    records = {1: r1, 2: r2}
    # any-of on the nested value
    assert _match_record_keys(
        records, {"drop_default_data.use_socket": [1, 5]}) == [1, 2]
    # AND across a nested and a flat condition
    assert _match_record_keys(records, {
        "drop_default_data.use_socket": 1,
        "equip_type_info": 9999,
    }) == [2]


# ── live table ──────────────────────────────────────────────────────────

def test_nested_match_selects_socketed_items_on_the_live_table():
    """The whole point: select every socketed item in one intent.

    Runs on the COMMITTED CD 1.13 fixture. It used to look for a
    tests/fixtures/iteminfo/ directory that has never existed, so it
    skipped silently in CI and proved nothing.
    """
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    records = _decode_records_for_match("iteminfo", body, header)

    got = _match_record_keys(records, {"drop_default_data.use_socket": 1})
    expected = [k for k, r in records.items()
                if (r.get("drop_default_data") or {}).get("use_socket") == 1]
    assert got == expected
    assert len(got) > 500, f"only {len(got)} socketed items matched"
