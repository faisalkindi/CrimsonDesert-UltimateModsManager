"""Phase 3f-cstring: asset_path writes when length is preserved.

mod 2276's 135 asset_path intents all set the new value to a string
whose UTF-8 byte length matches the existing value's, so a fixed-
width write of the body bytes (length prefix unchanged) is safe.

When the new value's byte length differs, we skip with a clear
warning rather than corrupt the entry , a length change requires
re-emitting the entry, which shifts all subsequent entries and
needs whole-table writer dispatch (deferred).
"""
from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

from cdumm.engine.format3_apply import expand_format3_into_aggregated


def _make_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER,"
        " json_source TEXT, priority INTEGER, mod_type TEXT)")
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)")
    for r in rows:
        conn.execute(
            "INSERT INTO mods (id, name, enabled, json_source, "
            "priority, mod_type) VALUES (?, ?, ?, ?, ?, 'paz')", r)
    conn.commit()
    return conn


class _DBWrap:
    def __init__(self, conn): self.connection = conn


def _build_entry_with_asset_path(asset_path: str) -> bytes:
    """Build a buffinfo entry with one item carrying a tag-3
    payload whose asset_path has the given value. All other fields
    use trivial defaults."""
    from tests.test_buffinfo_payload_common import _build_payload_bytes
    payload = _build_payload_bytes(tag=3, asset_path=asset_path)
    variant_tail = struct.pack("<IQ", 0, 0)
    name_b = b"X"
    return (
        struct.pack("<I", 1)              # key
        + struct.pack("<I", len(name_b)) + name_b
        + bytes([0])                      # is_blocked
        + struct.pack("<I", 1)            # buff_data_count
        + struct.pack("<I", 0xAA) + bytes([0])  # item header
        + payload + variant_tail
        + struct.pack("<I", 1) + struct.pack("<I", 5)  # min/max level
        + struct.pack("<I", 0) + bytes([0])  # seq
        + struct.pack("<I", 0) * 3 + bytes([0, 0])  # trailer
    )


def _pabgb(entry_bytes: bytes):
    header = struct.pack("<H", 1) + struct.pack("<II", 1, 0)
    return entry_bytes, header


def _write_mod(tmp_path: Path, intents):
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "format": 3, "target": "buffinfo.pabgb", "intents": intents,
    }), encoding="utf-8")
    return p


def test_asset_path_length_preserving_write_emits_change(tmp_path):
    body, header = _pabgb(_build_entry_with_asset_path("oldpathXXXX"))
    mod = _write_mod(tmp_path, [
        {"entry": "X", "key": 1,
         "field": "buff_data_list[0].data.base.asset_path",
         "op": "set", "new": "newpathYYYY"},  # same 11-byte length
    ])
    db = _DBWrap(_make_db([(1, "x", 1, str(mod), 100)]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    assert "buffinfo.pabgb" in aggregated, (
        "length-preserving asset_path write should produce a change")
    changes = aggregated["buffinfo.pabgb"]
    assert len(changes) == 1
    c = changes[0]
    # The patched bytes must equal the new asset_path UTF-8 encoding.
    assert bytes.fromhex(c["patched"]) == b"newpathYYYY"
    # Original must equal old asset_path bytes.
    assert bytes.fromhex(c["original"]) == b"oldpathXXXX"


def test_asset_path_length_changing_write_drops_silently(tmp_path):
    body, header = _pabgb(_build_entry_with_asset_path("oldpath"))  # 7 bytes
    mod = _write_mod(tmp_path, [
        {"entry": "X", "key": 1,
         "field": "buff_data_list[0].data.base.asset_path",
         "op": "set", "new": "much_longer_new_path"},  # 20 bytes
    ])
    db = _DBWrap(_make_db([(1, "x", 1, str(mod), 100)]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    # Length-changing write would shift entry size; skip cleanly.
    assert "buffinfo.pabgb" not in aggregated or \
        aggregated.get("buffinfo.pabgb") == []


def test_asset_path_empty_string_to_empty_string_write_emits_zero_byte_change(tmp_path):
    body, header = _pabgb(_build_entry_with_asset_path(""))
    mod = _write_mod(tmp_path, [
        {"entry": "X", "key": 1,
         "field": "buff_data_list[0].data.base.asset_path",
         "op": "set", "new": ""},  # length 0 == length 0
    ])
    db = _DBWrap(_make_db([(1, "x", 1, str(mod), 100)]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    # Zero-byte change (no patch needed); the helper may emit nothing
    # or emit an empty-bytes change , both are acceptable. Pin: no
    # crash, no spurious bytes.
    if "buffinfo.pabgb" in aggregated and aggregated["buffinfo.pabgb"]:
        c = aggregated["buffinfo.pabgb"][0]
        assert c["patched"] == ""  # no bytes to write
        assert c["original"] == ""
