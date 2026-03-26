import zipfile
from pathlib import Path

from cdmm.engine.import_handler import (
    detect_format,
    import_from_folder,
    import_from_zip,
)
from cdmm.engine.snapshot_manager import SnapshotManager, SnapshotWorker
from cdmm.storage.database import Database


def _setup_game_and_snapshot(tmp_path: Path) -> tuple[Path, Database, SnapshotManager, Path]:
    """Create a fake game dir, database, snapshot, and deltas dir."""
    game_dir = tmp_path / "game"

    # Create PAZ directories with files
    for dir_name in ["0008"]:
        d = game_dir / dir_name
        d.mkdir(parents=True)
        (d / "0.pamt").write_bytes(b"PAMT_HEADER" + b"\x00" * 100)
        (d / "0.paz").write_bytes(b"PAZ_FILE_CONTENT" + b"\x00" * 200)

    # Create meta
    meta = game_dir / "meta"
    meta.mkdir()
    (meta / "0.papgt").write_bytes(b"PAPGT_DATA" + b"\x00" * 50)

    # Create exe for validation
    (game_dir / "bin64").mkdir()
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"EXE")

    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = SnapshotWorker(game_dir, db.db_path)
    worker.run()

    snapshot = SnapshotManager(db)
    deltas_dir = tmp_path / "deltas"

    return game_dir, db, snapshot, deltas_dir


def test_detect_format_zip(tmp_path: Path) -> None:
    z = tmp_path / "mod.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("test.txt", "data")
    assert detect_format(z) == "zip"


def test_detect_format_folder(tmp_path: Path) -> None:
    d = tmp_path / "mod_folder"
    d.mkdir()
    assert detect_format(d) == "folder"


def test_detect_format_bat(tmp_path: Path) -> None:
    s = tmp_path / "install.bat"
    s.write_text("echo hello")
    assert detect_format(s) == "script"


def test_detect_format_py(tmp_path: Path) -> None:
    s = tmp_path / "patch.py"
    s.write_text("print('hi')")
    assert detect_format(s) == "script"


def test_detect_format_bsdiff(tmp_path: Path) -> None:
    f = tmp_path / "patch.bsdiff"
    f.write_bytes(b"\x00")
    assert detect_format(f) == "bsdiff"


def test_detect_format_unknown(tmp_path: Path) -> None:
    f = tmp_path / "readme.txt"
    f.write_text("hello")
    assert detect_format(f) == "unknown"


def test_import_from_zip(tmp_path: Path) -> None:
    game_dir, db, snapshot, deltas_dir = _setup_game_and_snapshot(tmp_path)

    # Create a mod zip with a modified PAZ file
    vanilla_paz = (game_dir / "0008" / "0.paz").read_bytes()
    modified_paz = bytearray(vanilla_paz)
    modified_paz[20:30] = b"\xFF" * 10
    modified_paz = bytes(modified_paz)

    mod_zip = tmp_path / "TestMod.zip"
    with zipfile.ZipFile(mod_zip, "w") as zf:
        zf.writestr("0008/0.paz", modified_paz)

    result = import_from_zip(mod_zip, game_dir, db, snapshot, deltas_dir)

    assert result.error is None
    assert result.name == "TestMod"
    assert len(result.changed_files) == 1
    assert result.changed_files[0]["file_path"] == "0008/0.paz"

    # Verify delta stored in database
    cursor = db.connection.execute("SELECT COUNT(*) FROM mod_deltas WHERE mod_id = 1")
    assert cursor.fetchone()[0] > 0

    # Verify mod stored
    cursor = db.connection.execute("SELECT name, mod_type FROM mods WHERE id = 1")
    row = cursor.fetchone()
    assert row == ("TestMod", "paz")

    db.close()


def test_import_from_zip_no_game_files(tmp_path: Path) -> None:
    game_dir, db, snapshot, deltas_dir = _setup_game_and_snapshot(tmp_path)

    # Zip with non-game files
    mod_zip = tmp_path / "BadMod.zip"
    with zipfile.ZipFile(mod_zip, "w") as zf:
        zf.writestr("readme.txt", "This is not a game file")

    result = import_from_zip(mod_zip, game_dir, db, snapshot, deltas_dir)
    assert result.error is not None
    assert "No recognized game files" in result.error
    db.close()


def test_import_from_folder(tmp_path: Path) -> None:
    game_dir, db, snapshot, deltas_dir = _setup_game_and_snapshot(tmp_path)

    # Create a mod folder with modified PAZ
    mod_folder = tmp_path / "MyMod"
    (mod_folder / "0008").mkdir(parents=True)
    vanilla_paz = (game_dir / "0008" / "0.paz").read_bytes()
    modified_paz = bytearray(vanilla_paz)
    modified_paz[50:55] = b"\xAA" * 5
    (mod_folder / "0008" / "0.paz").write_bytes(bytes(modified_paz))

    result = import_from_folder(mod_folder, game_dir, db, snapshot, deltas_dir)

    assert result.error is None
    assert result.name == "MyMod"
    assert len(result.changed_files) == 1
    db.close()


def test_import_identical_file_skipped(tmp_path: Path) -> None:
    game_dir, db, snapshot, deltas_dir = _setup_game_and_snapshot(tmp_path)

    # Create a mod with an identical (unmodified) PAZ
    mod_folder = tmp_path / "IdenticalMod"
    (mod_folder / "0008").mkdir(parents=True)
    vanilla_paz = (game_dir / "0008" / "0.paz").read_bytes()
    (mod_folder / "0008" / "0.paz").write_bytes(vanilla_paz)

    result = import_from_folder(mod_folder, game_dir, db, snapshot, deltas_dir)

    # Mod is created but with no changed files (identical content is skipped)
    assert result.error is None
    assert len(result.changed_files) == 0
    db.close()
