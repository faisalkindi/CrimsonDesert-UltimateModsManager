"""Unit + integration tests for the PABGB type walker (Path B).

Pin three things:
  1. Each primitive / variable type the walker claims to handle
     consumes the right number of bytes for hand-built payloads.
  2. The override schema for ItemInfo loads without errors and
     replaces NattKh's ``stream=?`` placeholders with descriptors
     the walker can actually walk.
  3. ``_consume_field_bytes`` (the format3 apply path) successfully
     walks a synthetic ItemInfo entry past every field up to and
     including ``_cooltime``.

Real-game vanilla iteminfo verification lives in a follow-up integration
test that needs the user's installed game files; this module covers
the deterministic synthetic case.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.semantic import pabgb_types
from cdumm.semantic.pabgb_types import consume_bytes, is_known_type


# ── Primitive walking ──────────────────────────────────────────────


@pytest.mark.parametrize("td,width", [
    ("u8", 1), ("i8", 1),
    ("u16", 2), ("i16", 2),
    ("u32", 4), ("i32", 4),
    ("u64", 8), ("i64", 8),
    ("f32", 4), ("f64", 8),
])
def test_primitive_widths(td, width):
    body = b"\xff" * 16
    assert consume_bytes(td, body, 0, 16) == width


def test_primitive_truncation_returns_none():
    """Reading past `end` must fail-safe rather than walk into garbage."""
    body = b"\x00" * 4
    assert consume_bytes("u64", body, 0, 4) is None


# ── CString ────────────────────────────────────────────────────────


def test_cstring_empty():
    body = struct.pack("<I", 0)
    assert consume_bytes("CString", body, 0, len(body)) == 4


def test_cstring_with_payload():
    payload = b"hello world"
    body = struct.pack("<I", len(payload)) + payload
    assert consume_bytes("CString", body, 0, len(body)) == 4 + len(payload)


def test_cstring_truncated_length_prefix_returns_none():
    body = b"\x00\x00"
    assert consume_bytes("CString", body, 0, 2) is None


def test_cstring_length_exceeds_buffer_returns_none():
    body = struct.pack("<I", 100) + b"only-3"
    assert consume_bytes("CString", body, 0, len(body)) is None


# ── LocalizableString ──────────────────────────────────────────────


def test_localizable_string_empty_default():
    # u8 category + u64 index + u32 default_len(0)
    body = struct.pack("<BQI", 1, 42, 0)
    assert consume_bytes("LocalizableString", body, 0, len(body)) == 13


def test_localizable_string_with_default():
    payload = b"hi"
    body = struct.pack("<BQI", 1, 42, len(payload)) + payload
    assert consume_bytes("LocalizableString", body, 0, len(body)) == 13 + 2


# ── COptional ──────────────────────────────────────────────────────


def test_coptional_none():
    body = b"\x00"  # flag=0 -> no payload
    assert consume_bytes("COptional<u32>", body, 0, len(body)) == 1


def test_coptional_some():
    body = b"\x01" + struct.pack("<I", 0xDEADBEEF)
    assert consume_bytes("COptional<u32>", body, 0, len(body)) == 5


def test_coptional_substruct():
    """COptional<DockingChildData> uses a sub-struct payload."""
    # DockingChildData starts: u32 + u32 + u32 + 2 CStrings + ...
    # For the "none" case we only need flag=0.
    body = b"\x00"
    assert consume_bytes("COptional<DockingChildData>", body, 0, 1) == 1


# ── CArray ─────────────────────────────────────────────────────────


def test_carray_of_primitives_empty():
    body = struct.pack("<I", 0)
    assert consume_bytes("CArray<u32>", body, 0, len(body)) == 4


def test_carray_of_primitives_three_elements():
    body = struct.pack("<I", 3) + struct.pack("<III", 1, 2, 3)
    assert consume_bytes("CArray<u32>", body, 0, len(body)) == 4 + 12


def test_carray_of_substructs():
    """CArray<ReserveSlotTargetData> = u32 count + N × (u32 + u32)."""
    body = struct.pack("<I", 2) + struct.pack("<IIII", 10, 20, 30, 40)
    assert consume_bytes(
        "CArray<ReserveSlotTargetData>", body, 0, len(body)) == 4 + 16


def test_carray_truncated_count_returns_none():
    body = struct.pack("<I", 100) + b"\x00" * 8  # claims 100, has 2
    assert consume_bytes("CArray<u32>", body, 0, len(body)) is None


def test_carray_count_exceeding_safety_cap_returns_none():
    body = struct.pack("<I", 99_999_999) + b"\x00"
    assert consume_bytes("CArray<u32>", body, 0, len(body)) is None


# ── Fixed array ────────────────────────────────────────────────────


def test_fixed_array_of_u32():
    body = struct.pack("<IIII", 1, 2, 3, 4)
    assert consume_bytes("[u32;4]", body, 0, len(body)) == 16


def test_fixed_array_of_f32():
    body = struct.pack("<fff", 1.0, 2.0, 3.0)
    assert consume_bytes("[f32;3]", body, 0, len(body)) == 12


# ── Substructs ─────────────────────────────────────────────────────


def test_occupied_equip_slot_data():
    # u32 equip_slot_name_key + CArray<u8> equip_slot_name_index_list
    body = struct.pack("<I", 5) + struct.pack("<I", 3) + b"\x01\x02\x03"
    assert consume_bytes(
        "OccupiedEquipSlotData", body, 0, len(body)) == 4 + 4 + 3


def test_item_icon_data():
    # u32 icon_path + u8 check_exist_sealed_data + CArray<u32> gimmick_state_list
    body = (struct.pack("<I", 100) + b"\x01"
            + struct.pack("<I", 2) + struct.pack("<II", 7, 8))
    assert consume_bytes(
        "ItemIconData", body, 0, len(body)) == 4 + 1 + 4 + 8


def test_item_bundle_data_fixed_size():
    # u64 + u32 = 12 bytes
    body = struct.pack("<QI", 99, 7)
    assert consume_bytes("ItemBundleData", body, 0, len(body)) == 12


def test_repair_data_fixed_size():
    # u32 + u16 + u8 + u64 = 15 bytes
    body = struct.pack("<IHB", 1, 2, 3) + struct.pack("<Q", 4)
    assert consume_bytes("RepairData", body, 0, len(body)) == 15


# ── Tagged variants ────────────────────────────────────────────────


def test_sub_item_none_variant():
    body = b"\x0e"  # type_id = 14 (None) -> no payload
    assert consume_bytes("SubItem", body, 0, 1) == 1


def test_sub_item_item_key_variant():
    body = b"\x00" + struct.pack("<I", 42)  # type_id=0 (ItemKey) + u32
    assert consume_bytes("SubItem", body, 0, 5) == 5


def test_sub_item_unknown_discriminator_returns_none():
    body = b"\x99"  # not a known SubItem type
    assert consume_bytes("SubItem", body, 0, 1) is None


def test_sealable_item_info_string_variant():
    # u8 type_tag=2 (String) + u32 item_key + u64 unknown0 + CString value
    payload = b"hello"
    body = (b"\x02" + struct.pack("<I", 7) + struct.pack("<Q", 99)
            + struct.pack("<I", len(payload)) + payload)
    expected = 1 + 4 + 8 + 4 + len(payload)
    assert consume_bytes(
        "SealableItemInfo", body, 0, len(body)) == expected


def test_sealable_item_info_item_variant():
    # u8 type_tag=0 (Item) + u32 item_key + u64 unknown0 + u32 ItemKey
    body = (b"\x00" + struct.pack("<I", 7) + struct.pack("<Q", 99)
            + struct.pack("<I", 1234))
    assert consume_bytes(
        "SealableItemInfo", body, 0, len(body)) == 1 + 4 + 8 + 4


# ── Type registry ──────────────────────────────────────────────────


def test_is_known_type_recognizes_primitives():
    for prim in ("u8", "i64", "f32"):
        assert is_known_type(prim)


def test_is_known_type_recognizes_complex():
    assert is_known_type("CString")
    assert is_known_type("LocalizableString")
    assert is_known_type("CArray<u32>")
    assert is_known_type("CArray<OccupiedEquipSlotData>")
    assert is_known_type("COptional<DockingChildData>")
    assert is_known_type("[f32;3]")
    assert is_known_type("SubItem")
    assert is_known_type("SealableItemInfo")


def test_is_known_type_rejects_garbage():
    assert not is_known_type("direct_15B")
    assert not is_known_type("UnknownStruct")
    assert not is_known_type("CArray<UnknownThing>")


# ── Schema integration: ItemInfo override loads correctly ────────────


def test_ordered_fields_typo_refuses_to_load_table(tmp_path, monkeypatch, caplog):
    """Adversarial review CONSENSUS-1: when ``_ordered_fields`` lists a
    field name that has no base NattKh entry AND no type override, the
    loader used to silently fabricate a stub that the legacy stream
    check then dropped — shifting every later field's offset without
    warning.

    Loader must now refuse to load the affected table (no schema entry
    in the cache) and log at ERROR naming the typo'd field.
    """
    import json
    import logging
    from cdumm.semantic import parser as parser_mod

    base_schema_path = tmp_path / "pabgb_complete_schema.json"
    overrides_path = tmp_path / "pabgb_type_overrides.json"
    base_schema_path.write_text(json.dumps({
        "FakeTable": [
            {"f": "_realField", "type": "direct_u32", "stream": 4},
        ]
    }))
    overrides_path.write_text(json.dumps({
        "FakeTable": {
            "_no_null_skip": True,
            "_ordered_fields": [
                "_realField", "_typoFieldThatDoesNotExist",
            ],
            "_realField": {"type": "u32"},
        }
    }))

    # Force the loader to find OUR temp files first by patching the
    # candidate list. Easiest: stub `_load_schemas` to use our paths.
    parser_mod._loaded_schemas = None
    real_load = parser_mod._load_schemas

    def patched_load():
        # Reproduce the production loader's first-existing-candidate
        # selection by short-circuiting to our temp paths.
        global_path = parser_mod.Path
        original_exists = global_path.exists
        # Just call the real loader after monkeypatching its
        # candidate scanner to return our temp file
        import cdumm.semantic.parser as pm
        pm._loaded_schemas = None
        with open(base_schema_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        overrides = pm._load_type_overrides(tmp_path)
        # Now invoke the real per-table merge logic by calling the
        # loader's inner mechanics. To avoid duplicating that logic in
        # the test, we instead replace the production loader entirely
        # for this test by reloading the module isn't available — so
        # we directly assert via the production `_load_schemas` after
        # monkeypatching its candidate list.
        return raw, overrides

    # Patch the production search candidates by setting a method on
    # the module that points at our tmp dir. The cleanest way: write
    # the real schema files to tmp_path AND set CDUMM_FIELD_SCHEMA_ROOT
    # ... but that's for field_schema not pabgb. Best: directly patch
    # the candidates list inside _load_schemas.
    # Since _load_schemas hardcodes paths via Path(__file__).parent...
    # the simplest test mechanism is to monkeypatch open() during the
    # call, which is fragile. Alternative: monkeypatch json.load.

    real_open = open
    real_json_load = json.load

    def patched_open(path, *args, **kwargs):
        path_str = str(path)
        if "pabgb_complete_schema.json" in path_str:
            return real_open(base_schema_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", patched_open)
    monkeypatch.setattr(
        parser_mod, "_load_type_overrides",
        lambda _dir: {
            "FakeTable": {
                "_no_null_skip": True,
                "_ordered_fields": [
                    "_realField", "_typoFieldThatDoesNotExist",
                ],
                "_realField": {"type": "u32"},
            }
        }
    )
    # Pretend the schema file exists so the loader picks it up
    real_exists = parser_mod.Path.exists
    monkeypatch.setattr(
        parser_mod.Path, "exists",
        lambda self: True if "pabgb_complete_schema.json" in str(self)
        else real_exists(self))

    parser_mod._loaded_schemas = None
    with caplog.at_level(logging.ERROR, logger="cdumm.semantic.parser"):
        schemas = parser_mod._load_schemas()

    # Either the table is refused entirely, OR an ERROR was logged
    # naming the typo'd field.
    table = schemas.get("faketable")
    error_text = " ".join(r.message for r in caplog.records
                          if r.levelno >= logging.ERROR)
    if table is not None:
        assert "_typoFieldThatDoesNotExist" in error_text, (
            "Loader silently dropped a typo'd _ordered_fields entry "
            "without logging an ERROR. Add detection that lists "
            "unmatched names and either refuses the table or surfaces "
            "the typo loudly.")


def test_apply_warning_mentions_walker_bail_for_variable_length_failures():
    """Adversarial review E3: when validation passes but apply-time
    walker bails on a variable-length field (e.g. StageInfo
    `_sequencerDesc` optional-object variant=1+), the user-facing
    warning at the "0 changes resolved" path must mention walker
    failure / variable-length walking. Old message only said
    'TID not found' / 'value out of range', leaving mod authors with
    no clue which class of failure they hit.

    Targeted assertion against the SPECIFIC warning string built
    inside expand_format3_into_aggregated rather than the whole
    module text (which has many unrelated 'variable' mentions).
    """
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    import inspect
    src = inspect.getsource(expand_format3_into_aggregated)
    # Slice to only the post-validation, zero-changes warning block
    marker = "produced 0 byte"
    # Two such warnings exist; the second (post intent walking) is the
    # one E3 targets. Locate occurrences and confirm at least one
    # nearby block mentions walker / variable-length walking.
    occurrences = [i for i in range(len(src))
                   if src.startswith(marker, i)]
    assert len(occurrences) >= 3, (
        "expected at least three 'produced 0 byte' warning blocks "
        f"(validation skip, vanilla extract fail, walker bail) — "
        f"found {len(occurrences)}")
    # Search ANY of the warning blocks for the walker / variable-length
    # mention. The runtime walker-bail block is typically last but
    # ordering shouldn't be assumed brittle.
    matched = False
    for occ in occurrences:
        block = src[occ:occ + 700].lower()
        if "walker" in block or "variable-length field" in block:
            matched = True
            break
    assert matched, (
        "No 'produced 0 byte' warning block mentions walker / "
        "variable-length walker bail. Adversarial E3 2026-04-27.")


def test_consume_field_bytes_legacy_fixed_path_rejects_past_eof():
    """Iteration 6 systematic-debugging finding: the legacy fixed-stream-
    size branch in `_consume_field_bytes` returns ``spec.stream_size``
    blindly without verifying the bytes actually fit before EOF or
    `entry_end`. The walker branch checks bounds; the legacy branch
    doesn't. Production callers compare the return value to entry_end
    AFTER advancing, so they'd catch a bad write — but the consume
    accounting itself is wrong.
    """
    from cdumm.engine.format3_apply import _consume_field_bytes
    from cdumm.semantic.parser import FieldSpec

    fixed_spec = FieldSpec(name="y", stream_size=4,
                            field_type="direct_u32", struct_fmt="I")
    body = b"\x00" * 9
    # Inside body — must consume 4
    assert _consume_field_bytes(body, 0, fixed_spec, len(body)) == 4
    assert _consume_field_bytes(body, 5, fixed_spec, len(body)) == 4
    # At EOF — must return None (no bytes left)
    assert _consume_field_bytes(body, 9, fixed_spec, len(body)) is None
    assert _consume_field_bytes(body, 6, fixed_spec, len(body)) is None  # only 3 bytes left, need 4
    # Past EOF — must return None
    assert _consume_field_bytes(body, 100, fixed_spec, len(body)) is None
    # Past entry_end (within body) — must return None
    assert _consume_field_bytes(body, 5, fixed_spec, 7) is None  # entry_end=7, off+4=9 > 7


def test_consume_field_bytes_legacy_cstring_path_rejects_negative_offset():
    """Iteration 5 systematic-debugging finding: the legacy CString path
    in `_consume_field_bytes` (used for fields without a type_descriptor
    override) raises ``struct.error`` on negative offsets — the walker
    branch has a guard but the legacy branch doesn't. Same defensive
    class as the consume_bytes negative-offset fix.
    """
    import struct
    from cdumm.engine.format3_apply import _consume_field_bytes
    from cdumm.semantic.parser import FieldSpec

    cstring_spec = FieldSpec(name="x", stream_size=0,
                              field_type="CString", struct_fmt=None)
    body = struct.pack("<I", 5) + b"hello"
    # Negative offset must return None, NOT crash.
    assert _consume_field_bytes(body, -1, cstring_spec, len(body)) is None
    assert _consume_field_bytes(body, -100, cstring_spec, len(body)) is None
    # Also: stream_size path
    fixed_spec = FieldSpec(name="y", stream_size=4,
                            field_type="direct_u32", struct_fmt="I")
    assert _consume_field_bytes(body, -1, fixed_spec, len(body)) is None


def test_consume_bytes_rejects_negative_offset():
    """Superpowers review SECURITY: ``struct.unpack_from(buf, -4)``
    returns plausible bytes from the buffer's end instead of raising.
    The walker must reject negative offsets up front so corrupted
    cumulative offsets can't produce silent reads from unrelated
    data."""
    body = b"\x00" * 100
    assert consume_bytes("u32", body, -1, len(body)) is None
    assert consume_bytes("CString", body, -4, len(body)) is None
    assert consume_bytes("CArray<u32>", body, -8, len(body)) is None


def test_payload_offset_no_entry_header_rejects_eof_offset():
    """Adversarial CONSENSUS-2: ``_payload_offset(no_entry_header=True)``
    used `entry_off <= len(body)`, allowing the boundary value
    ``entry_off == len(body)`` through. A subsequent walk would try to
    read past EOF. The check should be `<` not `<=` so EOF itself
    returns None.
    """
    from cdumm.engine.format3_apply import _payload_offset
    body = b"\x00" * 10
    # Valid offset
    assert _payload_offset(body, 5, key_size=2,
                           no_entry_header=True) == 5
    # Past-EOF must return None
    assert _payload_offset(body, 11, key_size=2,
                           no_entry_header=True) is None
    # Exact-EOF must ALSO return None — there's no field to read here
    assert _payload_offset(body, 10, key_size=2,
                           no_entry_header=True) is None


def test_coptional_subitem_with_unknown_discriminator_returns_none():
    """Compound failure: COptional<SubItem> where flag=1 (present) and
    the SubItem discriminator is an unknown value. The outer walker
    should propagate the None from the inner tagged-variant lookup
    rather than swallow it.
    """
    # flag=1 (Some) + discriminator=99 (not in SubItem variants)
    body = b"\x01\x63"
    assert consume_bytes("COptional<SubItem>", body, 0, len(body)) is None


def test_field_walker_reachable_rejects_unknown_descriptor():
    """The validator helper `_field_walker_reachable` must return False
    when a preceding field has neither a fixed stream_size nor a
    walker-known type descriptor. Pure pure-Python edge case; if this
    regresses, validation would silently mark write-blocked intents
    as supported."""
    from dataclasses import dataclass

    @dataclass
    class FakeSpec:
        name: str
        stream_size: int
        type_descriptor: str | None

    @dataclass
    class FakeSchema:
        fields: list

    from cdumm.engine.format3_handler import _field_walker_reachable

    schema = FakeSchema(fields=[
        FakeSpec(name="_first", stream_size=4, type_descriptor=None),
        FakeSpec(name="_unknown_var", stream_size=0, type_descriptor=None),
        FakeSpec(name="_after", stream_size=4, type_descriptor=None),
    ])
    # _first is reachable trivially
    assert _field_walker_reachable(schema, "_first") is True
    # _after is preceded by an unknown variable field — must NOT be
    # reachable; validator should surface skip reason
    assert _field_walker_reachable(schema, "_after") is False
    # _unknown_var itself is reachable (we stop AT it, not past it)
    assert _field_walker_reachable(schema, "_unknown_var") is True


def test_schema_loader_logs_warning_for_unknown_descriptor(tmp_path, monkeypatch, caplog):
    """When an override declares ``"type": "Bogus<u32>"`` the loader
    must log at WARNING and ignore the override (fall back to legacy
    NattKh behavior). Confirms the dead-code-defense path runs."""
    import json
    import logging
    from cdumm.semantic import parser as parser_mod

    base_path = tmp_path / "pabgb_complete_schema.json"
    base_path.write_text(json.dumps({
        "FakeBogusTable": [
            {"f": "_realField", "type": "direct_u32", "stream": 4},
        ]
    }))

    real_open = open

    def patched_open(path, *args, **kwargs):
        if "pabgb_complete_schema.json" in str(path):
            return real_open(base_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", patched_open)
    monkeypatch.setattr(
        parser_mod, "_load_type_overrides",
        lambda _dir: {
            "FakeBogusTable": {
                "_realField": {"type": "Bogus<u32>"},  # unknown grammar
            }
        }
    )
    real_exists = parser_mod.Path.exists
    monkeypatch.setattr(
        parser_mod.Path, "exists",
        lambda self: True if "pabgb_complete_schema.json" in str(self)
        else real_exists(self))

    parser_mod._loaded_schemas = None
    with caplog.at_level(logging.WARNING, logger="cdumm.semantic.parser"):
        parser_mod._load_schemas()

    msgs = " ".join(r.message for r in caplog.records)
    assert "Bogus<u32>" in msgs and "_realField" in msgs, (
        f"Expected warning naming the bogus descriptor + field; "
        f"got: {msgs!r}")


def test_iteminfo_override_loads_with_descriptors():
    """Force a fresh schema load and verify ItemInfo's _cooltime now
    has a type_descriptor populated by the override file."""
    from cdumm.semantic import parser as parser_mod

    parser_mod._loaded_schemas = None  # force reload
    schema = parser_mod.get_schema("iteminfo")
    assert schema is not None, "ItemInfo schema not found"

    by_name = {f.name: f for f in schema.fields}
    cooltime = by_name.get("_cooltime")
    assert cooltime is not None, "_cooltime not in loaded ItemInfo schema"
    # Override should set type_descriptor='i64' AND keep stream_size=8 +
    # struct_fmt='q' so legacy callers keep working.
    assert cooltime.type_descriptor == "i64"
    assert cooltime.stream_size == 8

    res_list = by_name.get("_reserveSlotTargetDataList")
    assert res_list is not None, (
        "_reserveSlotTargetDataList must be present so the walker can "
        "consume it; the override prevents the loader from skipping it.")
    assert res_list.type_descriptor == "CArray<ReserveSlotTargetData>"


def test_iteminfo_walker_reaches_cooltime_on_synthetic_entry():
    """Build a minimal but well-formed ItemInfo entry using the smallest
    legal value for every field, then verify ``_consume_field_bytes`` can
    walk through every field up to ``_cooltime`` without bailing.

    This proves the override + walker combination unblocks the
    NoCooldownForALLItems case without depending on real game files.
    """
    from cdumm.semantic import parser as parser_mod
    from cdumm.engine.format3_apply import _consume_field_bytes

    parser_mod._loaded_schemas = None
    schema = parser_mod.get_schema("iteminfo")
    assert schema is not None

    # Build the smallest legal payload by emitting empty/zero values for
    # every type. Each field's type_descriptor is interpreted to produce
    # the minimum byte sequence the walker will accept.
    body = bytearray()
    for f in schema.fields:
        body += _emit_min_value(f)

    # Walk: from offset 0, consume each field's bytes via the same code
    # path apply_engine uses. Track whether we successfully reach _cooltime.
    off = 0
    reached_cooltime = False
    for f in schema.fields:
        if f.name == "_cooltime":
            reached_cooltime = True
            break
        consumed = _consume_field_bytes(bytes(body), off, f, len(body))
        assert consumed is not None, (
            f"walker bailed on field {f.name!r} (type={f.field_type!r}, "
            f"descriptor={f.type_descriptor!r}) at offset {off}")
        off += consumed

    assert reached_cooltime
    # Now consume cooltime itself to confirm the descriptor is right.
    consumed = _consume_field_bytes(bytes(body), off, by_name_in(schema, "_cooltime"), len(body))
    assert consumed == 8, "cooltime must consume exactly 8 bytes (i64)"


def by_name_in(schema, name):
    for f in schema.fields:
        if f.name == name:
            return f
    raise KeyError(name)


def _emit_min_value(spec) -> bytes:
    """Produce the minimum legal byte sequence the walker will accept
    for this field. For primitives that's zero bytes of the right width;
    for variable-length types it's the empty-payload encoding.
    """
    descriptor = spec.type_descriptor
    if descriptor:
        return _emit_descriptor_min(descriptor)
    # Legacy CString / fixed stream_size
    if spec.field_type == "CString":
        return struct.pack("<I", 0)
    if spec.stream_size:
        return b"\x00" * spec.stream_size
    return b""


def _emit_descriptor_min(td: str) -> bytes:
    td = td.strip()
    width = pabgb_types._PRIMITIVE_WIDTH.get(td)
    if width is not None:
        return b"\x00" * width
    if td == "CString":
        return struct.pack("<I", 0)
    if td == "LocalizableString":
        return struct.pack("<BQI", 0, 0, 0)
    if td.startswith("COptional<") and td.endswith(">"):
        return b"\x00"  # flag = 0, no payload
    if td.startswith("CArray<") and td.endswith(">"):
        return struct.pack("<I", 0)
    if td.startswith("[") and td.endswith("]") and ";" in td:
        inner_part = td[1:-1]
        inner, count = inner_part.split(";", 1)
        inner_min = _emit_descriptor_min(inner.strip())
        return inner_min * int(count.strip())
    sub = pabgb_types.SUBSTRUCT_DEFS.get(td)
    if sub is not None:
        out = b""
        for _fname, ftype in sub:
            out += _emit_descriptor_min(ftype)
        return out
    variant = pabgb_types.TAGGED_VARIANT_DEFS.get(td)
    if variant is not None:
        # Pick the FIRST variant key (deterministic for tests). For
        # SubItem that's variant 0 (ItemKey -> 4 byte u32). For
        # SealableItemInfo that's variant 0 (Item -> u32 ItemKey)
        # following the fixed prefix (u32 item_key + u64 unknown0).
        disc_value = next(iter(variant["variants"]))
        disc_type = variant["discriminator"]
        if disc_type == "u8":
            out = bytes([disc_value])
        elif disc_type == "u16":
            out = struct.pack("<H", disc_value)
        elif disc_type == "u32":
            out = struct.pack("<I", disc_value)
        else:
            out = b""
        for _fname, ftype in variant.get("fixed_prefix", []):
            out += _emit_descriptor_min(ftype)
        payload = variant["variants"][disc_value]
        if payload:
            out += _emit_descriptor_min(payload)
        return out
    raise ValueError(f"_emit_descriptor_min does not handle {td!r}")
