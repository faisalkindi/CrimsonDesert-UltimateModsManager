"""Raw-XML-drop format support: some mod authors ship gamepad / UI
XML mods as plain `.xml` files inside a folder with a `modinfo.json`,
no `OG_` prefix and no Crimson Browser manifest. Until v3.2.7,
CDUMM rejected these with "no recognized mod format" because every
detector required a structural marker.

Fix: when no other format applies, scan the extracted dir for plain
`.xml` files, look each up in the game's PAMT by basename, and
register the matches via the existing OG_ XML pipeline.

Source: RockNBeard's Nexus comment 2026-04-30 about "Standard
Gamepad Layout" mod 1489 (file 1777549676).
"""
from __future__ import annotations
from pathlib import Path
import zipfile

import pytest


def _fake_pamt_entry(target_name: str):
    """Stand-in for the real PazEntry returned by _find_pamt_entry."""
    from cdumm.archive.paz_parse import PazEntry
    return PazEntry(
        path=target_name,
        paz_file="0008/0.paz",
        offset=1024,
        comp_size=512,
        orig_size=512,
        flags=0,
        paz_index=0,
    )


def test_plain_xml_drop_creates_mod(tmp_path, monkeypatch):
    """ZIP with `<folder>/inputmap_common.xml` + modinfo.json must
    create a mod when the basename matches a vanilla PAMT entry."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    # Pretend `inputmap_common.xml` exists in vanilla PAMT
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: (
            _fake_pamt_entry("inputmap_common.xml")
            if target.lower() == "inputmap_common.xml" else None
        ),
    )

    zip_path = tmp_path / "gamepad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "Standard Gamepad Layout/inputmap_common.xml",
            b"<?xml version='1.0'?><inputmap><binding/></inputmap>\n",
        )
        zf.writestr(
            "Standard Gamepad Layout/modinfo.json",
            b'{"modinfo": {"title": "Standard Gamepad Layout",'
            b' "version": "2.0.2", "author": "test"}}',
        )
        zf.writestr("Standard Gamepad Layout/readme.txt", b"hi\n")

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)

    result = import_from_zip(
        zip_path=zip_path, game_dir=game_dir, db=db,
        snapshot=snapshot, deltas_dir=deltas_dir,
    )

    assert result.error is None, (
        f"Plain XML drop mod should import. Got: {result.error!r}"
    )
    assert result.mod_id is not None, "mod row not created"

    delta_rows = db.connection.execute(
        "SELECT entry_path FROM mod_deltas WHERE mod_id = ?",
        (result.mod_id,)
    ).fetchall()
    paths = {r[0] for r in delta_rows}
    assert "inputmap_common.xml" in paths, (
        f"Expected inputmap_common.xml in mod_deltas, got {paths!r}"
    )


def test_plain_xml_no_pamt_match_falls_through(tmp_path, monkeypatch):
    """If none of the plain XML files match a vanilla PAMT entry, the
    plain-XML detector falls through and the import errors as
    'no recognized format' (existing behavior, not regressed)."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    # Pretend NO XML matches vanilla
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: None,
    )

    zip_path = tmp_path / "junk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Mod/random_unknown_file.xml",
                    b"<?xml version='1.0'?><stuff/>\n")

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)

    result = import_from_zip(
        zip_path=zip_path, game_dir=game_dir, db=db,
        snapshot=snapshot, deltas_dir=deltas_dir,
    )

    # Should fail cleanly, NOT register a bogus mod
    assert result.mod_id is None, (
        "Mod row was created for junk XML — plain-XML detector should "
        "only fire when basename matches PAMT"
    )


def test_real_standard_gamepad_layout_zip(tmp_path, monkeypatch):
    """Integration test using RockNBeard's actual ZIP from Nexus mod
    1489 (Standard Gamepad Layout v0.1, file 1777549676). Verifies
    the detector handles the real-world structure:
    - outer container folder `Standard Gamepad Layout/`
    - nested `ui/` subdir
    - two XML files (`inputmap.xml`, `inputmap_common.xml`)
    - modinfo.json at the mod root
    - Layout.txt readme as a sibling
    Skips when the fixture ZIP isn't downloaded into Downloads.
    """
    real_zip = Path(
        r"C:\Users\faisa\Downloads\Compressed"
        r"\Standard Gamepad Layout-1489-2-0-2-1777549676.zip"
    )
    if not real_zip.exists():
        pytest.skip(f"Real fixture ZIP not at {real_zip}")

    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    # Pretend the game's PAMT contains inputmap.xml and inputmap_common.xml
    matched = {"inputmap.xml", "inputmap_common.xml"}
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: (
            _fake_pamt_entry(target.lower())
            if target.lower() in matched else None
        ),
    )

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)

    result = import_from_zip(
        zip_path=real_zip, game_dir=game_dir, db=db,
        snapshot=snapshot, deltas_dir=deltas_dir,
    )

    assert result.error is None, (
        f"Real Standard Gamepad Layout ZIP should import. Got: "
        f"{result.error!r}"
    )
    assert result.mod_id is not None

    delta_rows = db.connection.execute(
        "SELECT entry_path FROM mod_deltas WHERE mod_id = ?",
        (result.mod_id,)
    ).fetchall()
    paths = {r[0] for r in delta_rows}
    # Both XML files matched the mocked PAMT and got registered
    assert "inputmap.xml" in paths
    assert "inputmap_common.xml" in paths
    # Mod name picked from modinfo.json title
    name_row = db.connection.execute(
        "SELECT name FROM mods WHERE id = ?", (result.mod_id,)
    ).fetchone()
    assert "Standard Gamepad Layout" in name_row[0]


def test_og_xml_takes_precedence_over_plain_xml(tmp_path, monkeypatch):
    """A ZIP containing both OG_-prefixed AND plain XML files must
    use the OG_ pipeline (which has explicit target naming), not the
    plain-XML fallback."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: _fake_pamt_entry(target),
    )

    zip_path = tmp_path / "og_only.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "Mod/OG_inventory__mymod.xml",
            b"<?xml version='1.0'?><inventory>data</inventory>\n",
        )

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)

    result = import_from_zip(
        zip_path=zip_path, game_dir=game_dir, db=db,
        snapshot=snapshot, deltas_dir=deltas_dir,
    )

    assert result.mod_id is not None
    delta_rows = db.connection.execute(
        "SELECT entry_path FROM mod_deltas WHERE mod_id = ?",
        (result.mod_id,)
    ).fetchall()
    paths = {r[0] for r in delta_rows}
    # Target should be "inventory.xml" (from OG_inventory__mymod.xml),
    # NOT "OG_inventory__mymod.xml" (the source filename) — the OG_
    # detector ran, plain-XML fallback was skipped.
    assert "inventory.xml" in paths, (
        f"OG_ format should resolve target=inventory.xml, got {paths!r}"
    )
    assert "OG_inventory__mymod.xml" not in paths, (
        "Plain-XML fallback wrongly picked up an OG_-named file"
    )
