"""statusinfo ``stat_level_data`` writer (DIRECT SPEED .cdmod stat mods).

The always-active stat presets (DIRECT MOVEMENT SPEED, DIRECT ATTACK SPEED,
...) set ``stat_level_data[0..15]`` on ``statusinfo.pabgb`` rate records. These
tests prove the writer applies them byte-exact and refuses any record that is
not a rate stat.

Ground truth (real 1.13 statusinfo, committed fixture): the four rate stats
-- MoveSpeedRate (1000011), AttackSpeedRate (1000010), CriticalRate (1000007),
DHIT (1000004) -- carry ``stat_level_data`` as 16 int64s at tail offset 80; the
other 71 stats have an 84-byte tail with no such array. The DIRECT MOVEMENT
SPEED x10 ``.cdmod`` sets all 16 MoveSpeedRate elements to 2,500,000,000.
"""
from __future__ import annotations

import json
import sqlite3
import struct
import zipfile

import pytest

from cdumm.engine.format3_handler import Format3Intent, validate_intents
from cdumm.engine.statusinfo_writer import build_statusinfo_changes
from cdumm.semantic.parser import parse_pabgh_index

from tests.fixture_loaders import has_vanilla113, load_vanilla113

FIXTURE = "statusinfo.pabgb"
MOVE_SPEED = 1000011
RATE_KEYS = {1000011, 1000010, 1000007, 1000004}

pytestmark = pytest.mark.skipif(
    not has_vanilla113(FIXTURE),
    reason="1.13 statusinfo fixture not present")


def _intent(key: int, idx: int, val: int) -> Format3Intent:
    return Format3Intent(entry="MoveSpeedRate", key=key,
                         field=f"stat_level_data[{idx}]", op="set", new=val)


def _apply(body: bytes, changes: list[dict]) -> bytes:
    out = bytearray(body)
    for c in changes:
        off = c["offset"]
        orig = bytes.fromhex(c["original"])
        assert out[off:off + len(orig)] == orig, "change 'original' must anchor"
        out[off:off + len(orig)] = bytes.fromhex(c["patched"])
    return bytes(out)


def _record(body: bytes, header: bytes, key: int) -> tuple[int, bytes]:
    _, offsets = parse_pabgh_index(header, "statusinfo")
    starts = sorted(offsets.values())
    o = offsets[key]
    i = starts.index(o)
    e = starts[i + 1] if i + 1 < len(starts) else len(body)
    return o, body[o:e]


def _stat_level_data(rec: bytes) -> list[int]:
    name_len = struct.unpack_from("<I", rec, 4)[0]
    blk = rec[8 + name_len + 80: 8 + name_len + 80 + 128]
    return [struct.unpack_from("<q", blk, i * 8)[0] for i in range(16)]


def test_direct_speed_applies_and_is_length_preserving():
    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")

    intents = [_intent(MOVE_SPEED, i, 2_500_000_000) for i in range(16)]
    changes, dropped = build_statusinfo_changes(body, header, intents)
    assert not dropped, dropped
    assert len(changes) == 1

    modified = _apply(body, changes)

    # Length-preserving: the table stays the same size, so the companion
    # .pabgh offsets remain valid without any rebuild.
    assert len(modified) == len(body)

    # Every changed byte falls inside MoveSpeedRate's 128-byte block.
    start, rec = _record(body, header, MOVE_SPEED)
    name_len = struct.unpack_from("<I", rec, 4)[0]
    blk0 = start + 8 + name_len + 80
    diff = [j for j in range(len(body)) if body[j] != modified[j]]
    assert diff, "the mod must change something"
    assert all(blk0 <= j < blk0 + 128 for j in diff), (
        "changes must be confined to the stat_level_data block")

    # The 16 elements now read exactly 2,500,000,000 (low u32) with a zeroed
    # high half -- setting the whole int64 element, matching the .cdmod.
    _, new_rec = _record(modified, header, MOVE_SPEED)
    assert _stat_level_data(new_rec) == [2_500_000_000] * 16


def test_every_other_record_is_byte_identical():
    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")
    intents = [_intent(MOVE_SPEED, i, 2_500_000_000) for i in range(16)]
    changes, dropped = build_statusinfo_changes(body, header, intents)
    modified = _apply(body, changes)

    _, offsets = parse_pabgh_index(header, "statusinfo")
    starts = sorted(offsets.values())
    for key, o in offsets.items():
        if key == MOVE_SPEED:
            continue
        i = starts.index(o)
        e = starts[i + 1] if i + 1 < len(starts) else len(body)
        assert body[o:e] == modified[o:e], (
            f"record {key} must be untouched")


def test_refuses_non_rate_record():
    """A regular stat (84-byte tail) has no stat_level_data; writing into its
    tail would corrupt it, so the writer must refuse, not write."""
    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")
    _, offsets = parse_pabgh_index(header, "statusinfo")
    regular = next(k for k in offsets if k not in RATE_KEYS)

    changes, dropped = build_statusinfo_changes(
        body, header, [_intent(regular, 0, 2_500_000_000)])
    assert changes == []
    assert len(dropped) == 1
    assert "not a rate stat" in dropped[0][1]


def test_refuses_out_of_range_index():
    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")
    changes, dropped = build_statusinfo_changes(
        body, header, [_intent(MOVE_SPEED, 16, 1)])
    assert changes == []
    assert "out of range" in dropped[0][1]


def test_refuses_missing_key():
    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")
    changes, dropped = build_statusinfo_changes(
        body, header, [_intent(99_999_999, 0, 1)])
    assert changes == []
    assert "no record" in dropped[0][1]


def test_setting_to_vanilla_value_is_a_noop():
    """Writing an element back to its current value must produce no change --
    the writer only emits a change when bytes actually differ."""
    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")
    _, rec = _record(body, header, MOVE_SPEED)
    current = _stat_level_data(rec)
    intents = [_intent(MOVE_SPEED, i, current[i]) for i in range(16)]
    changes, dropped = build_statusinfo_changes(body, header, intents)
    assert changes == []
    assert not dropped


def test_validate_intents_accepts_stat_level_data():
    """The wildcard LIST_WRITERS registration must classify a stat_level_data
    set as supported, so it reaches the writer instead of being skipped as
    schema-less (statusinfo has no CDUMM PABGB schema)."""
    intents = [Format3Intent(entry="MoveSpeedRate", key=MOVE_SPEED,
                             field=f"stat_level_data[{i}]", op="set",
                             new=2_500_000_000) for i in range(16)]
    v = validate_intents("statusinfo.pabgb", intents)
    assert len(v.supported) == 16, v
    assert not v.skipped, v


def test_end_to_end_cdmod_to_byte_change(tmp_path):
    """The full user path: a DIRECT SPEED ``.cdmod`` (built here with the exact
    shape of the real Nexus package) -> cdmod_to_format3 -> validate ->
    whole-table dispatch -> statusinfo writer -> a single byte-exact change
    that sets all 16 MoveSpeedRate levels to 2,500,000,000, length-preserving.
    """
    # .cdmod import is a fork-only feature (#288); skip cleanly where it is
    # absent so the core statusinfo writer stays portable to upstream.
    ch = pytest.importorskip("cdumm.engine.cdmod_handler")
    cdmod_to_format3 = ch.cdmod_to_format3
    from cdumm.engine.format3_apply import expand_format3_into_aggregated

    body = load_vanilla113("statusinfo.pabgb")
    header = load_vanilla113("statusinfo.pabgh")

    manifest = {
        "format": "crimson-mod-package", "format_version": 1,
        "name": "DIRECT MOVEMENT SPEED - 10X", "version": "1.13.01",
        "components": [{"type": "semantic-patch",
                        "path": "patches/semantic.json"}],
    }
    semantic = {
        "schema": 1,
        "targets": [{
            "file": "statusinfo.pabgb",
            "operations": [
                {"op": "set", "conversion": "conservative",
                 "path": f"stat_level_data[{i}]",
                 "selector": {"key": MOVE_SPEED,
                              "string_key": "MoveSpeedRate"},
                 "value": 2_500_000_000}
                for i in range(16)
            ],
        }],
    }
    cdmod = tmp_path / "direct_speed.cdmod"
    with zipfile.ZipFile(cdmod, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("patches/semantic.json", json.dumps(semantic))

    doc = cdmod_to_format3(cdmod)
    jp = tmp_path / "direct_speed.json"
    jp.write_text(json.dumps(doc), encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, enabled "
        "INTEGER, json_source TEXT, priority INTEGER, mod_type TEXT)")
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)")
    conn.execute(
        "INSERT INTO mods VALUES (1, 'DirectSpeed', 1, ?, 5, 'paz')",
        (str(jp),))
    conn.commit()
    db = type("DB", (), {"connection": conn})()

    aggregated: dict = {}
    signatures: dict = {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (body, header)
        if gf == "statusinfo.pabgb" else None)

    changes = aggregated.get("statusinfo.pabgb") or []
    assert len(changes) == 1, aggregated
    modified = _apply(body, changes)
    assert len(modified) == len(body)
    _, rec = _record(modified, header, MOVE_SPEED)
    assert _stat_level_data(rec) == [2_500_000_000] * 16
