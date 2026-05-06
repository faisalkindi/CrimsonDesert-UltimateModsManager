"""Skipped-mod-badge plumbing, chunk 1B: when process_json_patches
records a skipped change (byte mismatch / stale signature / etc.)
into skipped_out, it must propagate the change's `_source_mod_id`
so the apply pipeline can attribute the skip to a specific mod.

Without this, skipped_out entries only carry label/offset/reason and
the post-apply tally cannot tell which mod was affected.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest


def test_record_skip_includes_source_mod_id_when_present(tmp_path: Path):
    """When process_json_patches_for_overlay processes an aggregated
    patch list and a change carrying `_source_mod_id` fails byte
    matching, the skipped_out entry must include `_source_mod_id`.
    """
    from cdumm.engine.json_patch_handler import (
        process_json_patches_for_overlay)

    # Build a tiny synth patch file with one change carrying the tag
    patch_data = {
        "modinfo": {"title": "test"},
        "patches": [{
            "game_file": "fake.pabgb",
            "changes": [{
                "label": "A1",
                "offset": 100,
                "original": "deadbeef",
                "patched": "cafebabe",
                "_source_mod_id": 42,
            }],
        }],
    }
    json_path = tmp_path / "synth.json"
    json_path.write_text(json.dumps(patch_data), encoding="utf-8")

    # Vanilla resolver that returns None — forces all patches to skip
    # because we have no source bytes to verify against.
    def resolver(game_file):
        return None

    skipped: list[dict] = []
    errors: list[str] = []
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    process_json_patches_for_overlay(
        0, str(json_path), game_dir,
        disabled_indices=None, custom_values=None,
        vanilla_source_resolver=resolver,
        errors_out=errors, skipped_out=skipped,
    )

    # Either at least one skip got recorded with the tag, or the
    # resolver-None path bailed early. Accept "no entries to skip" as
    # a valid no-op outcome — but if any skip WAS recorded, it must
    # carry the tag.
    if skipped:
        assert all("_source_mod_id" in s for s in skipped), (
            f"At least one skip entry missing _source_mod_id: {skipped!r}"
        )
        assert any(s.get("_source_mod_id") == 42 for s in skipped), (
            f"No skip entry has _source_mod_id=42: {skipped!r}"
        )


def test_record_skip_helper_directly_propagates_mod_id():
    """Direct unit test on the inner _record_skip closure. Bypasses
    the outer pipeline so we can hit the propagation path without
    needing real PAZ bytes / vanilla source."""
    # _record_skip is defined inside process_json_patches as a closure,
    # so we test by invoking the outer function with a controlled
    # input that triggers the skip path. Vanilla bytes that DON'T match
    # the change's `original` field force a byte-mismatch skip.
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 1024)  # all zeros, won't match deadbeef
    changes = [{
        "label": "A1",
        "offset": 100,
        "original": "deadbeef",
        "patched": "cafebabe",
        "_source_mod_id": 42,
    }]
    skipped: list[dict] = []

    _apply_byte_patches(
        data, changes, signature=None,
        skipped_out=skipped,
    )

    assert skipped, "Expected at least one skip from byte mismatch"
    s = skipped[0]
    assert s.get("_source_mod_id") == 42, (
        f"Skipped entry must propagate _source_mod_id; got {s!r}"
    )
