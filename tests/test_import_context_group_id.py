"""#161: the user's folder group must ride through the import context so
the post-import restore can re-apply it after the old mod row is
dup-removed on update."""
from cdumm.gui.import_context import (
    IMPORT_CONTEXT_KEYS,
    snapshot_and_clear_import_context,
)


class _FakeWin:
    pass


def test_update_group_id_is_a_context_key() -> None:
    assert "update_group_id" in IMPORT_CONTEXT_KEYS


def test_snapshot_captures_and_clears_update_group_id() -> None:
    win = _FakeWin()
    win._update_priority = 5
    win._update_enabled = 1
    win._update_group_id = 7
    win._configurable_source = None
    win._configurable_labels = None
    win._variant_leaf_rel = None
    win._original_drop_path = None
    ctx = snapshot_and_clear_import_context(win)
    assert ctx["update_group_id"] == 7
    assert win._update_group_id is None
