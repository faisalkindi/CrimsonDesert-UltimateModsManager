"""Format 3 raw-record replacement intents on skill.pabgb (CrimsonWings).

GitHub issue #77 (deadriver35, 2026-05-10): CrimsonWings Stamina Manager
ships ``_buff_data_raw`` intents on ``skill.pabgb``. v3.2.10's changelog
claims Format 3 primitive mods on skill.pabgb apply correctly via
voiddoiv's contribution. The user reports all 365 intents are still
skipped with::

    365x target 'skill.pabgb' has no schema in CDUMM
    (table 'skill' not in pabgb_complete_schema.json)

Phase 1 systematic-debugging findings:

  * CrimsonWings ships intents with ``field = "_buff_data_raw"``, target
    ``skill.pabgb``, format 3, and BOTH ``old`` + ``new`` hex strings
    set. Each pair has equal byte length (380 / 2868 / 2868 / 1296 /
    9146 chars in the first 5 intents).
  * ``field_schema/skill.json`` ships exactly one primitive entry,
    ``mission_eff_farming_i`` (voiddoiv's mission_eff contribution).
    ``_buff_data_raw`` is NOT in the field_schema and is NOT in the
    PABGB schema (skill is not in pabgb_complete_schema.json).
  * The intended resolution path is the raw-record byte-replacement
    routing in :mod:`cdumm.engine.format3_handler` and
    :mod:`cdumm.engine.format3_apply`: when an intent supplies both
    ``old`` and ``new`` as equal-length hex strings, the apply step
    searches the entry payload for ``old`` and replaces it with ``new``,
    independent of any schema entry.

These tests pin that path against an explicit ``skill.pabgb`` target so
a future regression that breaks raw-record routing fails loudly here
instead of silently skipping every CrimsonWings intent.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import _intents_to_v2_changes
from cdumm.engine.format3_handler import (
    Format3Intent,
    parse_format3_mod,
    validate_intents,
)


# ── Synthetic skill.pabgb body helpers ──────────────────────────────


def _skill_entry(entry_id: int, name: str, payload: bytes) -> bytes:
    """Build one skill.pabgb entry: u32 entry_id + u32 name_len + name +
    null terminator + payload."""
    name_b = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_b))
    return head + name_b + b"\x00" + payload


def _skill_pabgb(entries: list[tuple[int, bytes]]) -> tuple[bytes, bytes]:
    """Build (body, header) for a synthetic skill.pabgb with the given
    (key, entry_bytes) pairs. skill is u16-count + u32-key + u32-offset
    per parser.py's UINT_COUNT_TABLES + arithmetic key sizing."""
    body = bytearray()
    pairs: list[tuple[int, int]] = []
    for k, ent in entries:
        pairs.append((k, len(body)))
        body.extend(ent)
    header = bytearray(struct.pack("<H", len(entries)))
    for k, off in pairs:
        header.extend(struct.pack("<II", k, off))
    return bytes(body), bytes(header)


# ── CrimsonWings exact-shape intents ────────────────────────────────


CRIMSONWINGS_FIRST_INTENT = {
    "entry": "Attack_Big_Stamina_Decrease",
    "key": 77,
    "field": "_buff_data_raw",
    "old": (
        "73e1c5ea73e1c5ea000000000000000000000000000000000000000000000000"
        "0000000003000000005a420f00c0f2fcffffffffff000000000cfeffff18fcff"
        "fff30100004d000000000000000000000000a1b9092100000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000aff0000"
    ),
    "new": (
        "73e1c5ea73e1c5ea000000000000000000000000000000000000000000000000"
        "0000000003000000005a420f00b03cffffffffffff000000000cfeffff18fcff"
        "fff30100004d000000000000000000000000a1b9092100000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000aff0000"
    ),
}


def _make_intent(d: dict) -> Format3Intent:
    return Format3Intent(
        entry=d["entry"],
        key=d["key"],
        field=d["field"],
        op="set",
        old=d.get("old"),
        new=d["new"],
    )


# ── Validator behavior pins ─────────────────────────────────────────


def test_validator_accepts_buff_data_raw_intent_on_skill():
    """A Format 3 intent on skill.pabgb whose field is ``_buff_data_raw``
    and which carries an ``old`` hex string MUST validate as supported.

    Routes through the no-PABGB-schema branch's ``i.old is not None``
    raw-replacement clause (format3_handler._routable). If this fails,
    every CrimsonWings intent is reported as skipped at import time
    with the misleading "table not in pabgb_complete_schema.json"
    message — exactly what GitHub issue #77 reports.
    """
    intent = _make_intent(CRIMSONWINGS_FIRST_INTENT)
    result = validate_intents("skill.pabgb", [intent])

    assert len(result.supported) == 1, (
        f"Expected the _buff_data_raw intent with old+new to validate "
        f"as supported. Got {len(result.skipped)} skipped: "
        f"{[r for _, r in result.skipped]}"
    )
    assert len(result.skipped) == 0


def test_validator_skips_buff_data_raw_intent_without_old_on_skill():
    """Sanity check the converse: when ``old`` is missing, the intent
    has no resolution path on skill.pabgb (no PABGB schema, no
    field_schema entry for ``_buff_data_raw``) and must be skipped
    with a precise reason naming both schema sources.
    """
    intent = Format3Intent(
        entry="Attack_Big_Stamina_Decrease",
        key=77,
        field="_buff_data_raw",
        op="set",
        new="deadbeef",
        old=None,
    )
    result = validate_intents("skill.pabgb", [intent])

    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert "skill" in reason
    assert "pabgb_complete_schema.json" in reason
    assert "field_schema/skill.json" in reason


def test_validator_accepts_full_crimsonwings_25pct_payload(tmp_path):
    """Parse the actual CrimsonWings_25pct.field.json shape and confirm
    every one of the 365 ``_buff_data_raw`` intents validates.

    Embedded as a 3-intent fixture rather than the full 365 to keep
    the test fast and self-contained, but uses the same target,
    field, op, and old/new shape as the production zip ships.
    """
    payload = {
        "modinfo": {
            "title": "CrimsonWings - Stamina & Spirit 25%",
            "version": "1.0",
            "author": "DatGuySnowfox",
        },
        "format": 3,
        "target": "skill.pabgb",
        "intents": [
            CRIMSONWINGS_FIRST_INTENT,
            {
                "entry": "Active_Recovery_SP",
                "key": 52133,
                "field": "_buff_data_raw",
                "old": "deadbeefcafef00d",
                "new": "0df0fecaefbeadde",
            },
            {
                "entry": "Active_Recovery_MP",
                "key": 52134,
                "field": "_buff_data_raw",
                "old": "1234567890abcdef",
                "new": "fedcba0987654321",
            },
        ],
    }
    p = tmp_path / "CrimsonWings_25pct.field.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    target, intents = parse_format3_mod(p)
    assert target == "skill.pabgb"
    assert len(intents) == 3
    assert all(i.field == "_buff_data_raw" for i in intents)
    assert all(i.old is not None for i in intents)

    result = validate_intents(target, intents)
    assert len(result.supported) == 3, (
        f"All 3 CrimsonWings intents should validate. Skipped: "
        f"{[r for _, r in result.skipped]}"
    )
    assert len(result.skipped) == 0


# ── Apply-path behavior pins ────────────────────────────────────────


def test_apply_emits_raw_replacement_change_on_skill_pabgb():
    """End-to-end: a CrimsonWings-style intent on skill.pabgb whose
    ``old`` bytes appear exactly once in the entry payload must produce
    a v2-style change with the right rel_offset, original, and patched
    bytes.
    """
    # Place the CrimsonWings 'old' blob inside the entry's payload
    # surrounded by other bytes so the search has to find it.
    old_bytes = bytes.fromhex(CRIMSONWINGS_FIRST_INTENT["old"])
    new_bytes = bytes.fromhex(CRIMSONWINGS_FIRST_INTENT["new"])
    assert len(old_bytes) == len(new_bytes)

    # Pad: 4 leading bytes + old_bytes + 4 trailing bytes
    payload = b"\xAA\xBB\xCC\xDD" + old_bytes + b"\xEE\xFF\x11\x22"
    entry = _skill_entry(77, "Attack_Big_Stamina_Decrease", payload)
    body, header = _skill_pabgb([(77, entry)])

    intent = _make_intent(CRIMSONWINGS_FIRST_INTENT)
    changes = _intents_to_v2_changes(
        "skill.pabgb", body, header, [intent])

    assert len(changes) == 1, (
        f"Expected one raw-replacement change. Got {len(changes)}. "
        f"This regresses voiddoiv's v3.2.10 _buff_data_raw routing."
    )
    c = changes[0]
    assert c["entry"] == "Attack_Big_Stamina_Decrease"
    assert c["original"] == old_bytes.hex()
    assert c["patched"] == new_bytes.hex()
    assert c["label"] == "Attack_Big_Stamina_Decrease._buff_data_raw"


def test_apply_skips_when_old_bytes_not_in_skill_entry():
    """If the vanilla skill.pabgb entry does NOT contain ``old``, the
    intent should be silently dropped at apply time. Same shape the
    user sees when their game version's vanilla bytes have drifted from
    the bytes the mod author exported."""
    # Entry payload doesn't contain the CrimsonWings 'old' pattern
    payload = b"\xFF" * 256
    entry = _skill_entry(77, "Attack_Big_Stamina_Decrease", payload)
    body, header = _skill_pabgb([(77, entry)])

    intent = _make_intent(CRIMSONWINGS_FIRST_INTENT)
    changes = _intents_to_v2_changes(
        "skill.pabgb", body, header, [intent])

    assert changes == []
