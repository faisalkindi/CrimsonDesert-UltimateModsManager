"""Bug from Faisal 2026-06-08: updating a mod through CDUMM applies the
new file, but after closing and reopening CDUMM the mod is flagged as
needing an update again, every time.

Root cause (from his cdumm.log): the direct-update result handler wrote
nexus_real_file_id (the new numeric file_id) and nexus_file_id (the new
version string) but left the mods.version column at the OLD value. The
update check's self-correction then saw the stored file_id sitting on
the latest Nexus file WHILE mods.version disagreed with that file's
version, and because the old file still lingered on the Nexus page it
concluded the user must be on the old file and forced has_update=True.
So every launch re-flagged a mod that was actually current.

The fix keeps mods.version in sync with the file that was installed (the
same fix already applied to the multi-variant re-import path in
test_variant_reimport_updates_version). These tests pin the contract the
fix relies on at the update-check layer: when the stored file_id is the
latest AND the stored version matches that file, the mod is current even
if an older file is still on the page. The stale-version case is shown
to mis-fire, which is exactly the state the writer fix prevents.
"""
from __future__ import annotations

from unittest.mock import patch

from cdumm.engine.nexus_api import (
    check_mod_updates, NexusFileInfo, NexusFileUpdate,
)


def _files():
    """Two files on the page: the old 1.8 (still listed) and the new 2.1
    that supersedes it via the author's file_updates chain."""
    old = NexusFileInfo(file_id=10000, name="Better Radial Menus",
                        version="1.8", uploaded_timestamp=100,
                        file_name="brm-618-1-8-100.zip")
    new = NexusFileInfo(file_id=10495, name="Better Radial Menus",
                        version="2.1", uploaded_timestamp=200,
                        file_name="brm-618-2-1-200.zip")
    chain = [NexusFileUpdate(old_file_id=10000, new_file_id=10495,
                             uploaded_timestamp=200)]
    return [old, new], chain


def _check(mod_row):
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={618}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               return_value=_files()):
        updates, _checked, _now, _backfill = check_mod_updates(
            [mod_row], api_key="k")
    return updates


def test_in_sync_version_reads_as_current():
    """The post-fix state: a mod just updated to file 10495 (v2.1) with
    mods.version synced to '2.1'. Even though the old 1.8 file is still
    on the page, the mod must NOT be flagged for update."""
    mod_row = {
        "id": 1, "name": "Better Radial Menus", "nexus_mod_id": 618,
        "nexus_real_file_id": 10495, "version": "2.1",
        "nexus_last_checked_at": 0,
    }
    updates = _check(mod_row)
    outdated = [u for u in updates if u.has_update]
    assert not outdated, (
        "a mod sitting on the latest file with a matching version must "
        f"read as current, got has_update for: {[u.local_name for u in outdated]}")


def test_stale_version_misfires_documenting_the_writer_bug():
    """The bug state the writer fix prevents: same install, but
    mods.version was left at the OLD '1.8' after the update. The check
    mis-fires and re-flags the mod, which is why the writer must keep
    version in sync."""
    mod_row = {
        "id": 1, "name": "Better Radial Menus", "nexus_mod_id": 618,
        "nexus_real_file_id": 10495, "version": "1.8",
        "nexus_last_checked_at": 0,
    }
    updates = _check(mod_row)
    assert any(u.has_update for u in updates), (
        "with a stale version the check mis-fires (this is the bug); the "
        "writer fix keeps version in sync so this state never arises")
