"""Multi-file mods: skip individual files that fail, don't reject
the whole mod.

Bug from Faisal 2026-04-29 (Faster NPC Animations Instant): a mod
ships 116 patches across 116 different game_files. The very first
file processed has all 6 of its patches mismatching (the .paseq's
string content shifted between game versions). The other 115 files
might be fine, but ``import_json_as_entr`` aborts the entire mod
on the first all-mismatch file (json_patch_handler.py:1737-1745),
so the user gets "rejected" instead of "115/116 applied, 1 file
skipped".

Fix design: when one file in a multi-file mod has applied=0 and
mismatched>0, log a warning, add it to a per-mod ``skipped_files``
list, and ``continue`` to the next file. After the loop, the
return value carries ``skipped_files`` so the GUI can surface it.
The whole-mod ``version_mismatch`` rejection only fires when EVERY
file failed (changed_files is empty AND skipped_files is non-empty).

Single-file mods are unaffected: if the only file fails, the mod
still rejects with the same version_mismatch message as before.
"""
from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path

import pytest


def _make_synthetic_paz_entry(tmp_path: Path, dir_num: str, content: bytes,
                               entry_path: str) -> tuple[Path, Path]:
    """Build a minimal PAZ + PAMT pair containing one entry. Returns
    (paz_path, pamt_path)."""
    paz_dir = tmp_path / dir_num
    paz_dir.mkdir(exist_ok=True)
    paz_path = paz_dir / "0.paz"
    pamt_path = paz_dir / "0.pamt"

    # Write content uncompressed (comp_type=0)
    paz_path.write_bytes(content)

    # PAMT: very minimal header + 1 entry. Real format has more
    # fields, but parse_pamt walks generously. We use the existing
    # parse_pamt round-trip: reading mod 1555's PAMT and surgically
    # patching one entry would be safer than synthesizing from
    # scratch — but for THIS test we don't need the PAMT to work
    # via parse_pamt, we use the higher-level test fixtures.
    pamt_path.write_bytes(b"")
    return paz_path, pamt_path


def test_multi_file_mod_skips_one_failed_file_keeps_others():
    """A 2-file mod where file A applies cleanly and file B all-fails
    must apply file A and skip file B (not reject the entire mod)."""
    # Use existing live game data — pick two real files. File A:
    # iteminfo.pabgb (we know its bytes, can craft a matching patch).
    # File B: anything else where we craft a deliberately-mismatching
    # patch.
    from cdumm.engine.json_patch_handler import import_json_as_entr
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.storage.database import Database
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.engine.json_patch_handler import _extract_from_paz

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip(f"Live game dir not available")

    pamt = game_dir / '0008' / '0.pamt'
    entries = parse_pamt(str(pamt), paz_dir=str(pamt.parent))
    iteminfo = next(e for e in entries
                    if e.path == 'gamedata/iteminfo.pabgb')
    iteminfo_bytes = bytes(_extract_from_paz(iteminfo))
    # Pick any 4-byte window we can match exactly.
    file_a_offset = 100
    file_a_orig = iteminfo_bytes[file_a_offset:file_a_offset+4].hex()
    # File B: same iteminfo file, but use a deliberately-wrong
    # `original` so all patches fail. We use a SECOND patch group
    # for the same game_file to keep the test data simple — that's
    # not how a real mod would look, but it exercises the same
    # multi-patch-group iteration.
    # Actually use a different game_file: vehicleinfo.pabgb in 0008.
    vehicleinfo = next((e for e in entries
                        if e.path == 'gamedata/vehicleinfo.pabgb'), None)
    if vehicleinfo is None:
        pytest.skip("vehicleinfo.pabgb not in 0008/")

    patch_data = {
        "modinfo": {"title": "TestMultiFile", "version": "1.0"},
        "patches": [
            {
                "game_file": "gamedata/iteminfo.pabgb",
                "changes": [
                    {"offset": file_a_offset, "original": file_a_orig,
                     "patched": "ffffffff",
                     "label": "file_A_will_apply"},
                ],
            },
            {
                "game_file": "gamedata/vehicleinfo.pabgb",
                "changes": [
                    {"offset": 0, "original": "deadbeef",
                     "patched": "00000000",
                     "label": "file_B_will_fail"},
                    {"offset": 100, "original": "deadbeef",
                     "patched": "00000000",
                     "label": "file_B_also_fails"},
                ],
            },
        ],
    }

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db = Database(td / "test.db")
        db.initialize()
        deltas = td / "deltas"
        deltas.mkdir()

        result = import_json_as_entr(
            patch_data, game_dir, db, deltas, "TestMultiFile")
        db.connection.close()

    assert result is not None, "import returned None"
    # The whole-mod version_mismatch flag must NOT be set when at
    # least one file applied successfully.
    assert not result.get("version_mismatch"), (
        f"Whole-mod rejection fired even though file A applied. "
        f"result={result}")
    # File A must be in changed_files. changed_files contains dicts
    # with `entry_path` describing which file was modified.
    changed = result.get("changed_files") or []
    assert any("iteminfo" in (cf.get("entry_path", "") if isinstance(cf, dict) else cf)
               for cf in changed), (
        f"File A (iteminfo.pabgb) should have applied. changed_files={changed}")
    # File B must be reported as skipped — the GUI uses this list
    # to surface the warning.
    skipped = result.get("skipped_files") or []
    assert any("vehicleinfo" in sf.get("game_file", "")
               for sf in skipped), (
        f"File B (vehicleinfo.pabgb) should be in skipped_files. "
        f"skipped={skipped}")


def test_single_file_mod_all_fail_still_rejects():
    """Regression guard: a 1-file mod where the only file fails ALL
    patches must still reject with version_mismatch (existing
    behavior preserved)."""
    from cdumm.engine.json_patch_handler import import_json_as_entr
    from cdumm.storage.database import Database

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip(f"Live game dir not available")

    patch_data = {
        "modinfo": {"title": "TestSingleFileFail", "version": "1.0"},
        "patches": [
            {
                "game_file": "gamedata/iteminfo.pabgb",
                "changes": [
                    {"offset": 0,
                     "original": "deadbeefcafebabedeadbeefcafebabe",
                     "patched": "00000000000000000000000000000000",
                     "label": "fails"},
                    {"offset": 4, "original": "deadbeef",
                     "patched": "00000000", "label": "fails"},
                ],
            },
        ],
    }

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db = Database(td / "test.db")
        db.initialize()
        deltas = td / "deltas"
        deltas.mkdir()

        result = import_json_as_entr(
            patch_data, game_dir, db, deltas, "TestSingleFileFail")
        db.connection.close()

    # iteminfo.pabgb is a data table, so this hits the strict
    # _should_reject_partial_pabgb path AND the all-fail path.
    # Either way: version_mismatch must be True for a 1-file mod
    # where the only file fails.
    assert result is not None
    assert result.get("version_mismatch") is True, (
        f"Single-file mod where the only file fails must still "
        f"reject with version_mismatch. result={result}")


def test_multi_file_mod_all_files_fail_rejects_whole_mod():
    """If EVERY file in a multi-file mod fails, the mod is genuinely
    incompatible — version_mismatch should still fire."""
    from cdumm.engine.json_patch_handler import import_json_as_entr
    from cdumm.storage.database import Database

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip(f"Live game dir not available")

    # Two non-data-table targets where all patches will fail.
    # Use .paseq files to avoid triggering the strict-pabgb path.
    patch_data = {
        "modinfo": {"title": "TestAllFilesFail", "version": "1.0"},
        "patches": [
            {
                "game_file": "sequencer/cd_seq_basecamp_domestic.paseq",
                "changes": [
                    {"offset": 0,
                     "original": "deadbeefcafebabedeadbeefcafebabe",
                     "patched": "00000000000000000000000000000000",
                     "label": "fails"},
                ],
            },
            {
                "game_file": "sequencer/gimmick_craft_repair_01.paseq",
                "changes": [
                    {"offset": 0,
                     "original": "deadbeefcafebabedeadbeefcafebabe",
                     "patched": "00000000000000000000000000000000",
                     "label": "fails"},
                ],
            },
        ],
    }

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db = Database(td / "test.db")
        db.initialize()
        deltas = td / "deltas"
        deltas.mkdir()

        result = import_json_as_entr(
            patch_data, game_dir, db, deltas, "TestAllFilesFail")
        db.connection.close()

    assert result is not None
    assert result.get("version_mismatch") is True, (
        f"All-files-fail mod must reject. result={result}")
