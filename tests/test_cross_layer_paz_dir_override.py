"""#145: cross-layer merge — when a PAZ-dir mod ships its own copy of
a logical game file AND another JSON mod patches the same file, the
JSON patches must stack ON TOP of the PAZ-dir mod's content instead of
either silently winning over the other in-game.

Reporter scenario: estereba had Fat Stacks 9999 (ships `0036/0.paz`
containing a modified `gamedata/iteminfo.pabgb`) enabled alongside
ExtraSockets V2.2.0 (a JSON patch on `gamedata/iteminfo.pabgb`). One
mod's changes always vanished in-game. The fix:

1. ``collect_paz_dir_overrides`` walks enabled PAZ-dir mods' stored
   paz+pamt, returns a map of logical file → override metadata.
2. ``resolve_vanilla_source`` checks the map before falling through
   to vanilla/live resolution; if the game_file is claimed, it hands
   back a ``PazEntry`` bound to the PAZ-dir mod's stored paz.
3. Phase 1 skips direct-staging the PAZ-dir mod's ``NNNN/0.paz`` so
   the overlay (which now contains the JSON-patched version of its
   content) wins cleanly in-game.
"""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from cdumm.engine.apply_engine import (
    collect_paz_dir_overrides, resolve_vanilla_source,
)


class _PamtEntryStub:
    """Minimal PazEntry-compatible stub — `parse_pamt` isn't invoked
    in the unit test so we patch it out."""
    def __init__(self, path, paz_file, offset=0, comp_size=0,
                 decomp_size=0, flags=0):
        self.path = path
        self.paz_file = paz_file
        self.offset = offset
        self.comp_size = comp_size
        self.decomp_size = decomp_size
        self.flags = flags


def _mk_db_with_paz_dir_mod(tmpdir: Path, mod_name: str, priority: int,
                            pamt_dir: str, logical_path: str,
                            enabled: bool = True):
    """Build an in-memory sqlite DB with one PAZ-dir mod + its
    NNNN/0.paz and NNNN/0.pamt deltas pointing at temp files."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
                "mod_type TEXT, enabled INTEGER, priority INTEGER)")
    con.execute("CREATE TABLE mod_deltas (id INTEGER PRIMARY KEY, "
                "mod_id INTEGER, file_path TEXT, delta_path TEXT, "
                "entry_path TEXT)")
    con.execute("INSERT INTO mods VALUES (?,?,?,?,?)",
                (1, mod_name, "paz", int(enabled), priority))

    # Synthetic delta files on disk — content irrelevant, parse_pamt
    # is mocked.
    paz_delta = tmpdir / f"{mod_name}_0.paz"
    pamt_delta = tmpdir / f"{mod_name}_0.pamt"
    paz_delta.write_bytes(b"\x00" * 16)
    pamt_delta.write_bytes(b"\x00" * 16)

    con.execute("INSERT INTO mod_deltas VALUES (?,?,?,?,?)",
                (1, 1, f"{pamt_dir}/0.paz", str(paz_delta), None))
    con.execute("INSERT INTO mod_deltas VALUES (?,?,?,?,?)",
                (2, 1, f"{pamt_dir}/0.pamt", str(pamt_delta), None))
    con.commit()

    class _DbShim:
        connection = con
    return _DbShim(), paz_delta, pamt_delta


def test_collect_returns_override_for_enabled_paz_dir_mod(monkeypatch, tmp_path):
    db, paz_path, pamt_path = _mk_db_with_paz_dir_mod(
        tmp_path, "Fat Stacks 9999", priority=5, pamt_dir="0036",
        logical_path="gamedata/iteminfo.pabgb")

    monkeypatch.setattr(
        "cdumm.archive.paz_parse.parse_pamt",
        lambda path, paz_dir=None: [_PamtEntryStub(
            path="gamedata/iteminfo.pabgb", paz_file=str(paz_path),
            offset=0, comp_size=771660)])

    overrides = collect_paz_dir_overrides(db)
    assert "gamedata/iteminfo.pabgb" in overrides
    ov = overrides["gamedata/iteminfo.pabgb"]
    assert ov["mod_name"] == "Fat Stacks 9999"
    assert ov["pamt_dir"] == "0036"
    assert ov["priority"] == 5
    # entry.paz_file was rebound to the mod's stored paz so the
    # existing _extract_from_paz helper reads from there.
    assert ov["entry"].paz_file == str(paz_path)


def test_collect_skips_disabled_paz_dir_mod(monkeypatch, tmp_path):
    db, _, _ = _mk_db_with_paz_dir_mod(
        tmp_path, "Fat Stacks 9999", priority=5, pamt_dir="0036",
        logical_path="gamedata/iteminfo.pabgb", enabled=False)
    monkeypatch.setattr(
        "cdumm.archive.paz_parse.parse_pamt",
        lambda *a, **kw: [_PamtEntryStub(
            path="gamedata/iteminfo.pabgb", paz_file="x")])

    overrides = collect_paz_dir_overrides(db)
    assert overrides == {}, "disabled PAZ-dir mods must not supply overrides"


def test_resolver_returns_override_entry_instead_of_vanilla(monkeypatch, tmp_path):
    """When an override exists for the requested game_file, the
    resolver must return the PAZ-dir mod's entry instead of falling
    through to vanilla lookup."""
    override_entry = _PamtEntryStub(
        path="gamedata/iteminfo.pabgb",
        paz_file=str(tmp_path / "fs_0.paz"),
        offset=0, comp_size=12345)
    overrides = {
        "gamedata/iteminfo.pabgb": {
            "mod_id": 1, "mod_name": "Fat Stacks 9999",
            "priority": 5, "pamt_dir": "0036",
            "paz_delta_path": str(tmp_path / "fs_0.paz"),
            "pamt_delta_path": str(tmp_path / "fs_0.pamt"),
            "entry": override_entry,
        }
    }

    # Mock snapshot manager just so the signature is satisfied —
    # we should never reach the vanilla/live code path at all.
    snap_mgr = MagicMock()
    warn_calls = []

    result = resolve_vanilla_source(
        "gamedata/iteminfo.pabgb",
        vanilla_dir=tmp_path / "vanilla",
        game_dir=tmp_path / "game",
        snapshot_mgr=snap_mgr,
        warn_callback=lambda m: warn_calls.append(m),
        paz_dir_overrides=overrides)

    assert result is override_entry, (
        "resolver must return the override's PazEntry exactly, not a "
        "copy or freshly-looked-up vanilla entry")
    assert any("cross-layer" in m for m in warn_calls), (
        "caller must be told about the cross-layer base change so the "
        "GUI can surface it like the live-PAZ self-heal warning")


def test_resolver_falls_through_when_no_override_for_game_file(monkeypatch, tmp_path):
    """An override for ONE file must not bleed into resolution of
    OTHER files — the resolver must fall through to vanilla lookup."""
    # Override exists for iteminfo.pabgb but NOT for
    # skill.pabgb, which is the target of this call.
    unrelated_entry = _PamtEntryStub(
        path="gamedata/iteminfo.pabgb",
        paz_file=str(tmp_path / "x_0.paz"))
    overrides = {
        "gamedata/iteminfo.pabgb": {
            "mod_id": 1, "mod_name": "Fat Stacks",
            "priority": 5, "pamt_dir": "0036",
            "paz_delta_path": "x", "pamt_delta_path": "x",
            "entry": unrelated_entry,
        }
    }

    # Stub the vanilla-lookup helper to make the fall-through path
    # return a sentinel we can distinguish.
    sentinel_van = _PamtEntryStub(
        path="gamedata/skill.pabgb", paz_file="van_0.paz")

    def fake_find(game_file, base):
        # Only return for vanilla_dir; live side returns None so we
        # never reach the hash-verify branch.
        if "vanilla" in str(base):
            return sentinel_van
        return None

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry", fake_find)

    # The vanilla paz file must exist on disk or resolve_vanilla_source
    # will fall through to the live path.
    (tmp_path / "van_0.paz").write_bytes(b"")
    sentinel_van.paz_file = str(tmp_path / "van_0.paz")

    result = resolve_vanilla_source(
        "gamedata/skill.pabgb",
        vanilla_dir=tmp_path / "vanilla",
        game_dir=tmp_path / "game",
        snapshot_mgr=MagicMock(),
        paz_dir_overrides=overrides)

    assert result is sentinel_van, (
        f"expected fall-through to vanilla for unrelated file, got {result}")


def test_collect_lower_priority_wins_on_same_game_file(monkeypatch, tmp_path):
    """Two PAZ-dir mods both ship the same logical file — the lower
    priority number (CDUMM convention = top) wins."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
                "mod_type TEXT, enabled INTEGER, priority INTEGER)")
    con.execute("CREATE TABLE mod_deltas (id INTEGER PRIMARY KEY, "
                "mod_id INTEGER, file_path TEXT, delta_path TEXT, "
                "entry_path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?,?,?,?)", [
        (1, "MegaStacks", "paz", 1, 3),   # higher number → loses
        (2, "FatStacks",  "paz", 1, 1),   # lower number → wins
    ])
    for mid, mname in [(1, "mega"), (2, "fat")]:
        paz_path = tmp_path / f"{mname}_0.paz"
        pamt_path = tmp_path / f"{mname}_0.pamt"
        paz_path.write_bytes(b"")
        pamt_path.write_bytes(b"")
        con.execute("INSERT INTO mod_deltas VALUES (NULL,?,?,?,NULL)",
                    (mid, "0036/0.paz", str(paz_path)))
        con.execute("INSERT INTO mod_deltas VALUES (NULL,?,?,?,NULL)",
                    (mid, "0036/0.pamt", str(pamt_path)))
    con.commit()

    class _DbShim:
        connection = con

    monkeypatch.setattr(
        "cdumm.archive.paz_parse.parse_pamt",
        lambda path, paz_dir=None: [
            _PamtEntryStub(path="gamedata/iteminfo.pabgb", paz_file=path)])

    overrides = collect_paz_dir_overrides(_DbShim())
    assert overrides["gamedata/iteminfo.pabgb"]["mod_name"] == "FatStacks"
    assert overrides["gamedata/iteminfo.pabgb"]["priority"] == 1
