"""GitHub #165 (jikulopo): Reimport-from-source does not work for
Crimson Browser mods (e.g. Nexus 203 Map Tweaks). Root cause: a CB /
loose-file / texture mod is converted to .paz/.pamt via
convert_to_paz_mod, and the CONVERTED output (not the original archive)
was what got copied to CDMods/sources/<mod_id>/. The converted .paz
carries offsets baked against the vanilla at original-import time and
has no manifest, so reimport-from-source re-detects it as a plain PAZ
mod and re-applies the stale bytes instead of re-converting against the
current vanilla. The mod stays outdated / crashes; only a full
redownload (which re-fetches the original archive) works. jikulopo's
"you're only keeping .pamt and .paz files in sources" was correct.

Fix: _process_extracted_files takes an optional source_archive_dir.
When provided (the original extracted archive), THAT is archived to
sources/ instead of the post-conversion extracted_dir. Plain PAZ mods
pass nothing and keep archiving extracted_dir (unchanged).
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.import_handler import _process_extracted_files
from cdumm.engine.snapshot_manager import SnapshotManager, SnapshotWorker
from cdumm.storage.database import Database


def _setup_game_and_snapshot(tmp_path: Path):
    game_dir = tmp_path / "game"
    d = game_dir / "0008"
    d.mkdir(parents=True)
    (d / "0.pamt").write_bytes(b"PAMT_HEADER" + b"\x00" * 100)
    (d / "0.paz").write_bytes(b"PAZ_FILE_CONTENT" + b"\x00" * 200)
    meta = game_dir / "meta"
    meta.mkdir()
    (meta / "0.papgt").write_bytes(b"PAPGT_DATA" + b"\x00" * 50)
    (game_dir / "bin64").mkdir()
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"EXE")

    db = Database(tmp_path / "test.db")
    db.initialize()
    SnapshotWorker(game_dir, db.db_path).run()
    snapshot = SnapshotManager(db)
    deltas_dir = tmp_path / "deltas"
    return game_dir, db, snapshot, deltas_dir


def _make_converted_extracted_dir(tmp_path: Path, game_dir: Path) -> Path:
    """A post-conversion dir: a modified 0008/0.paz, so the import
    matches a game file and reaches the source-archive step. Stands in
    for convert_to_paz_mod's output (no manifest, just paz/pamt)."""
    extracted = tmp_path / "converted"
    (extracted / "0008").mkdir(parents=True)
    vanilla = (game_dir / "0008" / "0.paz").read_bytes()
    modified = bytearray(vanilla)
    modified[20:30] = b"\xFF" * 10
    (extracted / "0008" / "0.paz").write_bytes(bytes(modified))
    return extracted


def _make_original_archive(tmp_path: Path) -> Path:
    """The original CB droppable: a manifest + files/, the thing that
    must survive in sources/ so reimport can re-detect + re-convert."""
    original = tmp_path / "original_cb"
    (original / "files" / "0012").mkdir(parents=True)
    (original / "manifest.json").write_text('{"id": "map-tweaks"}',
                                            encoding="utf-8")
    (original / "files" / "0012" / "worldmapview.css").write_text(
        "body{}", encoding="utf-8")
    return original


def test_source_archive_dir_archives_the_original_not_converted(tmp_path: Path):
    """#165: when source_archive_dir is given, sources/<mod_id>/ must
    hold the ORIGINAL archive (manifest present), not the converted paz."""
    game_dir, db, snapshot, deltas_dir = _setup_game_and_snapshot(tmp_path)
    extracted = _make_converted_extracted_dir(tmp_path, game_dir)
    original = _make_original_archive(tmp_path)

    result = _process_extracted_files(
        extracted, game_dir, db, snapshot, deltas_dir, "Map Tweaks",
        source_archive_dir=original)

    assert result.mod_id is not None
    sources = deltas_dir.parent / "sources" / str(result.mod_id)
    assert (sources / "manifest.json").exists(), (
        "reimport source must keep the original CB archive (manifest), so "
        "reimport can re-detect + re-convert against current vanilla")
    assert (sources / "files" / "0012" / "worldmapview.css").exists()
    # The converted paz must NOT be what we archived as the source.
    assert not (sources / "0008" / "0.paz").exists(), (
        "the post-conversion paz must not masquerade as the mod source")


def test_default_archives_extracted_dir_unchanged(tmp_path: Path):
    """Plain PAZ mods pass no override: behaviour is unchanged, the
    extracted dir itself is archived as the source."""
    game_dir, db, snapshot, deltas_dir = _setup_game_and_snapshot(tmp_path)
    extracted = _make_converted_extracted_dir(tmp_path, game_dir)

    result = _process_extracted_files(
        extracted, game_dir, db, snapshot, deltas_dir, "Plain Paz Mod")

    assert result.mod_id is not None
    sources = deltas_dir.parent / "sources" / str(result.mod_id)
    assert (sources / "0008" / "0.paz").exists(), (
        "default (no override) must keep archiving the extracted dir")
