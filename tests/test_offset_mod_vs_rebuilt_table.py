"""Byte-offset mods must not be applied to a table a Format 3 mod rebuilds.

GitHub #293, reported by falobos76 on #191.

A Format 3 mod doesn't patch bytes: CDUMM parses the whole table, edits
records and RE-SERIALIZES it. Records change size, so every byte offset
after the first edited record MOVES.

A Format 2 mod patches fixed offsets. Applied against a rebuilt table, its
write lands in the middle of some other record -- the table is structurally
invalid and the game will not start. That's what falobos76 hit: pinapana's
socket mods (Format 3, iteminfo) plus any of three offset mods that also
patch iteminfo. Each works alone.

CDUMM already knew which tables get rebuilt -- `f3_target_files` was
collected and then used ONLY for a display label. Nothing guarded on it.

This is worse than the silent no-ops of #259/#275/#278/#285: it doesn't
merely fail to apply, it CORRUPTS. So the bar is that the unsafe
combination is refused, loudly, naming both mods.
"""
from __future__ import annotations

import json

import pytest

from cdumm.engine.apply_engine import (
    _normalize_target, aggregate_json_mods_into_synthetic_patches,
)


class _FakeDB:
    """Just enough DB to drive the aggregator."""

    def __init__(self, rows):
        self._rows = rows
        self.connection = self

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return self._rows


def _row(mod_id, name, path, priority=0):
    return (mod_id, name, str(path), None, priority, None)


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


OFFSET_MOD = {
    "format": 2,
    "patches": [{
        "game_file": "gamedata/iteminfo.pabgb",
        "changes": [{"offset": 2265877, "original": "6400",
                     "patched": "ffff"}],
    }],
}

FORMAT3_MOD = {
    "format": 3,
    "format_minor": 1,
    "modinfo": {"title": "Armor Five Sockets"},
    "targets": [{
        "file": "iteminfo.pabgb",
        "intents": [{"entry": "", "key": 14510, "field": "max_stack_count",
                     "op": "set", "new": 5}],
    }],
}


# ── the guard ───────────────────────────────────────────────────────────

def test_offset_mod_is_refused_when_a_format3_mod_rebuilds_the_table(
        tmp_path):
    db = _FakeDB([
        _row(1, "Armor Five Sockets", _write(tmp_path, "f3.json", FORMAT3_MOD)),
        _row(2, "Mission Efficiency x20",
             _write(tmp_path, "off.json", OFFSET_MOD)),
    ])

    synth, _summary = aggregate_json_mods_into_synthetic_patches(db)

    # the offset mod contributed NOTHING -- it was refused, not applied
    assert synth["patches"] == [], (
        "a byte-offset patch was aggregated against a table that a Format 3 "
        "mod rebuilds; its offsets are stale and this corrupts the file")

    refused = synth.get("_refused_offset_mods") or []
    assert len(refused) == 1
    r = refused[0]
    assert r["mod_name"] == "Mission Efficiency x20"
    assert r["rebuilt_by"] == "Armor Five Sockets"   # names BOTH mods
    assert "iteminfo" in r["game_file"]


def test_the_format3_mod_itself_is_untouched(tmp_path):
    """The guard drops the unsafe offset write, not the Format 3 mod."""
    db = _FakeDB([
        _row(1, "Armor Five Sockets", _write(tmp_path, "f3.json", FORMAT3_MOD)),
        _row(2, "Offset Mod", _write(tmp_path, "off.json", OFFSET_MOD)),
    ])
    synth, _s = aggregate_json_mods_into_synthetic_patches(db)
    # Format 3 mods never go through this byte aggregator at all -- they have
    # no "patches" key. Nothing here should have swallowed them.
    assert "_refused_offset_mods" in synth


# ── no false refusals ───────────────────────────────────────────────────

def test_an_offset_mod_on_a_DIFFERENT_table_still_applies(tmp_path):
    """The failure mode of an over-eager guard: refusing what was safe."""
    other = {
        "format": 2,
        "patches": [{
            "game_file": "gamedata/skill.pabgb",
            "changes": [{"offset": 100, "original": "00", "patched": "01"}],
        }],
    }
    db = _FakeDB([
        _row(1, "Armor Five Sockets", _write(tmp_path, "f3.json", FORMAT3_MOD)),
        _row(2, "Skill Mod", _write(tmp_path, "s.json", other)),
    ])
    synth, _s = aggregate_json_mods_into_synthetic_patches(db)

    assert not synth.get("_refused_offset_mods")
    assert len(synth["patches"]) == 1
    assert "skill" in synth["patches"][0]["game_file"]


def test_offset_mods_alone_are_untouched(tmp_path):
    """No Format 3 mod enabled -> nothing to go stale -> apply as before.
    This is the case that has always worked; it must keep working."""
    db = _FakeDB([
        _row(1, "Offset Mod", _write(tmp_path, "off.json", OFFSET_MOD)),
    ])
    synth, _s = aggregate_json_mods_into_synthetic_patches(db)

    assert not synth.get("_refused_offset_mods")
    assert len(synth["patches"]) == 1
    assert synth["patches"][0]["changes"][0]["offset"] == 2265877


# ── the path-vs-bare-name trap ──────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("gamedata/iteminfo.pabgb", "iteminfo.pabgb"),
    ("gamedata/binary__/client/bin/iteminfo.pabgb", "gamedata/iteminfo.pabgb"),
    ("gamedata\\iteminfo.pabgb", "iteminfo.pabgb"),
    ("gamedata/ITEMINFO.pabgb", "iteminfo.pabgb"),
])
def test_a_format3_target_and_a_format2_game_file_are_compared_fairly(a, b):
    """Format 3 ships a path; Format 2 ships a different path. Comparing
    them raw silently never matches -- and a guard that never matches is a
    guard that isn't there. This exact trap made `match` select zero records
    (#275) and array_append no-op (#278). Third time, so it's pinned."""
    assert _normalize_target(a) == _normalize_target(b)
