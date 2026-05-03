"""Mixed RAR/7z archives that ship `.asi` plugins alongside PAZ
content used to silently drop the ASI half. The staging block in
`import_from_zip` (lines 2117-2208) wasn't mirrored in
`_import_from_extracted` (the helper used by both RAR and 7z paths).

Source: round 2 audit follow-up after F8 closed Format 3 detection
but didn't add ASI staging.

Fix: add `_stage_asi_files` helper, call from `import_from_rar` and
`import_from_7z` BEFORE `_import_from_extracted`, then re-attach the
staged paths to whatever result that helper returns.
"""
from __future__ import annotations
from pathlib import Path
import pytest


def test_stage_asi_files_moves_asi_and_companion_ini(tmp_path):
    """The helper must move `.asi` and matching `.ini` files out of
    the extract dir into a persistent per-import staging subdir, and
    return their final paths. Game-data `.ini` files (e.g. inside
    numbered PAZ dirs with no sibling `.asi`) must NOT be staged."""
    from cdumm.engine.import_handler import _stage_asi_files

    extract = tmp_path / "extract"
    extract.mkdir()

    # An ASI plugin + companion config
    (extract / "MyMod.asi").write_bytes(b"MZ\x00\x00fake")
    (extract / "MyMod.ini").write_bytes(b"[MyMod]\nopt=1\n")

    # Game data .ini that must NOT be moved (no sibling .asi)
    game_dir = extract / "0008"
    game_dir.mkdir()
    (game_dir / "config.ini").write_bytes(b"[Game]\nx=1\n")

    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    staged = _stage_asi_files(extract, deltas_dir)

    assert len(staged) == 2, (
        f"Expected MyMod.asi + MyMod.ini staged, got {staged!r}"
    )
    for p in staged:
        assert Path(p).exists(), f"staged path missing: {p}"
        assert "_asi_staging" in p
    # Game-data .ini must remain in place
    assert (game_dir / "config.ini").exists(), (
        "Game-data .ini was incorrectly staged"
    )


def test_stage_asi_files_returns_empty_when_no_asi(tmp_path):
    from cdumm.engine.import_handler import _stage_asi_files

    extract = tmp_path / "extract"
    extract.mkdir()
    (extract / "readme.txt").write_bytes(b"hi")
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    staged = _stage_asi_files(extract, deltas_dir)
    assert staged == []


def test_stage_asi_files_persistent_after_caller_temp_cleanup(tmp_path):
    """Staged paths must survive after the caller's tempfile.TemporaryDirectory
    auto-deletes — that's the whole point of staging into deltas_dir."""
    import tempfile
    from cdumm.engine.import_handler import _stage_asi_files

    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    with tempfile.TemporaryDirectory() as tmp:
        extract = Path(tmp)
        (extract / "MyMod.asi").write_bytes(b"asi")
        staged = _stage_asi_files(extract, deltas_dir)
    # tmp dir auto-deleted here; staged paths should still exist
    assert staged
    for p in staged:
        assert Path(p).exists(), (
            f"Staged path {p} was deleted with the tempfile dir. "
            f"Staging must use a persistent location."
        )
