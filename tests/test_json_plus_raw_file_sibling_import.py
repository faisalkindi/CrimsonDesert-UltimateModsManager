"""JSON byte-patch imports that ship sibling raw-file replacements
must also import the raw-file portion.

Bug 2026-05-09 (goodygoosey, IIIF0RERUNNER, Nexus): crewny23's
"ALL Weapons and Armor Fully Usable on Every Single Character"
(mod 1543) ships TWO things in one folder:

  * Kliff_Damiane_Runtimepackages.json — JSON byte-patch hitting
    gamedata/characterinfo.pabgb. Drives weapons / movesets /
    walking animation.
  * UniEquip - 1.05.01 Update/files/gamedata/binary__/client/bin/
    {iteminfo,equipslotinfo}.{pabgb,pabgh} — loose-file
    replacements at the engine's inner path layout. Drives armor
    equipping.

CDUMM up through v3.2.14 picked up the JSON byte-patch via
``import_json_as_entr`` and returned, so the four loose files were
silently ignored. Symptom: "weapons work but armor doesn't" exactly
as reported.

Fix: after the JSON byte-patch branch lands the mod row, also run
``_detect_raw_file_replacements_via_pamt`` on the same drop folder
and persist any non-JSON matches as additional deltas under the
same mod_id via ``_persist_raw_match_deltas``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class _FakePazEntry:
    path: str
    paz_file: str
    offset: int = 0
    comp_size: int = 100
    orig_size: int = 100
    compression_type: int = 0
    flags: int = 0
    paz_index: int = 0
    encrypted: bool = False


def test_persist_helper_writes_one_delta_row_per_match(
        tmp_path, monkeypatch):
    """``_persist_raw_match_deltas`` is the helper that the JSON+raw
    sibling-import path uses to attach raw-file deltas to an
    already-existing mod row. It must NOT touch the mods table; it
    must NOT delete prior deltas; it must return one entry path per
    match and stage one save_entry_delta call per match."""
    from cdumm.engine import import_handler as ih

    src1 = tmp_path / "iteminfo.pabgb"
    src1.write_bytes(b"new iteminfo bytes")
    src2 = tmp_path / "equipslotinfo.pabgb"
    src2.write_bytes(b"new equipslotinfo bytes")

    matches = [
        ("gamedata/binary__/client/bin/iteminfo.pabgb",
         src1,
         _FakePazEntry(
             path="gamedata/binary__/client/bin/iteminfo.pabgb",
             paz_file=str(tmp_path / "0072" / "0.paz"),
             offset=100, comp_size=200, orig_size=200,
             paz_index=0)),
        ("gamedata/binary__/client/bin/equipslotinfo.pabgb",
         src2,
         _FakePazEntry(
             path="gamedata/binary__/client/bin/equipslotinfo.pabgb",
             paz_file=str(tmp_path / "0008" / "0.paz"),
             offset=300, comp_size=400, orig_size=400,
             paz_index=0)),
    ]

    saved: list[tuple] = []
    monkeypatch.setattr(
        "cdumm.engine.delta_engine.save_entry_delta",
        lambda content, metadata, delta_path: saved.append(
            (bytes(content), dict(metadata), delta_path)))

    class _FakeConn:
        def __init__(self):
            self.executes: list[tuple] = []
        def execute(self, sql, params=()):
            self.executes.append((sql, params))
            return self

    class _FakeDB:
        def __init__(self):
            self.connection = _FakeConn()

    db = _FakeDB()

    changed = ih._persist_raw_match_deltas(
        mod_id=42, matches=matches, db=db,
        deltas_dir=tmp_path / "deltas")

    assert sorted(changed) == sorted([
        "gamedata/binary__/client/bin/iteminfo.pabgb",
        "gamedata/binary__/client/bin/equipslotinfo.pabgb",
    ])

    # Two save_entry_delta calls, one per match
    assert len(saved) == 2

    # Two INSERTs into mod_deltas, both keyed on mod_id=42, NO
    # DELETE / UPDATE on mods table.
    inserts = [
        e for e in db.connection.executes
        if "INSERT INTO mod_deltas" in e[0]
    ]
    assert len(inserts) == 2
    for sql, params in inserts:
        assert params[0] == 42, (
            f"helper must use the supplied mod_id verbatim; "
            f"got {params[0]}"
        )

    # Confirm helper does NOT touch the mods table or delete prior
    # deltas — this is the contract that lets it run AFTER an already
    # successful JSON-byte-patch import without wiping that import's
    # work.
    bad = [
        sql for sql, _ in db.connection.executes
        if "DELETE FROM mod_deltas" in sql
        or "INSERT INTO mods" in sql
        or "UPDATE mods" in sql
    ]
    assert bad == [], (
        f"helper must not write the mods table or DELETE prior "
        f"deltas; saw: {bad!r}"
    )
