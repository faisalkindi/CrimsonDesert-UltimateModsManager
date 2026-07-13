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
merely fail to apply, it CORRUPTS.

#294 refused the combination outright. #296 does better: it works out where
those bytes MOVED to and rewrites the offsets, so both mods apply. But the
re-anchor needs the rebuilt table, which only exists after
``expand_format3_into_aggregated`` runs -- so the aggregator TAGS the
changes and ``_reanchor_offsets_onto_rebuilds`` moves them afterwards.

The refusal is still there, as the fallback it should always have been:
  * the Format 3 rebuild never materialised -> the offsets are still stale;
  * a change can't be re-anchored -> the two mods disagree about those bytes.
"""
from __future__ import annotations

import json

import pytest

from cdumm.engine.apply_engine import (
    _normalize_target, _reanchor_offsets_onto_rebuilds,
    aggregate_json_mods_into_synthetic_patches,
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


# ── stage 1: the aggregator TAGS, it no longer refuses ──────────────────

def test_the_offset_changes_are_tagged_for_reanchoring(tmp_path):
    """They must survive aggregation. #294 dropped them here, which made
    #296's re-anchor unreachable -- the module was shipped, tested, and
    never called by anything."""
    db = _FakeDB([
        _row(1, "Armor Five Sockets", _write(tmp_path, "f3.json", FORMAT3_MOD)),
        _row(2, "Mission Efficiency x20",
             _write(tmp_path, "off.json", OFFSET_MOD)),
    ])

    synth, _summary = aggregate_json_mods_into_synthetic_patches(db)

    assert len(synth["patches"]) == 1
    changes = synth["patches"][0]["changes"]
    assert len(changes) == 1
    assert changes[0]["_needs_reanchor"] == "Armor Five Sockets", (
        "the change must carry WHICH mod rebuilds the table, so the "
        "re-anchor stage can name it if it has to refuse")
    assert not synth.get("_refused_offset_mods"), (
        "nothing is refused yet -- that's decided after the rebuild exists")


# ── stage 2: re-anchor onto the rebuilt table (the fix) ─────────────────

def _tagged(offset, original, patched):
    return {"offset": offset, "original": original, "patched": patched,
            "_needs_reanchor": "Armor Five Sockets",
            "_source_mod_name": "Mission Efficiency x20"}


def test_offsets_are_moved_onto_the_rebuilt_table(tmp_path):
    """The whole point: both mods apply. A record before the patch grew by
    4 bytes, so the patch sits 4 bytes later in the rebuilt table."""
    vanilla = bytes(range(64)) * 4
    rebuilt = vanilla[:16] + b"\xaa\xaa\xaa\xaa" + vanilla[16:]
    off = 100
    orig = vanilla[off:off + 2]

    aggregated = {"gamedata/iteminfo.pabgb": [
        {"offset": 0, "original": vanilla.hex(), "patched": rebuilt.hex()},
        _tagged(off, orig.hex(), "ffff"),
    ]}
    synth: dict = {}

    _reanchor_offsets_onto_rebuilds(aggregated, synth)

    kept = aggregated["gamedata/iteminfo.pabgb"]
    moved = [c for c in kept if "_reanchored_from" in c]
    assert len(moved) == 1
    assert moved[0]["offset"] == off + 4, "offset must follow the bytes"
    assert moved[0]["_reanchored_from"] == off
    assert rebuilt[moved[0]["offset"]:moved[0]["offset"] + 2] == orig, (
        "the re-anchored offset must land on the author's exact bytes")
    assert "_needs_reanchor" not in moved[0], "the tag is consumed"
    assert not synth.get("_refused_offset_mods")


def test_a_missing_rebuild_is_refused_not_written_blind(tmp_path):
    """A Format 3 mod claimed this table but produced no rebuilt body (its
    extraction failed). The offsets are STILL stale. Writing them would be
    exactly the corruption #293 reported."""
    aggregated = {"gamedata/iteminfo.pabgb": [_tagged(100, "6400", "ffff")]}
    synth: dict = {}

    _reanchor_offsets_onto_rebuilds(aggregated, synth)

    assert aggregated["gamedata/iteminfo.pabgb"] == [], "not applied"
    refused = synth["_refused_offset_mods"]
    assert len(refused) == 1
    assert refused[0]["mod_name"] == "Mission Efficiency x20"
    assert refused[0]["rebuilt_by"] == "Armor Five Sockets"  # names BOTH
    assert "rebuild is missing" in refused[0]["reason"]


def test_a_change_the_format3_mod_overwrote_is_refused(tmp_path):
    """Genuine disagreement: the Format 3 mod changed the very bytes the
    offset mod patches. There is no right answer, so we don't invent one."""
    vanilla = bytes(range(64)) * 4
    off = 100
    orig = vanilla[off:off + 2]
    # the rebuild rewrites those same two bytes
    rebuilt = vanilla[:off] + b"\x77\x77" + vanilla[off + 2:]

    aggregated = {"gamedata/iteminfo.pabgb": [
        {"offset": 0, "original": vanilla.hex(), "patched": rebuilt.hex()},
        _tagged(off, orig.hex(), "ffff"),
    ]}
    synth: dict = {}

    _reanchor_offsets_onto_rebuilds(aggregated, synth)

    kept = aggregated["gamedata/iteminfo.pabgb"]
    assert all("_needs_reanchor" not in c for c in kept)
    assert len(kept) == 1, "only the Format 3 rebuild survives"
    assert synth["_refused_offset_mods"], "the disagreement must be surfaced"


def test_untagged_changes_are_left_completely_alone(tmp_path):
    """No Format 3 mod on this table -> nothing to re-anchor -> don't touch
    it. An over-eager fix is its own bug."""
    aggregated = {"gamedata/skill.pabgb": [
        {"offset": 100, "original": "00", "patched": "01"},
    ]}
    synth: dict = {}

    _reanchor_offsets_onto_rebuilds(aggregated, synth)

    assert aggregated["gamedata/skill.pabgb"] == [
        {"offset": 100, "original": "00", "patched": "01"}]
    assert not synth.get("_refused_offset_mods")


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
