"""Priority ordering in the JSON merge paths and delta dicts.

Audit findings 1 + 2 (2026-06-11):

1. ``_merge_json_patch_deltas`` applied ``reversed(...)`` to lists that
   ``_get_file_deltas`` already orders losers-first (``m.priority
   DESC``, lower number wins), which made the LOWEST-precedence mod
   write last and win overlapping bytes. With the reversal removed the
   winner (lowest priority number, last in the list) must own overlaps.

2. ``_get_file_deltas`` never carried a "priority" key, so the
   full-replace winner pick in ``_compose_file`` sorted everything as 0.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdumm.engine.apply_engine import ApplyWorker
from cdumm.storage.database import Database


class _FakeEntry:
    """Minimal stand-in for a PAMT entry / PazEntry."""

    def __init__(self) -> None:
        self.path = "data/table.pabgb"
        self.paz_file = ""
        self.offset = 0
        self.comp_size = 16
        self.orig_size = 16
        self.flags = 0
        self.paz_index = 0
        self.compression_type = 0
        self.encrypted = False


VANILLA = b"\x00" * 16


def _delta(name: str, delta_path: str, patched_hex: str) -> dict:
    return {
        "delta_path": delta_path,
        "mod_name": name,
        "is_new": False,
        "json_patches": json.dumps({
            "entry_path": "data/table.pabgb",
            "changes": [
                {"offset": 0, "original": "00", "patched": patched_hex},
            ],
        }),
    }


def test_fast_path_merge_winner_writes_last(tmp_path, monkeypatch):
    """The list arrives losers-first (priority DESC); the LAST delta is
    the priority winner and must own the overlapping byte."""
    import cdumm.engine.json_patch_handler as jph

    monkeypatch.setattr(jph, "_find_pamt_entry",
                        lambda gf, base: _FakeEntry())
    monkeypatch.setattr(jph, "_extract_from_paz",
                        lambda entry, paz_path=None: VANILLA)

    db = Database(tmp_path / "t.db")
    db.initialize()
    try:
        worker = ApplyWorker(tmp_path / "game", tmp_path / "vanilla",
                             db.db_path)
        worker._db = db
        (tmp_path / "game").mkdir(exist_ok=True)

        deltas = [
            _delta("Loser (priority 5)", "loser.delta", "aa"),
            _delta("Winner (priority 1)", "winner.delta", "bb"),
        ]
        merged, remaining = worker._merge_json_patch_deltas(
            "0008/0.paz", deltas)
        assert remaining == []
        assert len(merged) == 1
        content = merged[0]["_merged_content"]
        assert content[0] == 0xBB, (
            "winner (last in losers-first order) must own the "
            "overlapping byte")
    finally:
        db.close()


def test_get_file_deltas_carries_priority(tmp_path):
    db = Database(tmp_path / "t.db")
    db.initialize()
    try:
        d1 = tmp_path / "d1.delta"
        d2 = tmp_path / "d2.delta"
        d1.write_bytes(b"SPRS" + b"\x00" * 4)
        d2.write_bytes(b"SPRS" + b"\x00" * 4)
        db.connection.execute(
            "INSERT INTO mods (id, name, mod_type, enabled, priority) "
            "VALUES (1, 'High', 'paz', 1, 1)")
        db.connection.execute(
            "INSERT INTO mods (id, name, mod_type, enabled, priority) "
            "VALUES (2, 'Low', 'paz', 1, 7)")
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end) VALUES (1, '0008/0.paz', ?, 0, 4)",
            (str(d1),))
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end) VALUES (2, '0008/0.paz', ?, 0, 4)",
            (str(d2),))
        db.connection.commit()

        worker = ApplyWorker(tmp_path / "game", tmp_path / "vanilla",
                             db.db_path)
        worker._db = db
        file_deltas = worker._get_file_deltas()
        ds = file_deltas["0008/0.paz"]
        by_name = {d["mod_name"]: d for d in ds}
        assert by_name["High"]["priority"] == 1
        assert by_name["Low"]["priority"] == 7
        # priority DESC ordering: loser (7) first, winner (1) last.
        assert [d["mod_name"] for d in ds] == ["Low", "High"]
        # The full-replace winner rule: sorted ascending, [0] wins.
        winner = sorted(ds, key=lambda d: d.get("priority", 0))[0]
        assert winner["mod_name"] == "High"
    finally:
        db.close()
