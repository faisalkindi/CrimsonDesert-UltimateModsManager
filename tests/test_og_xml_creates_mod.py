"""OG_ XML full-replacement format regression: a folder/ZIP shipping
only `OG_<name>__<suffix>.xml` files used to write the replacement
.entr file to disk but never insert a `mods` row, never insert
`mod_deltas` rows, and never set `result.mod_id`. The import then
fell through to other detectors which all failed, surfacing as
"no recognized mod content" — and the orphaned .entr file sat in
deltas_dir with no DB owner.

Fix: when OG_ XML detection finds at least one target that resolves
against the game's PAMT, create a real mod row + mod_deltas rows and
return success.
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


def test_og_xml_only_zip_creates_mod_row(tmp_path, monkeypatch):
    """A ZIP containing only `OG_inventory__mymod.xml` must create a
    mod row + mod_deltas row, not an orphan delta file."""
    from cdumm.engine import import_handler
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    # Force PAMT lookup to succeed without a real game install
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: _fake_pamt_entry("inventory.xml"),
    )

    zip_path = tmp_path / "ogmod.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("OG_inventory__mymod.xml",
                    b"<?xml version='1.0'?><inventory/>\n")

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
        f"OG_-only mod should import cleanly. Got error: {result.error!r}"
    )
    assert result.mod_id is not None, (
        "OG_-only mod must produce a mod_id (mod row was never inserted)"
    )

    # Verify mod row exists in DB
    row = db.connection.execute(
        "SELECT name FROM mods WHERE id = ?", (result.mod_id,)
    ).fetchone()
    assert row is not None, "mods row missing for OG_ mod"

    # Verify mod_deltas row exists
    delta_rows = db.connection.execute(
        "SELECT file_path, entry_path, delta_path FROM mod_deltas "
        "WHERE mod_id = ?", (result.mod_id,)
    ).fetchall()
    assert len(delta_rows) >= 1, (
        "mod_deltas row missing — OG_ replacement was not registered"
    )

    # Verify delta file lives under deltas_dir/<mod_id>/ (mod_id subdir),
    # not at deltas_dir root (FIX 6: prevent silent overwrite when two
    # OG_ files target the same vanilla path across mods).
    delta_path = Path(delta_rows[0][2])
    assert str(result.mod_id) in delta_path.parts, (
        f"OG_ delta {delta_path} must be under deltas_dir/{result.mod_id}/, "
        f"not at deltas_dir root"
    )


def test_two_og_files_same_target_no_silent_overwrite(tmp_path, monkeypatch):
    """Two OG_ files targeting the same vanilla path inside one mod
    must not silently overwrite each other on disk. They land under
    a per-mod subdir so cross-mod collisions are also impossible."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: _fake_pamt_entry(target),
    )

    zip_path = tmp_path / "two_og.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("OG_a__mod.xml",
                    b"<?xml version='1.0'?><a><foo>bar</foo></a>\n")
        zf.writestr("OG_b__mod.xml",
                    b"<?xml version='1.0'?><b><baz>qux</baz></b>\n")

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

    assert result.error is None
    assert result.mod_id is not None

    delta_rows = db.connection.execute(
        "SELECT delta_path FROM mod_deltas WHERE mod_id = ?",
        (result.mod_id,)
    ).fetchall()
    assert len(delta_rows) == 2, (
        f"Both OG_ replacements should produce delta rows. Got "
        f"{len(delta_rows)}: {delta_rows!r}"
    )
    paths = {Path(r[0]).name for r in delta_rows}
    assert len(paths) == 2, (
        f"Two OG_ files with different targets must produce two "
        f"distinct delta files, got {paths!r}"
    )


def test_empty_og_xml_file_is_skipped(tmp_path, monkeypatch):
    """An empty (or near-empty) OG_ XML file would otherwise produce
    a 3-byte BOM-only delta that bricks the target file. The
    importer must skip it with a warning, not register it."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, game_dir: _fake_pamt_entry(target),
    )

    zip_path = tmp_path / "empty_og.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("OG_empty__mod.xml", b"")
        # One real one alongside so the import succeeds overall
        zf.writestr("OG_real__mod.xml",
                    b"<?xml version='1.0'?><real>data</real>\n")

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
    assert "real.xml" in paths
    assert "empty.xml" not in paths, (
        "Empty OG_ file should have been skipped, not registered"
    )
    assert result.info and "skipped" in result.info.lower(), (
        f"User-facing skip note missing from result.info: {result.info!r}"
    )
