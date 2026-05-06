"""Phase 3f follow-up: buffinfo intents using camelCase + leading-
underscore field names (CDUMM PABGB schema convention) must apply
the same as snake_case names (field-names dialect convention).

The format3 validator already accepts BOTH shapes via a 4-shape
candidate chain (intent.field, _intent.field, snake_to_camel,
_snake_to_camel) in _resolve_write_pos. The Phase 3f buffinfo
helper must mirror that or intents validate-then-fail-to-apply,
which presents to users as "imported, enabled, applies, no
effect" , the worst possible failure mode.

Real-world impact: any user who hand-authored a buffinfo Format 3
mod using the CDUMM schema names they see in the conflict viewer
(`_minLevel`, `_isBlocked`, etc.) hits this. The field-names
dialect uses snake_case so it works either way.
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


def _build_entry(min_level=1) -> bytes:
    """Minimal buffinfo entry with key=42, name='X', no items."""
    out = bytearray()
    out += struct.pack("<I", 42)         # key
    out += struct.pack("<I", 1) + b"X"   # name
    out += bytes([0])                     # is_blocked
    out += struct.pack("<I", 0)           # buff_data_count
    out += struct.pack("<I", min_level)   # min_level
    out += struct.pack("<I", 99)          # max_level
    out += struct.pack("<I", 0)           # seq_len
    out += bytes([0])                     # blct
    out += struct.pack("<III", 0, 0, 0)   # ui_template, ui_comp, elem
    out += bytes([0, 0])                  # iuspd, ucbgt
    return bytes(out)


def _build_pabgb():
    body = _build_entry()
    header = struct.pack("<H", 1) + struct.pack("<II", 42, 0)
    return body, header


def _write_mod(tmp_path: Path, field: str, new_val: int) -> Path:
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "format": 3, "target": "buffinfo.pabgb",
        "intents": [{"entry": "X", "key": 42,
                     "field": field, "op": "set", "new": new_val}]
    }), encoding="utf-8")
    return p


def _expand(tmp_path: Path, field: str):
    body, header = _build_pabgb()
    mod = _write_mod(tmp_path, field, 5)
    db = _DBWrap(_make_db([(1, "x", 1, str(mod), 100)]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        lambda t: (body, header) if t == "buffinfo.pabgb" else None)
    return aggregated


def test_snake_case_field_name_applies(tmp_path):
    agg = _expand(tmp_path, "min_level")
    assert "buffinfo.pabgb" in agg, "snake_case must apply"
    assert agg["buffinfo.pabgb"][0]["patched"] == \
        struct.pack("<I", 5).hex()


def test_camel_case_with_underscore_prefix_applies(tmp_path):
    """CDUMM PABGB schema convention , what users see in the
    conflict viewer."""
    agg = _expand(tmp_path, "_minLevel")
    assert "buffinfo.pabgb" in agg, (
        "_minLevel (CDUMM schema name) must apply same as min_level")
    assert agg["buffinfo.pabgb"][0]["patched"] == \
        struct.pack("<I", 5).hex()


def test_underscore_prefixed_snake_case_applies(tmp_path):
    """``_min_level`` , uncommon but accepted by the validator's
    4-shape lookup, so we honor it too."""
    agg = _expand(tmp_path, "_min_level")
    assert "buffinfo.pabgb" in agg
    assert agg["buffinfo.pabgb"][0]["patched"] == \
        struct.pack("<I", 5).hex()


def test_camelcase_no_prefix_applies(tmp_path):
    """``minLevel`` , no underscore prefix."""
    agg = _expand(tmp_path, "minLevel")
    assert "buffinfo.pabgb" in agg
    assert agg["buffinfo.pabgb"][0]["patched"] == \
        struct.pack("<I", 5).hex()
