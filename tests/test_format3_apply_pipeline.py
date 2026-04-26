"""Format 3 — apply pipeline integration (Phase 4 / Option B).

The new ``expand_format3_into_aggregated`` function bridges the gap
between Phase 1-3's stand-alone Format 3 byte writer and CDUMM's
existing v2 mount-time aggregator. It runs AFTER the v2 aggregator
in ``apply_engine.aggregate_json_mods_into_synthetic_patches``,
processes any enabled Format 3 mods, and APPENDS their resolved
intents as v2-style change dicts to the same ``aggregated`` dict.

Design invariants tested here:

  R1. Existing v2 logic is untouched — a Format 3 expansion with no
      Format 3 mods enabled must be a no-op on the input dict.

  R2. Format 3 changes are APPENDED, not replacing — v2 mods on the
      same file keep their changes alongside Format 3 changes.

  R3. Vanilla extraction failures are non-fatal — a missing or
      unreadable target file logs at warning, skips that mod's
      intents, and the rest of apply continues.

  R4. Format 3 mods that resolve to zero changes do not pollute the
      aggregated dict (no empty ``aggregated[game_file] = []``).

  R5. The expansion uses the same key_size-aware logic as the
      stand-alone writer (the H2 fix from this session) — refuses
      key_size != 2/4 instead of silently mis-aligning.
"""
from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import expand_format3_into_aggregated
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import FieldSpec, TableSchema


# ── Minimal in-memory mods table ────────────────────────────────────


def _make_db(rows: list[tuple]) -> sqlite3.Connection:
    """Build the minimal mods/mod_config schema the aggregator queries.

    ``rows`` is a list of (id, name, enabled, json_source, priority).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER,"
        " json_source TEXT, priority INTEGER, mod_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE mod_config ("
        "mod_id INTEGER, custom_values TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO mods (id, name, enabled, json_source, "
            "priority, mod_type) VALUES (?, ?, ?, ?, ?, 'paz')",
            r,
        )
    conn.commit()
    return conn


class _DBWrap:
    def __init__(self, conn):
        self.connection = conn


# ── Synthetic schema fixture ────────────────────────────────────────


@pytest.fixture
def synth_schema(monkeypatch):
    fields = [
        FieldSpec(name="_alpha", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_beta", stream_size=2,
                  field_type="direct_u16", struct_fmt="H"),
    ]
    schema = TableSchema(table_name="pipetest", fields=fields)
    parser_mod._load_schemas()
    cache = dict(parser_mod._loaded_schemas or {})
    cache["pipetest"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", cache)
    yield schema


def _build_pabgb(entry_id: int, name: str,
                 alpha: int, beta: int) -> tuple[bytes, bytes]:
    """One-entry PABGB body + matching PABGH header (key_size=4)."""
    name_b = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_b))
    payload = struct.pack("<IH", alpha, beta)
    body = head + name_b + b"\x00" + payload
    header = struct.pack("<H", 1) + struct.pack("<II", entry_id, 0)
    return body, header


def _write_format3(tmp_path: Path, target: str,
                   intents: list[dict]) -> Path:
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "format": 3,
        "target": target,
        "intents": intents,
    }), encoding="utf-8")
    return p


# ── R1: no-op when no Format 3 mods enabled ─────────────────────────


def test_no_format3_mods_leaves_aggregated_unchanged(synth_schema):
    """The whole-purpose regression guard: if zero Format 3 mods are
    enabled in the DB, aggregated and signatures must come back
    byte-identical to what they went in as. This pins R1 — v2-only
    flows are unaffected by the Format 3 expansion path."""
    db = _DBWrap(_make_db([]))
    aggregated = {"existing/v2.pabgb": [
        {"entry": "x", "rel_offset": 0, "original": "00",
         "patched": "01"}
    ]}
    signatures = {"existing/v2.pabgb": "deadbeef"}

    snapshot_aggregated = json.loads(json.dumps(aggregated))
    snapshot_signatures = dict(signatures)

    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: None,
    )

    assert aggregated == snapshot_aggregated
    assert signatures == snapshot_signatures


def test_format3_mod_with_disabled_state_is_ignored(synth_schema, tmp_path):
    """Only enabled=1 mods run through the expansion. Disabled rows
    must not contribute changes."""
    body, header = _build_pabgb(1, "X", 0x11111111, 0x55)
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xDEADBEEF}
    ])
    db = _DBWrap(_make_db([
        (1, "DisabledMod", 0, str(json_path), 1),
    ]))
    aggregated, signatures = {}, {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (body, header),
    )
    assert aggregated == {}


# ── R2: appended alongside v2 mods on same file ─────────────────────


def test_format3_appends_to_existing_v2_changes_for_same_file(
        synth_schema, tmp_path):
    """A v2 mod and a Format 3 mod both targeting pipetest.pabgb:
    the format3 expansion APPENDS its change to the same list. The
    v2 mod's existing change is preserved unchanged."""
    body, header = _build_pabgb(1, "X", 0x11111111, 0x55)
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xCAFEBABE}
    ])
    db = _DBWrap(_make_db([
        (1, "Format3Mod", 1, str(json_path), 5),
    ]))

    # Pre-existing v2 change on the same file
    v2_change = {"entry": "X", "rel_offset": 999,
                 "original": "00", "patched": "01"}
    aggregated = {"pipetest.pabgb": [v2_change]}
    signatures = {}

    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (body, header),
    )

    # v2 change still there, format3 change appended
    changes = aggregated["pipetest.pabgb"]
    assert v2_change in changes
    assert len(changes) >= 2
    # Format 3 change has the right new bytes (u32 0xCAFEBABE LE)
    f3_change = next(c for c in changes if c is not v2_change)
    assert f3_change.get("patched", "").lower() == "bebafeca"


# ── R3: vanilla extraction failure is non-fatal ─────────────────────


def test_vanilla_extraction_failure_skips_mod_silently(
        synth_schema, tmp_path):
    """When vanilla_extractor returns None for the target file, the
    Format 3 mod must skip without raising. Other mods' contributions
    must still be intact."""
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xDEADBEEF}
    ])
    db = _DBWrap(_make_db([
        (1, "Format3Mod", 1, str(json_path), 1),
    ]))
    aggregated = {"other/file.pabgb": [{"keep": "me"}]}
    signatures = {}

    # No exception raised
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: None,
    )

    # Other file's changes intact
    assert aggregated["other/file.pabgb"] == [{"keep": "me"}]
    # No phantom entry for the failed target
    assert "pipetest.pabgb" not in aggregated


def test_malformed_format3_json_skips_mod_without_raising(
        synth_schema, tmp_path):
    """A mod whose json_source points at malformed JSON must skip,
    not crash the apply phase. Mirrors the v2 aggregator's behaviour
    on parse failure."""
    p = tmp_path / "bad.json"
    p.write_text("this isn't valid json {[", encoding="utf-8")
    db = _DBWrap(_make_db([
        (1, "BadMod", 1, str(p), 1),
    ]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        vanilla_extractor=lambda gf: (b"", b""),
    )
    assert aggregated == {}


# ── R4: zero-change mods don't pollute aggregated ───────────────────


def test_format3_mod_with_zero_resolved_intents_no_dict_entry(
        synth_schema, tmp_path):
    """All intents skipped (e.g., field_schema empty + no PABGB
    schema match): the function must NOT create an empty
    ``aggregated[target] = []``. Empty lists confuse downstream
    code that expects 'present means non-empty'."""
    body, header = _build_pabgb(1, "X", 0x11111111, 0x55)
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "totallyMadeUp",
         "op": "set", "new": 42}
    ])
    db = _DBWrap(_make_db([
        (1, "EmptyResolveMod", 1, str(json_path), 1),
    ]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        vanilla_extractor=lambda gf: (body, header),
    )
    assert "pipetest.pabgb" not in aggregated


# ── R5: key_size guard ──────────────────────────────────────────────


def test_unsupported_key_size_skips_format3_mod(
        synth_schema, tmp_path):
    """If the PABGH index has an unsupported key_size (e.g. 8), the
    expansion must skip rather than misalign — same defensive
    posture as apply_intents_to_pabgb_bytes (H2 fix). Other mods
    must continue to be processed."""
    name = b"X"
    head = struct.pack("<II", 1, len(name))
    payload = struct.pack("<IH", 0xCAFE, 0x55)
    body = head + name + b"\x00" + payload
    # Hand-build a header with key_size=8 (one u64 key + u32 offset)
    bad_header = struct.pack("<H", 1) + struct.pack("<Q", 1) + struct.pack("<I", 0)

    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xDEADBEEF}
    ])
    db = _DBWrap(_make_db([
        (1, "BadKeySize", 1, str(json_path), 1),
    ]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        vanilla_extractor=lambda gf: (body, bad_header),
    )
    assert "pipetest.pabgb" not in aggregated


# ── Smoke: end-to-end through expansion ─────────────────────────────


# ── User-facing warnings on zero-change mods ────────────────────────


def test_zero_supported_intents_appends_user_warning(
        synth_schema, tmp_path):
    """A Format 3 mod whose validation returns zero supported intents
    must produce a user-visible warning, not just a debug log line.
    Without this, the user sees green Apply, no in-game effect, no
    indication anything went wrong with their mod."""
    body, header = _build_pabgb(1, "X", 0x11111111, 0x55)
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "totallyMadeUp",
         "op": "set", "new": 42}
    ])
    db = _DBWrap(_make_db([
        (1, "GhostMod", 1, str(json_path), 1),
    ]))
    warnings_out: list[str] = []
    expand_format3_into_aggregated(
        {}, {}, db,
        vanilla_extractor=lambda gf: (body, header),
        warnings_out=warnings_out,
    )
    assert warnings_out, (
        "expected a user-visible warning for the zero-change mod")
    msg = warnings_out[0]
    assert "GhostMod" in msg
    assert "0" in msg or "zero" in msg.lower() or "no" in msg.lower()


def test_vanilla_extraction_failure_appends_user_warning(
        synth_schema, tmp_path):
    """When vanilla_extractor returns None, the user must see a
    warning naming the mod and the missing target. Otherwise the
    apply UI shows success and the user wonders why their mod
    didn't change anything."""
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xDEADBEEF}
    ])
    db = _DBWrap(_make_db([
        (1, "MissingTargetMod", 1, str(json_path), 1),
    ]))
    warnings_out: list[str] = []
    expand_format3_into_aggregated(
        {}, {}, db,
        vanilla_extractor=lambda gf: None,
        warnings_out=warnings_out,
    )
    assert warnings_out
    msg = warnings_out[0]
    assert "MissingTargetMod" in msg
    assert "pipetest.pabgb" in msg


def test_warnings_out_optional_not_required(synth_schema, tmp_path):
    """Backward compat: callers that don't pass warnings_out get the
    same behaviour as before (no warnings collected, function still
    runs to completion)."""
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "totallyMadeUp",
         "op": "set", "new": 42}
    ])
    db = _DBWrap(_make_db([
        (1, "X", 1, str(json_path), 1),
    ]))
    # No warnings_out — must not raise
    expand_format3_into_aggregated(
        {}, {}, db,
        vanilla_extractor=lambda gf: (b"", b""),
    )


def test_apply_engine_emits_warning_for_format3_zero_changes():
    """Source-level guard: apply_engine must collect warnings from
    expand_format3_into_aggregated and route them to the existing
    warning signal so on_apply_done renders them in the InfoBar.
    Same surfacing mechanism v3.2.1's skipped-patches feature uses."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "engine" / "apply_engine.py"
           ).read_text(encoding="utf-8")
    # The wire-up must collect warnings and emit them
    assert "f3_warnings" in src, (
        "apply_engine must collect Format 3 zero-change warnings "
        "into a list named f3_warnings")
    # And the warning emit must be tied to that list (proximity check)
    f3_idx = src.find("f3_warnings")
    assert f3_idx != -1
    # Within 1000 chars of the f3_warnings collection, the warning
    # signal must fire — that's the route to the InfoBar.
    block = src[f3_idx:f3_idx + 1500]
    assert "self.warning.emit" in block or "warning.emit" in block, (
        "Format 3 warnings must go through the warning signal so "
        "on_apply_done renders them in the InfoBar")
    # The warning must mention 'Format 3' and 'byte' so users
    # understand what failed.
    assert "Format 3" in block
    assert "byte" in block.lower() or "change" in block.lower()


def test_supported_intent_produces_v2_compatible_change_dict(
        synth_schema, tmp_path):
    """The change dict added to aggregated must match the v2
    schema downstream consumers expect: keys ``entry``, ``rel_offset``,
    ``original``, ``patched``. Pin those keys so the wire-up to
    process_json_patches_for_overlay doesn't break later."""
    body, header = _build_pabgb(1, "X", 0x11111111, 0x55)
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xDEADBEEF}
    ])
    db = _DBWrap(_make_db([
        (1, "Format3Mod", 1, str(json_path), 1),
    ]))
    aggregated = {}
    expand_format3_into_aggregated(
        aggregated, {}, db,
        vanilla_extractor=lambda gf: (body, header),
    )
    changes = aggregated.get("pipetest.pabgb")
    assert changes and len(changes) == 1
    c = changes[0]
    # v2 change shape
    assert "entry" in c or "rel_offset" in c
    # Original and patched are hex strings the apply layer expects
    assert isinstance(c.get("original"), str)
    assert isinstance(c.get("patched"), str)
    # Patched value is the LE-packed new value
    assert c["patched"].lower() == "efbeadde"
