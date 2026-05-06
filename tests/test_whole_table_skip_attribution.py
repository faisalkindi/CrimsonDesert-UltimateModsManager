"""H3: when a Format 3 whole-table merged change (iteminfo.pabgb /
skill.pabgb) byte-mismatches at apply time, the skip must attribute
to EVERY contributing mod's id so all the affected mod cards light
up yellow.

Pre-fix: whole-table changes carry _target_file but not
_source_mod_id (the merged change represents N mods at once, so a
single int doesn't fit). On byte mismatch, _record_skip writes a
record with no mod id, persist_skip_summary skips it
(`if mod_id is None: continue`), the toast counts the skip but no
badge lights up. Users see 'X patches skipped' with zero clue which
mods caused it.

Post-fix: the whole-table change carries `_source_mod_ids: list[int]`,
_record_skip propagates it, persist_skip_summary fans out one row
per contributor.
"""
from __future__ import annotations

import json
import sqlite3

from cdumm.engine.apply_engine import persist_skip_summary


def _setup_mods_table(conn, mod_ids):
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, "
        "last_apply_skipped_count INTEGER NOT NULL DEFAULT 0, "
        "last_apply_skip_summary TEXT)"
    )
    for mid in mod_ids:
        conn.execute(
            "INSERT INTO mods (id, last_apply_skipped_count) VALUES (?, 0)",
            (mid,))
    conn.commit()


def test_persist_skip_fans_out_source_mod_ids_list_to_each_mod():
    """A single skip entry with _source_mod_ids=[10, 20, 30] must
    increment the skip count on all three mod rows, with the same
    summary entry on each."""
    conn = sqlite3.connect(":memory:")
    _setup_mods_table(conn, [10, 20, 30, 99])

    skips = [
        {
            "label": "iteminfo whole-table",
            "reason": "byte mismatch at offset 0",
            "_source_mod_ids": [10, 20, 30],
            "_target_file": "iteminfo.pabgb",
            "offset": 0,
            "actual": "deadbeef",
            "expected": "cafebabe",
        }
    ]
    participating = {10, 20, 30, 99}
    persist_skip_summary(conn, skips, participating)

    for mid in (10, 20, 30):
        row = conn.execute(
            "SELECT last_apply_skipped_count, last_apply_skip_summary "
            "FROM mods WHERE id = ?", (mid,)
        ).fetchone()
        assert row[0] == 1, (
            f"mod {mid} contributed to the whole-table change , its "
            f"skip count must be 1, got {row[0]}")
        summary = json.loads(row[1])
        assert summary[0]["file"] == "iteminfo.pabgb"
        assert "iteminfo whole-table" in summary[0]["label"]

    # Mod 99 didn't contribute , row stays at zero / NULL.
    row99 = conn.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id = 99").fetchone()
    assert row99[0] == 0
    assert row99[1] is None


def test_record_skip_propagates_source_mod_ids_list():
    """When the change dict has _source_mod_ids (plural list), the
    skip entry written by _apply_byte_patches must carry it through
    to skipped_out so persist_skip_summary can fan out."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    vanilla = b"\x00" * 16
    data = bytearray(vanilla)
    changes = [
        {"label": "merged", "offset": 0,
         "original": "ff", "patched": "11",  # mismatch (vanilla is 00)
         "_source_mod_ids": [42, 43],
         "_target_file": "iteminfo.pabgb"},
    ]
    skipped: list[dict] = []
    _apply_byte_patches(
        data, changes, signature=None, vanilla_data=vanilla,
        skipped_out=skipped)
    assert len(skipped) == 1
    assert skipped[0].get("_source_mod_ids") == [42, 43], (
        f"_source_mod_ids list must propagate from the change dict "
        f"to the skip entry, got {skipped[0]!r}")
    assert skipped[0].get("_target_file") == "iteminfo.pabgb"


def test_whole_table_writer_tags_merged_change_with_contributor_ids(tmp_path):
    """End-to-end: the whole-table writer in expand_format3_into_aggregated
    must stamp _source_mod_ids onto the single merged change it emits
    for iteminfo.pabgb / skill.pabgb."""
    from cdumm.engine import format3_apply as f3

    original_parse_targets = f3.parse_format3_mod_targets
    original_validate = f3.validate_intents
    original_intents_to = f3._intents_to_v2_changes

    class _ValRes:
        def __init__(self, supported):
            self.supported = supported
            self.skipped = []

    p1 = tmp_path / "m1.json"
    p2 = tmp_path / "m2.json"
    p1.write_text("{}")
    p2.write_text("{}")

    def _stub_parse_targets(p):
        return [("iteminfo.pabgb", [{"intent": str(p)}])]

    def _stub_validate(target, intents):
        return _ValRes(intents)

    # Whole-table writer collects intents, calls _intents_to_v2_changes
    # ONCE post-loop with the union, returns one merged change.
    def _stub_intents_to(target, body, header, intents):
        # Confirm we received the batched intents (2 mods worth).
        return [{"label": "iteminfo merged",
                 "offset": 0, "original": "00", "patched": "01"}]

    f3.parse_format3_mod_targets = _stub_parse_targets
    f3.validate_intents = _stub_validate
    f3._intents_to_v2_changes = _stub_intents_to

    class _C:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_a, **_kw):
            class _Cur:
                def __init__(self, r):
                    self.r = r

                def fetchall(self):
                    return self.r
            return _Cur(self._rows)

    class _D:
        def __init__(self, rows):
            self.connection = _C(rows)

    rows = [
        (501, "mod a", str(p1), 10),
        (502, "mod b", str(p2), 10),
    ]
    db = _D(rows)
    aggregated: dict[str, list[dict]] = {}
    signatures: dict[str, str] = {}

    try:
        f3.expand_format3_into_aggregated(
            aggregated, signatures, db,
            vanilla_extractor=lambda t: (b"\x00" * 32, b""),
        )
    finally:
        f3.parse_format3_mod_targets = original_parse_targets
        f3.validate_intents = original_validate
        f3._intents_to_v2_changes = original_intents_to

    merged_changes = aggregated.get("iteminfo.pabgb", [])
    assert len(merged_changes) == 1, (
        f"whole-table writer should emit ONE merged change, got "
        f"{len(merged_changes)}")
    mc = merged_changes[0]
    assert sorted(mc.get("_source_mod_ids", [])) == [501, 502], (
        f"merged change must carry both contributing mod ids in "
        f"_source_mod_ids, got {mc.get('_source_mod_ids')!r}")
    assert mc.get("_target_file") == "iteminfo.pabgb"
