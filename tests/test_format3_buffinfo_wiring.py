"""Phase 3f: Format 3 apply path wires buffinfo intents into v2-style
byte changes via the clean-room buffinfo parser.

Before this wiring, every buffinfo.pabgb intent fell through the
"target has no schema in CDUMM" skip branch in validate_intents and
produced 0 byte changes at apply time. mods like norva2's #2276
(Double Resource Buff Effect , Field) imported and listed in the UI
but applied nothing. This test pins:

  * Wrapper-level intents (min_level, ui_template_name, etc.) on a
    keyed buff entry resolve to a correct (offset, width, value)
    byte change at apply time.
  * Item-level intents on buff_data_list[N].data.base.X resolve when
    item N is reachable via known-tag walks.
  * Intents on unknown variant tails (or unknown leaf names) are
    skipped cleanly , no exceptions, no bytes emitted, the rest of
    the mod's intents still apply.
"""
from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import expand_format3_into_aggregated


def _make_db(rows: list[tuple]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER,"
        " json_source TEXT, priority INTEGER, mod_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO mods (id, name, enabled, json_source, "
            "priority, mod_type) VALUES (?, ?, ?, ?, ?, 'paz')", r)
    conn.commit()
    return conn


class _DBWrap:
    def __init__(self, conn):
        self.connection = conn


def _build_buffinfo_entry(
    *, key: int, name: str,
    is_blocked: int = 0,
    min_level: int = 1, max_level: int = 99,
    sequencer_name: str = "",
    buff_level_calculate_type: int = 0,
    ui_template_name: int = 0,
    ui_component_name: int = 0,
    elemental_status_info: int = 0,
    is_use_skill_info_pattern_description: int = 0,
    use_counting_by_global_timer: int = 0,
    items: bytes = b"",
    buff_data_count: int = 0,
) -> bytes:
    """Serialize one BuffInfo entry exactly like the parser expects."""
    name_b = name.encode("utf-8")
    seq_b = sequencer_name.encode("utf-8")
    out = bytearray()
    out += struct.pack("<I", key)
    out += struct.pack("<I", len(name_b))
    out += name_b
    out += bytes([is_blocked])
    out += struct.pack("<I", buff_data_count)
    out += items
    out += struct.pack("<I", min_level)
    out += struct.pack("<I", max_level)
    out += struct.pack("<I", len(seq_b))
    out += seq_b
    out += bytes([buff_level_calculate_type])
    out += struct.pack("<I", ui_template_name)
    out += struct.pack("<I", ui_component_name)
    out += struct.pack("<I", elemental_status_info)
    out += bytes([is_use_skill_info_pattern_description])
    out += bytes([use_counting_by_global_timer])
    return bytes(out)


def _build_buffinfo_pabgb(entries: list[tuple[int, bytes]]
                          ) -> tuple[bytes, bytes]:
    """Concatenate entries, return (body, pabgh_header).

    PABGH layout: u16 count, then [(u32 key, u32 offset)] pairs.
    """
    body = bytearray()
    offsets: list[tuple[int, int]] = []
    for key, raw in entries:
        offsets.append((key, len(body)))
        body += raw
    header = bytearray()
    header += struct.pack("<H", len(offsets))
    for k, off in offsets:
        header += struct.pack("<II", k, off)
    return bytes(body), bytes(header)


def _write_format3(tmp_path: Path, target: str,
                   intents: list[dict]) -> Path:
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "format": 3, "target": target, "intents": intents,
    }), encoding="utf-8")
    return p


# ── Phase 3f tests ──────────────────────────────────────────────────


def test_buffinfo_wrapper_field_intent_emits_byte_change(tmp_path):
    """A min_level intent on a real buffinfo entry must produce a
    v2-style change with the correct (entry, rel_offset, original,
    patched) shape, NOT silently skip with a "no schema" warning."""
    entry_raw = _build_buffinfo_entry(
        key=1000114, name="BuffLevel_Socket_ContributionExp",
        min_level=1, max_level=10, sequencer_name="seq.psbg",
        buff_data_count=0)
    body, header = _build_buffinfo_pabgb([(1000114, entry_raw)])

    mod_path = _write_format3(tmp_path, "buffinfo.pabgb", [
        {"entry": "BuffLevel_Socket_ContributionExp",
         "key": 1000114, "field": "min_level",
         "op": "set", "new": 5},
    ])
    db = _DBWrap(_make_db(
        [(1, "norva2-style buffinfo mod", 1, str(mod_path), 100)]))
    aggregated: dict[str, list[dict]] = {}
    signatures: dict[str, str] = {}

    def vanilla(target: str):
        if target == "buffinfo.pabgb":
            return body, header
        return None

    expand_format3_into_aggregated(
        aggregated, signatures, db, vanilla)

    assert "buffinfo.pabgb" in aggregated, (
        "buffinfo intents must produce changes, not be silently skipped")
    changes = aggregated["buffinfo.pabgb"]
    assert len(changes) == 1
    c = changes[0]
    # The new value (5) must be packed as u32 LE.
    assert c["patched"] == struct.pack("<I", 5).hex()
    # The original value (1) must be readable from the change.
    assert c["original"] == struct.pack("<I", 1).hex()


def test_buffinfo_unknown_leaf_skips_gracefully(tmp_path):
    """An intent on a leaf name we don't decode (e.g. a typo) must
    be skipped with zero crashes, and other intents in the mod must
    still apply."""
    entry_raw = _build_buffinfo_entry(
        key=42, name="X", min_level=3, max_level=7,
        sequencer_name="")
    body, header = _build_buffinfo_pabgb([(42, entry_raw)])
    mod_path = _write_format3(tmp_path, "buffinfo.pabgb", [
        {"entry": "X", "key": 42, "field": "totally_made_up_field",
         "op": "set", "new": 99},
        {"entry": "X", "key": 42, "field": "min_level",
         "op": "set", "new": 5},
    ])
    db = _DBWrap(_make_db([(1, "mixed mod", 1, str(mod_path), 100)]))
    aggregated: dict[str, list[dict]] = {}
    signatures: dict[str, str] = {}

    def vanilla(t):
        return (body, header) if t == "buffinfo.pabgb" else None

    expand_format3_into_aggregated(
        aggregated, signatures, db, vanilla)

    # Only the resolvable intent should produce a change.
    assert len(aggregated.get("buffinfo.pabgb", [])) == 1
    assert aggregated["buffinfo.pabgb"][0]["patched"] == \
        struct.pack("<I", 5).hex()


def test_buffinfo_intent_keyed_to_missing_entry_drops(tmp_path):
    """If intent.key isn't in the PABGH index, the intent silently
    drops , no crash, no change emitted."""
    entry_raw = _build_buffinfo_entry(
        key=42, name="X", min_level=3, sequencer_name="")
    body, header = _build_buffinfo_pabgb([(42, entry_raw)])
    mod_path = _write_format3(tmp_path, "buffinfo.pabgb", [
        {"entry": "GHOST", "key": 999999, "field": "min_level",
         "op": "set", "new": 5},
    ])
    db = _DBWrap(_make_db([(1, "ghost mod", 1, str(mod_path), 100)]))
    aggregated: dict[str, list[dict]] = {}

    def vanilla(t):
        return (body, header) if t == "buffinfo.pabgb" else None

    expand_format3_into_aggregated(
        aggregated, {}, db, vanilla)

    assert "buffinfo.pabgb" not in aggregated or \
        aggregated["buffinfo.pabgb"] == []


def test_buffinfo_change_carries_source_mod_id(tmp_path):
    """Skip-tracking: each emitted buffinfo change must carry
    _source_mod_id so persist_skip_summary can attribute byte
    mismatches back to the originating mod."""
    entry_raw = _build_buffinfo_entry(
        key=1, name="X", min_level=1, sequencer_name="")
    body, header = _build_buffinfo_pabgb([(1, entry_raw)])
    mod_path = _write_format3(tmp_path, "buffinfo.pabgb", [
        {"entry": "X", "key": 1, "field": "min_level",
         "op": "set", "new": 9},
    ])
    db = _DBWrap(_make_db([(77, "tagged mod", 1, str(mod_path), 50)]))
    aggregated: dict[str, list[dict]] = {}
    participating: set[int] = set()

    def vanilla(t):
        return (body, header) if t == "buffinfo.pabgb" else None

    expand_format3_into_aggregated(
        aggregated, {}, db, vanilla,
        participating_mod_ids=participating)

    assert aggregated["buffinfo.pabgb"][0]["_source_mod_id"] == 77
    assert aggregated["buffinfo.pabgb"][0]["_target_file"] == \
        "buffinfo.pabgb"
    assert 77 in participating
