"""HIGH #7: no-changed-files branch in _import_sibling_json_patches must roll back.

If a sibling JSON is detected as a patch but resolves to zero changes
(e.g. no-op because the patched bytes already equal current game bytes),
import_json_as_entr may have already inserted a mod row. Without a
rollback, the user gets an orphan sibling mod with zero deltas.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from cdumm.engine.import_handler import _import_sibling_json_patches


def test_no_changed_files_triggers_remove_mod(tmp_path: Path):
    sibling = tmp_path / "Some_Shop.json"
    sibling.write_text(
        '{"patches": [{"game_file": "x.pabgb", "changes": []}], "name": "Shop"}'
    )
    exclude_dir = tmp_path / "_nonexistent_"
    exclude_dir.mkdir()
    deltas = tmp_path / "deltas"
    deltas.mkdir()
    db = MagicMock()

    detected_jp = {
        "_json_path": sibling,
        "patches": [{"game_file": "x.pabgb", "changes": []}],
        "name": "Shop",
    }

    fake_result = {
        "mod_id": 999,
        "changed_files": [],       # triggers no-changed-files branch
        "version_mismatch": False,
    }

    with patch(
        "cdumm.engine.json_patch_handler.detect_json_patches_all",
        return_value=[detected_jp],
    ), patch(
        "cdumm.engine.json_patch_handler.import_json_as_entr",
        return_value=fake_result,
    ), patch(
        "cdumm.engine.mod_manager.ModManager"
    ) as mock_mm_cls:
        mock_mm = mock_mm_cls.return_value
        _import_sibling_json_patches(
            tmp_path, exclude_dir, tmp_path, db, deltas,
        )

    assert mock_mm.remove_mod.called, (
        "remove_mod must be called when sibling produces 0 changed_files")
    call_args = mock_mm.remove_mod.call_args
    assert call_args.args == (999,), (
        f"remove_mod called with wrong mod_id: {call_args}")
