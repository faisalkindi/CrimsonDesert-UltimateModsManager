"""Mixed-ZIP regression: ASI files staged by import_from_zip used to
land in the auto-deleted tempfile.TemporaryDirectory tree. Once the
function returned, the staging dir was wiped and the GUI-side install
loop saw `asi_staged` paths that no longer existed on disk. Result:
ASI plugins shipped inside mixed ZIPs (PAZ + ASI together) silently
vanished even though the ZIP imported "successfully".

Fix: stage ASI files into deltas_dir/_asi_staging/ which persists
across the worker→GUI handoff. The GUI handler cleans up after copy.
"""
from __future__ import annotations
from pathlib import Path
import zipfile

import pytest


def _make_mixed_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("MyMod.asi", b"MZ\x00\x00fake-asi-bytes")
        zf.writestr("MyMod.ini", b"[Settings]\nKey=Value\n")
        # Bogus PAZ side so the importer reaches the staging block
        # then fails downstream (we don't care if the PAZ side fails).
        zf.writestr("0008/0.paz", b"PAZ\x00fake-paz")


def test_asi_staged_paths_exist_after_import(tmp_path):
    """Staged ASI paths must remain on disk after import_from_zip
    returns, so the GUI handler can copy them into bin64/."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    zip_path = tmp_path / "mixed.zip"
    _make_mixed_zip(zip_path)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)

    # Import will likely fail on the PAZ side (no real vanilla data)
    # but staging must happen first and the paths must persist.
    result = import_from_zip(
        zip_path=zip_path, game_dir=game_dir, db=db,
        snapshot=snapshot, deltas_dir=deltas_dir,
    )

    assert result.asi_staged, (
        "Mixed ZIP must populate result.asi_staged with the staged "
        "ASI/INI files. Got empty list."
    )
    for staged in result.asi_staged:
        assert Path(staged).exists(), (
            f"Staged ASI path {staged!r} was deleted before the GUI "
            f"handler could copy it. Stage outside the auto-cleaned "
            f"tempfile directory."
        )
        # Belt-and-suspenders: ensure the staging dir is under
        # deltas_dir (persistent), not under a tempfile tree.
        assert str(deltas_dir.resolve()) in str(Path(staged).resolve()), (
            f"Staging path {staged!r} is not under deltas_dir — "
            f"will be auto-cleaned by tempfile."
        )


def test_game_data_ini_not_stolen_by_asi_staging(tmp_path):
    """Regression: a `.ini` inside a numbered game-data dir
    (e.g., `0008/foo.ini`) must NOT be staged as an ASI companion.
    Only `.ini` files whose stem matches a sibling `.asi` should
    be picked up.
    """
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    zip_path = tmp_path / "trap.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Game-data ini that should stay where it is
        zf.writestr("0008/foo.ini", b"[GameData]\nKey=Value\n")
        # Just enough PAZ-side content to avoid early "no files" exit
        zf.writestr("0008/0.paz", b"PAZ\x00fake")

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

    # The game-data .ini has no companion .asi, so it must NOT be
    # in asi_staged.
    for staged in (result.asi_staged or []):
        assert not staged.endswith("foo.ini"), (
            f"Game-data .ini was incorrectly staged as ASI: {staged}"
        )
