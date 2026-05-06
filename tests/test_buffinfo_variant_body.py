"""Phase 3e: variant body decoder for ``data.variant.body.fXX``.

Each variant tag has its own struct laid out after the common payload.
For tags whose body is purely fixed-width primitives (no
``BuffDataValueBlock`` or other variable-length sub-records), the
field offsets are deterministic and locate_buff_field can resolve
``buff_data_list[N].data.variant.body.f00/f01/...`` to a byte
patch target.

Variant TYPE writes are a different beast , changing the type
requires re-encoding the whole tail. This test covers the no-op
case (mod sets type to the SAME name as the entry currently has,
which is what norva2 mod 2276 does for all 135 variant.type
intents): we resolve to the tag byte's offset (1B u8) and the
apply path emits a confirmation write of the same byte.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


_VANILLA = Path(r"C:/temp/buffinfo.pabgb")
_VANILLA_PABGH = Path(r"C:/temp/buffinfo.pabgh")


def _build_payload_bytes(*, tag: int, **kw) -> bytes:
    """Reuse builder from test_buffinfo_payload_common."""
    from tests.test_buffinfo_payload_common import _build_payload_bytes
    return _build_payload_bytes(tag=tag, **kw)


def test_variant_body_f00_resolves_for_known_tag():
    """A buff_data_list[0].data.variant.body.f00 path on a tag-3
    item must resolve to the byte at offset 0 of the variant tail
    (immediately after the common payload), with width matching the
    f00 field in VaryStaticStatBuffData (u32)."""
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    payload_tag3 = _build_payload_bytes(tag=3)
    # Variant tail for tag 3: f00:u32, f01:u64 (12B total)
    variant_tail = struct.pack("<IQ", 0xCAFEBABE, 0xDEADBEEFCAFEBABE)
    name = b"X"
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", len(name)) + name
        + bytes([0])
        + struct.pack("<I", 1)  # 1 item
        + struct.pack("<I", 0xAA) + bytes([0x00])  # item header
        + payload_tag3
        + variant_tail
        # wrapper trailer
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    res = locate_buff_field(
        raw, "buff_data_list[0].data.variant.body.f00")
    assert res is not None, (
        "variant.body.f00 should resolve for known fixed-width tag")
    off, width, dtype = res
    assert width == 4 and dtype == "u32"
    assert struct.unpack_from("<I", raw, off)[0] == 0xCAFEBABE


def test_variant_body_f01_resolves_for_known_tag():
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    payload_tag104 = _build_payload_bytes(tag=104)
    # Variant tail for tag 104: f00:u8, f01:u64 (9B total)
    variant_tail = bytes([0x42]) + struct.pack("<Q", 0x1234567890ABCDEF)
    name = b"X"
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", 1) + name
        + bytes([0])
        + struct.pack("<I", 1)
        + struct.pack("<I", 0xAA) + bytes([0x00])
        + payload_tag104
        + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    res = locate_buff_field(
        raw, "buff_data_list[0].data.variant.body.f01")
    assert res is not None
    off, width, dtype = res
    assert width == 8 and dtype == "u64"
    assert struct.unpack_from("<Q", raw, off)[0] == 0x1234567890ABCDEF


def test_variant_body_unknown_tag_returns_none():
    """A variant tail tag we don't have a body decoder for must
    return None , safer than guessing."""
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    payload_tag200 = _build_payload_bytes(tag=200)
    name = b"X"
    raw = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0xAA) + bytes([0x00])
        + payload_tag200
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    assert locate_buff_field(
        raw, "buff_data_list[0].data.variant.body.f00") is None


def test_variant_type_resolves_to_tag_byte():
    """The ``data.variant.type`` path on an item maps to the tag
    byte (offset 0 of common payload, 1 byte u8). This lets the
    apply path detect no-op confirmations (mod sets type to the
    name that already maps to the entry's current tag)."""
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    payload_tag3 = _build_payload_bytes(tag=3)
    variant_tail = struct.pack("<IQ", 1, 1)
    name = b"X"
    raw = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0xAA) + bytes([0x00])
        + payload_tag3 + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    res = locate_buff_field(
        raw, "buff_data_list[0].data.variant.type")
    assert res is not None
    off, width, dtype = res
    assert width == 1 and dtype == "u8"
    assert raw[off] == 3  # the tag byte itself


def test_variant_type_noop_confirmation_emits_change(tmp_path):
    """End-to-end: an intent setting variant.type to the SAME name
    that the entry's current tag maps to is a no-op confirmation.
    The apply path should emit a change (writing the same byte) so
    the rest of the mod's intents on this item still apply."""
    import json
    import sqlite3
    from pathlib import Path
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from tests.test_buffinfo_payload_common import _build_payload_bytes

    payload_tag104 = _build_payload_bytes(tag=104)
    variant_tail = bytes([0]) + struct.pack("<Q", 0)
    name = b"X"
    body = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0) + bytes([0])
        + payload_tag104 + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    header = struct.pack("<H", 1) + struct.pack("<II", 1, 0)
    mod = tmp_path / "mod.json"
    mod.write_text(json.dumps({
        "format": 3, "target": "buffinfo.pabgb",
        "intents": [
            {"entry": "X", "key": 1,
             "field": "buff_data_list[0].data.variant.type",
             "op": "set",
             "new": "AddPercentInGameContentsBuffData"},
        ]}), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
        "enabled INTEGER, json_source TEXT, priority INTEGER, "
        "mod_type TEXT)")
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)")
    conn.execute(
        "INSERT INTO mods VALUES (1, 'x', 1, ?, 100, 'paz')",
        (str(mod),))

    class W:
        connection = conn

    agg = {}
    expand_format3_into_aggregated(
        agg, {}, W(),
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" in agg
    c = agg["buffinfo.pabgb"][0]
    assert c["original"] == c["patched"], (
        "no-op confirmation must write the same byte")
    assert bytes.fromhex(c["original"]) == bytes([104])


def test_variant_type_change_to_different_tag_drops(tmp_path):
    """If the new variant.type name maps to a DIFFERENT tag than
    the entry currently has, the intent must be skipped , changing
    the type means a different tail layout."""
    import json
    import sqlite3
    from pathlib import Path
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from tests.test_buffinfo_payload_common import _build_payload_bytes

    payload_tag104 = _build_payload_bytes(tag=104)
    variant_tail = bytes([0]) + struct.pack("<Q", 0)
    name = b"X"
    body = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0) + bytes([0])
        + payload_tag104 + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    header = struct.pack("<H", 1) + struct.pack("<II", 1, 0)
    mod = tmp_path / "mod.json"
    mod.write_text(json.dumps({
        "format": 3, "target": "buffinfo.pabgb",
        "intents": [
            {"entry": "X", "key": 1,
             "field": "buff_data_list[0].data.variant.type",
             "op": "set",
             "new": "VaryStaticStatBuffData"},  # tag 3, not 104
        ]}), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
        "enabled INTEGER, json_source TEXT, priority INTEGER, "
        "mod_type TEXT)")
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)")
    conn.execute(
        "INSERT INTO mods VALUES (1, 'x', 1, ?, 100, 'paz')",
        (str(mod),))

    class W:
        connection = conn

    agg = {}
    expand_format3_into_aggregated(
        agg, {}, W(),
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" not in agg or agg["buffinfo.pabgb"] == []


def test_variant_type_int_write_must_match_current_tag(tmp_path):
    """A bare int intent on variant.type must validate against the
    current tag. If the int differs, skip , otherwise the layout
    would corrupt (different tail size after type change)."""
    import json
    import sqlite3
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from tests.test_buffinfo_payload_common import _build_payload_bytes

    payload_tag104 = _build_payload_bytes(tag=104)
    variant_tail = bytes([0]) + struct.pack("<Q", 0)
    name = b"X"
    body = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0) + bytes([0])
        + payload_tag104 + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    header = struct.pack("<H", 1) + struct.pack("<II", 1, 0)

    def make_db(new):
        mod = tmp_path / f"mod_{new}.json"
        mod.write_text(json.dumps({
            "format": 3, "target": "buffinfo.pabgb",
            "intents": [{"entry": "X", "key": 1,
                         "field": "buff_data_list[0].data.variant.type",
                         "op": "set", "new": new}]
        }), encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
            "enabled INTEGER, json_source TEXT, priority INTEGER, "
            "mod_type TEXT)")
        conn.execute(
            "CREATE TABLE mod_config (mod_id INTEGER, "
            "custom_values TEXT)")
        conn.execute(
            "INSERT INTO mods VALUES (1, 'x', 1, ?, 100, 'paz')",
            (str(mod),))

        class W:
            connection = conn
        return W()

    # Matching int: emit confirmation
    agg = {}
    expand_format3_into_aggregated(
        agg, {}, make_db(104),
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" in agg
    assert bytes.fromhex(agg["buffinfo.pabgb"][0]["patched"]) == bytes([104])

    # Different int: skip (layout mismatch would corrupt)
    agg = {}
    expand_format3_into_aggregated(
        agg, {}, make_db(3),  # tag 3 != current 104
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" not in agg or agg["buffinfo.pabgb"] == [], (
        "int type-change must be skipped, not silently corrupt the "
        "entry")


def test_data_base_tag_write_must_match_current(tmp_path):
    """``data.base.tag`` reaches the SAME byte as ``data.variant.type``.
    A mismatched int write would change the tag without re-encoding
    the variant tail , silent corruption. Must skip cleanly when
    new tag != current tag."""
    import json
    import sqlite3
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from tests.test_buffinfo_payload_common import _build_payload_bytes

    payload_tag104 = _build_payload_bytes(tag=104)
    variant_tail = bytes([0]) + struct.pack("<Q", 0)
    name = b"X"
    body = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0) + bytes([0])
        + payload_tag104 + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    header = struct.pack("<H", 1) + struct.pack("<II", 1, 0)

    def make_db(new):
        mod = tmp_path / f"basetag_{new}.json"
        mod.write_text(json.dumps({
            "format": 3, "target": "buffinfo.pabgb",
            "intents": [{"entry": "X", "key": 1,
                         "field": "buff_data_list[0].data.base.tag",
                         "op": "set", "new": new}]
        }), encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
            "enabled INTEGER, json_source TEXT, priority INTEGER, "
            "mod_type TEXT)")
        conn.execute(
            "CREATE TABLE mod_config (mod_id INTEGER, "
            "custom_values TEXT)")
        conn.execute(
            "INSERT INTO mods VALUES (1, 'x', 1, ?, 100, 'paz')",
            (str(mod),))

        class W:
            connection = conn
        return W()

    # Matching tag write: emit confirmation
    agg = {}
    expand_format3_into_aggregated(
        agg, {}, make_db(104),
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" in agg
    assert bytes.fromhex(agg["buffinfo.pabgb"][0]["patched"]) == bytes([104])

    # Mismatched tag: skip to prevent corruption
    agg = {}
    expand_format3_into_aggregated(
        agg, {}, make_db(3),  # tag 3 != current 104
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" not in agg or agg["buffinfo.pabgb"] == [], (
        "data.base.tag write that changes the tag must be skipped")


def test_real_vanilla_resolves_variant_body_paths():
    """Walk every vanilla entry, for each item with a known-tag
    variant body, resolve f00 and verify the offset is in-range."""
    from cdumm._vendor.buffinfo_parser import (
        locate_buff_field, parse_entry, _VARIANT_TAIL_SIZES,
    )

    if not (_VANILLA.exists() and _VANILLA_PABGH.exists()):
        pytest.skip("local vanilla buffinfo files not present")

    pabgb = _VANILLA.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    n = struct.unpack_from("<H", pabgh, 0)[0]
    offsets = []
    pos = 2
    for _ in range(n):
        k = struct.unpack_from("<I", pabgh, pos)[0]
        off = struct.unpack_from("<I", pabgh, pos + 4)[0]
        offsets.append((k, off))
        pos += 8
    offsets.sort(key=lambda x: x[1])

    n_resolved = 0
    for i, (_k, off) in enumerate(offsets):
        end = (offsets[i + 1][1]
               if i + 1 < len(offsets) else len(pabgb))
        raw = pabgb[off:end]
        try:
            entry = parse_entry(raw)
        except Exception:
            continue
        # Try item 0 only (always reachable)
        if entry.buff_data_count == 0:
            continue
        res = locate_buff_field(
            raw, "buff_data_list[0].data.variant.body.f00")
        if res is not None:
            off2, _, _ = res
            assert 0 <= off2 < len(raw)
            n_resolved += 1
    assert n_resolved > 100, (
        f"expected many vanilla entries to resolve f00, got "
        f"{n_resolved}")
