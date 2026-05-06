"""Bug found via /systematic-debugging on the just-shipped skip-tracking
chunks: persist_skip_summary builds the badge tooltip's per-entry
'file' field via ``s.get("_target_file", "")`` , but nothing in the
pipeline actually writes ``_target_file`` to the skip dict. The
default empty string always wins. The badge tooltip lists labels but
omits the file path, hiding which game asset (iteminfo.pabgb,
skill.pabgb, etc.) failed.

Fix: tag every change with ``_target_file = game_file`` at the same
aggregator layer that stamps ``_source_mod_id``. ``_record_skip``
already propagates any underscore-prefixed metadata from the change
into the skip entry, so once the change carries the tag, it flows
all the way to the badge tooltip.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_persist_skip_summary_records_file_field(tmp_path: Path):
    """Contract pin: when a skip entry already carries ``_target_file``,
    persist_skip_summary writes it into the JSON summary's 'file'
    key. Demonstrates that the column-side already does the right
    thing , the bug is upstream."""
    from cdumm.engine.apply_engine import persist_skip_summary
    from cdumm.storage.database import Database

    db = Database(tmp_path / "t.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority) "
        "VALUES (1, 'Test', 'paz', 1)")
    db.connection.commit()

    skips = [{
        "label": "stamina_swim",
        "reason": "byte mismatch",
        "_target_file": "skill.pabgb",
        "_source_mod_id": 1,
    }]
    persist_skip_summary(db.connection, skips, {1})

    import json as _json
    row = db.connection.execute(
        "SELECT last_apply_skip_summary FROM mods WHERE id = 1"
    ).fetchone()
    parsed = _json.loads(row[0])
    assert parsed[0]["file"] == "skill.pabgb"
    db.close()


def test_apply_byte_patches_skip_carries_target_file_via_change():
    """When a change dict carries ``_target_file`` (stamped at the
    aggregator), ``_record_skip`` must propagate it into the skip
    entry the same way it propagates ``_source_mod_id``. Without
    this propagation, the badge tooltip's file column stays empty."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 1024)
    changes = [{
        "label": "A1",
        "offset": 100,
        "original": "deadbeef",
        "patched": "cafebabe",
        "_source_mod_id": 42,
        "_target_file": "skill.pabgb",
    }]
    skipped: list[dict] = []

    _apply_byte_patches(
        data, changes, signature=None, skipped_out=skipped)

    assert skipped, "Expected a skip from byte mismatch on all-zero data"
    s = skipped[0]
    assert s.get("_target_file") == "skill.pabgb", (
        f"Skip entry must propagate _target_file from the change so "
        f"persist_skip_summary's 'file' lookup finds something. "
        f"Got {s!r}"
    )


def test_aggregator_stamps_target_file_on_every_change(tmp_path: Path):
    """Top-level pin: aggregate_json_mods_into_synthetic_patches must
    tag every emitted change with ``_target_file = game_file`` so the
    skip-attribution chain (change , _record_skip , persist_skip_summary
    , badge tooltip) is fully wired. Mirror of the existing
    _source_mod_id contract from chunk 1."""
    from cdumm.engine.apply_engine import (
        aggregate_json_mods_into_synthetic_patches,
    )
    from cdumm.storage.database import Database
    import json as _json

    db = Database(tmp_path / "t.db")
    db.initialize()

    patch_payload = {
        "modinfo": {"title": "Multi-target Mod"},
        "patches": [
            {"game_file": "skill.pabgb", "changes": [
                {"label": "S1", "offset": 0,
                 "original": "00", "patched": "01"},
            ]},
            {"game_file": "iteminfo.pabgb", "changes": [
                {"label": "I1", "offset": 0,
                 "original": "00", "patched": "01"},
                {"label": "I2", "offset": 1,
                 "original": "00", "patched": "01"},
            ]},
        ],
    }
    src = tmp_path / "mod.json"
    src.write_text(_json.dumps(patch_payload), encoding="utf-8")

    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "json_source) VALUES (1, 'M', 'paz', 1, 1, ?)", (str(src),))
    db.connection.commit()

    synth, summary = aggregate_json_mods_into_synthetic_patches(db)

    assert summary, "Aggregator should report participation for the mod"
    for patch in synth["patches"]:
        gf = patch["game_file"]
        for c in patch["changes"]:
            assert c.get("_target_file") == gf, (
                f"Aggregator must stamp _target_file={gf!r} on every "
                f"change going into synth_patch_data. Without this, "
                f"byte-mismatch skips arrive at persist_skip_summary "
                f"with no file attribution and the badge tooltip's "
                f"'file' column stays empty. Got: {c!r}"
            )
    db.close()
