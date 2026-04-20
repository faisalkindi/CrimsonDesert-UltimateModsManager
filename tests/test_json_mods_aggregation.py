"""#145 Option Y: aggregate enabled JSON mods' patches into a single
pass so `_apply_byte_patches`'s cumulative-delta tracking handles
inserts across mod boundaries.

Before this fix, two JSON mods targeting `gamedata/iteminfo.pabgb` —
one doing pure replaces (Fat Stacks JSON) and one with byte inserts
(ExtraSockets V2.2.0) — produced separate overlay entries that the
byte-merger refused to combine when sizes differed, silently dropping
one mod ("Only Fat Stacks is active for that file").
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cdumm.engine.apply_engine import (
    aggregate_json_mods_into_synthetic_patches,
)


def _mk_db(tmp_path, mods: list[dict]):
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE mods (
        id INTEGER PRIMARY KEY, name TEXT, mod_type TEXT,
        enabled INTEGER, priority INTEGER, json_source TEXT,
        disabled_patches TEXT)""")
    con.execute("""CREATE TABLE mod_config (
        mod_id INTEGER, custom_values TEXT)""")
    for m in mods:
        json_path = tmp_path / f"{m['name']}.json"
        json_path.write_text(json.dumps(m["data"]), encoding="utf-8")
        con.execute(
            "INSERT INTO mods VALUES (?, ?, 'paz', ?, ?, ?, ?)",
            (m["id"], m["name"], int(m.get("enabled", 1)),
             m.get("priority", 1), str(json_path),
             json.dumps(m.get("disabled_patches", []))
             if m.get("disabled_patches") else None))
    con.commit()

    class _DbShim:
        connection = con
    return _DbShim()


def test_two_mods_same_target_aggregate_into_one_patch_entry(tmp_path):
    db = _mk_db(tmp_path, [
        {
            "id": 1, "name": "Fat Stacks", "priority": 2,
            "data": {"patches": [{
                "game_file": "gamedata/iteminfo.pabgb",
                "signature": "ABCD",
                "changes": [
                    {"offset": 100, "original": "00", "patched": "FF"},
                    {"offset": 200, "original": "01", "patched": "EE"},
                ],
            }]},
        },
        {
            "id": 2, "name": "ExtraSockets", "priority": 1,
            "data": {"patches": [{
                "game_file": "gamedata/iteminfo.pabgb",
                "signature": "ABCD",
                "changes": [
                    {"offset": 300, "type": "insert", "bytes": "DEADBEEF"},
                    {"offset": 400, "original": "02", "patched": "DD"},
                ],
            }]},
        },
    ])
    synth, summary = aggregate_json_mods_into_synthetic_patches(db)
    assert len(synth["patches"]) == 1, (
        "both mods target the same game_file → one combined patch")
    patch = synth["patches"][0]
    assert patch["game_file"] == "gamedata/iteminfo.pabgb"
    assert len(patch["changes"]) == 4, (
        "4 changes total (2 from each mod) — none should be dropped")
    assert patch["signature"] == "ABCD"
    # priority ordering — mods ORDER BY priority DESC so priority=2
    # (Fat Stacks, lower precedence) comes first, priority=1
    # (ExtraSockets, winner on collision) comes last.
    assert summary[0]["mod_name"] == "Fat Stacks"
    assert summary[1]["mod_name"] == "ExtraSockets"


def test_priority_1_changes_come_last_so_they_win_on_offset_ties(tmp_path):
    db = _mk_db(tmp_path, [
        {
            "id": 1, "name": "LoPriority", "priority": 5,
            "data": {"patches": [{
                "game_file": "gamedata/foo.pabgb",
                "changes": [
                    {"offset": 100, "original": "00", "patched": "AA"},
                ],
            }]},
        },
        {
            "id": 2, "name": "HiPriority", "priority": 1,
            "data": {"patches": [{
                "game_file": "gamedata/foo.pabgb",
                "changes": [
                    {"offset": 100, "original": "00", "patched": "BB"},
                ],
            }]},
        },
    ])
    synth, _ = aggregate_json_mods_into_synthetic_patches(db)
    changes = synth["patches"][0]["changes"]
    assert len(changes) == 2
    # HiPriority (priority=1, winner) must come LAST so when
    # _apply_byte_patches stable-sorts by offset and ties, the later
    # entry overwrites → HiPriority wins.
    assert changes[0]["patched"] == "AA", "LoPriority first"
    assert changes[1]["patched"] == "BB", "HiPriority last → wins on tie"


def test_disabled_patches_filtered_before_aggregation(tmp_path):
    db = _mk_db(tmp_path, [
        {
            "id": 1, "name": "M", "priority": 1,
            "disabled_patches": [1],  # disable second change
            "data": {"patches": [{
                "game_file": "gamedata/x.pabgb",
                "changes": [
                    {"offset": 100, "original": "00", "patched": "AA"},
                    {"offset": 200, "original": "00", "patched": "BB"},
                    {"offset": 300, "original": "00", "patched": "CC"},
                ],
            }]},
        },
    ])
    synth, _ = aggregate_json_mods_into_synthetic_patches(db)
    changes = synth["patches"][0]["changes"]
    assert len(changes) == 2, "change at flat_idx=1 must be dropped"
    patched_values = [c["patched"] for c in changes]
    assert "BB" not in patched_values


def test_disabled_mod_contributes_nothing(tmp_path):
    db = _mk_db(tmp_path, [
        {
            "id": 1, "name": "Off", "priority": 1,
            "enabled": False,
            "data": {"patches": [{
                "game_file": "gamedata/x.pabgb",
                "changes": [{"offset": 100, "original": "00", "patched": "FF"}],
            }]},
        },
    ])
    synth, summary = aggregate_json_mods_into_synthetic_patches(db)
    assert synth["patches"] == []
    assert summary == []


def test_non_overlapping_targets_kept_separate(tmp_path):
    db = _mk_db(tmp_path, [
        {"id": 1, "name": "A", "priority": 1, "data": {"patches": [
            {"game_file": "gamedata/a.pabgb", "changes": [
                {"offset": 1, "original": "00", "patched": "AA"}]}]}},
        {"id": 2, "name": "B", "priority": 2, "data": {"patches": [
            {"game_file": "gamedata/b.pabgb", "changes": [
                {"offset": 1, "original": "00", "patched": "BB"}]}]}},
    ])
    synth, _ = aggregate_json_mods_into_synthetic_patches(db)
    assert len(synth["patches"]) == 2, (
        "two mods, two DIFFERENT targets → two patch entries")
    game_files = {p["game_file"] for p in synth["patches"]}
    assert game_files == {"gamedata/a.pabgb", "gamedata/b.pabgb"}
