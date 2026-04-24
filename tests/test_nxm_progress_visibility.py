"""Bug #3: Silent nxm:// download.

Evidence: during the 2026-04-22 Bigger Minimap Tweaks click, CDUMM
downloaded + imported 12 files in ~2 seconds but Faisal reported seeing
no visible UI feedback. The InfoBar that ``_handle_nxm_url`` fires has
``duration=4000`` and only ``parent=self`` — on multi-monitor setups or
with other toasts competing for the same slot, it can be drawn offscreen
or immediately obscured. Worse, there's no record of the operation in
the Activity page either, so the user has no paper trail.

Fix: in addition to the InfoBar (kept for visibility), log an activity
entry when an nxm:// download is received and another when the import
completes. The Activity page is a reliable persistent record.

This test validates two pure helpers: ``_format_nxm_download_activity``
(for the download-started entry) and ``_format_nxm_import_activity``
(for completion).
"""
from __future__ import annotations

from cdumm.gui.fluent_window import (
    _format_nxm_download_activity,
    _format_nxm_import_activity,
)


def test_download_activity_includes_mod_and_file_id() -> None:
    msg = _format_nxm_download_activity(mod_id=275, file_id=4721)
    assert "275" in msg
    assert "4721" in msg
    assert msg.lower().startswith("downloading")


def test_import_activity_distinguishes_update_from_fresh() -> None:
    """When the download is an update-in-place (existing_mod_id was set),
    the activity message should say 'updated', not 'imported'."""
    update_msg = _format_nxm_import_activity(
        mod_name="Bigger Minimap Tweaks",
        is_update=True)
    assert "updated" in update_msg.lower()
    assert "Bigger Minimap Tweaks" in update_msg

    fresh_msg = _format_nxm_import_activity(
        mod_name="Some New Mod",
        is_update=False)
    assert "imported" in fresh_msg.lower()
    assert "Some New Mod" in fresh_msg


def test_import_activity_handles_missing_name() -> None:
    """Defensive: if the worker didn't return a mod name, don't crash."""
    msg = _format_nxm_import_activity(mod_name=None, is_update=False)
    assert msg  # non-empty
    assert "mod" in msg.lower()
