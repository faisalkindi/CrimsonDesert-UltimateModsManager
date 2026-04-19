"""HIGH #5: _resolve_all_offsets must log when a change's offset cannot
be resolved or drops fields silently. Without logs, users see 'mismatched'
without knowing whether the mod JSON was malformed or the game drifted.
"""
from __future__ import annotations

import logging

from cdumm.engine.json_patch_handler import _apply_byte_patches


def test_unresolvable_change_logs_warning(caplog):
    """A change with neither record_key+offsets nor entry+offsets nor
    a resolvable literal `offset` must emit a warning naming the entry."""
    caplog.set_level(logging.WARNING, logger="cdumm.engine.json_patch_handler")

    data = bytearray(b"\xff" * 64)
    # entry name set but no name_offsets and no literal offset → unresolvable.
    changes = [{"entry": "MysteryRecord", "original": "aa", "patched": "bb"}]

    applied, mismatched, _r = _apply_byte_patches(data, changes)
    assert applied == 0
    assert mismatched == 1
    assert any("MysteryRecord" in rec.getMessage()
               for rec in caplog.records), (
        f"expected warning mentioning entry 'MysteryRecord', got: "
        f"{[r.getMessage() for r in caplog.records]}")


def test_bad_record_key_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="cdumm.engine.json_patch_handler")
    data = bytearray(b"\xff" * 64)
    changes = [
        {
            "record_key": "not-an-int",
            "relative_offset": 0,
            "original": "aa",
            "patched": "bb",
        }
    ]
    _apply_byte_patches(data, changes, record_offsets={1: 0})
    # Either warning or debug log mentioning the bad key should be present.
    relevant = [r for r in caplog.records
                if "record_key" in r.getMessage().lower()
                or "not-an-int" in r.getMessage()]
    assert relevant, (
        f"no log entry for bad record_key conversion; "
        f"captured: {[r.getMessage() for r in caplog.records]}")


def test_entry_not_in_name_offsets_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="cdumm.engine.json_patch_handler")
    data = bytearray(b"\xff" * 64)
    changes = [
        {
            "entry": "Missing",
            "rel_offset": 0,
            "original": "aa",
            "patched": "bb",
        }
    ]
    _apply_byte_patches(data, changes, name_offsets={"Present": 0})
    relevant = [r for r in caplog.records
                if "Missing" in r.getMessage()
                or "name_offsets" in r.getMessage()
                or "entry" in r.getMessage().lower()]
    assert relevant
