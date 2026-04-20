"""HIGH #11: DB partial-state when import_json_as_entr raises mid-insert.

If import_json_as_entr inserts the `mods` row but raises before inserting
`mod_deltas`, the orphan mod row persists. The sibling loop's except
handler must look for mods with name=jp_name AND zero deltas and
clean them up.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from cdumm.engine.import_handler import _import_sibling_json_patches


def test_exception_in_entr_triggers_orphan_lookup(tmp_path: Path):
    sibling = tmp_path / "shop.json"
    sibling.write_text('{"patches":[], "name":"Shop"}')
    exclude_dir = tmp_path / "_excluded"
    exclude_dir.mkdir()
    deltas = tmp_path / "deltas"
    deltas.mkdir()

    # Mock DB execute to return an orphan row when queried.
    db = MagicMock()
    orphan_cursor = MagicMock()
    orphan_cursor.fetchall.return_value = [(42,)]   # orphan mod_id=42
    db.connection.execute.return_value = orphan_cursor

    detected = {
        "_json_path": sibling,
        "patches": [], "name": "Shop",
    }

    with patch(
        "cdumm.engine.json_patch_handler.detect_json_patches_all",
        return_value=[detected],
    ), patch(
        "cdumm.engine.json_patch_handler.import_json_as_entr",
        side_effect=RuntimeError("simulated crash mid-import"),
    ), patch(
        "cdumm.engine.mod_manager.ModManager"
    ) as mock_mm_cls:
        mock_mm = mock_mm_cls.return_value
        _import_sibling_json_patches(
            tmp_path, exclude_dir, tmp_path, db, deltas,
        )

    # orphan cleanup: remove_mod called with the id from the orphan scan
    assert mock_mm.remove_mod.called, (
        "exception path must look up and remove any orphan mod row")
    assert (42,) == mock_mm.remove_mod.call_args.args, (
        f"remove_mod wrong id: {mock_mm.remove_mod.call_args}")


def test_orphan_scan_scoped_to_post_watermark_only(tmp_path: Path):
    """C-H5: orphan cleanup must NOT touch pre-existing mods with the
    same name that happen to have zero deltas (legitimate in-progress
    imports, or unrelated mods). Only mods inserted AFTER the
    pre-import watermark should be eligible.
    """
    sibling = tmp_path / "Shop.json"
    sibling.write_text('{"patches":[], "name":"Shop"}')
    exclude_dir = tmp_path / "_excluded"
    exclude_dir.mkdir()
    deltas = tmp_path / "deltas"
    deltas.mkdir()

    db = MagicMock()
    # SELECT MAX(id) returns watermark=100. Orphan scan with id > 100
    # must find no rows (no orphan after import failure).
    max_cursor = MagicMock()
    max_cursor.fetchone.return_value = (100,)
    orphan_cursor = MagicMock()
    orphan_cursor.fetchall.return_value = []
    db.connection.execute.side_effect = [max_cursor, orphan_cursor]

    detected = {"_json_path": sibling, "patches": [], "name": "Shop"}

    with patch(
        "cdumm.engine.json_patch_handler.detect_json_patches_all",
        return_value=[detected],
    ), patch(
        "cdumm.engine.json_patch_handler.import_json_as_entr",
        side_effect=RuntimeError("crash mid-import"),
    ), patch(
        "cdumm.engine.mod_manager.ModManager"
    ) as mock_mm_cls:
        mock_mm = mock_mm_cls.return_value
        _import_sibling_json_patches(
            tmp_path, exclude_dir, tmp_path, db, deltas)

    assert not mock_mm.remove_mod.called, (
        "orphan scan must not delete mods when the post-watermark "
        "scan returns empty (no rows were inserted by the failed call)")
    # And the orphan query must have been parameterised with the
    # watermark — not just the name.
    calls = db.connection.execute.call_args_list
    orphan_call_sql = calls[1].args[0]
    orphan_call_params = calls[1].args[1]
    assert "id >" in orphan_call_sql, (
        f"orphan query must include 'id >' watermark filter; "
        f"got {orphan_call_sql}")
    assert 100 in orphan_call_params, (
        f"watermark must be passed to the query; got params {orphan_call_params}")
