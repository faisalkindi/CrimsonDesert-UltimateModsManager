"""End-to-end: feed kori228's 695-intent dropsetinfo mod through the
expander + byte patcher, verify all 695 changes apply with no
mismatches.

This is the integration test that proves the dropset_writer module,
the format3_apply expander, and the existing _apply_byte_patches
cumulative-shift cascade work together for variable-length list
rewriting.
"""
from __future__ import annotations
import json
import struct
from pathlib import Path

import pytest


_VANILLA_PABGB = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgh")
_FIXTURE = Path(__file__).parent / "fixtures" / "format3" / "dropsetinfo_5x_drops.json"


def _have_inputs() -> bool:
    return (_VANILLA_PABGB.exists() and _VANILLA_PABGH.exists()
            and _FIXTURE.exists())


@pytest.mark.skipif(not _have_inputs(),
                    reason="vanilla extract or fixture not present")
def test_kori228_mod_695_intents_all_apply():
    """Run the full pipeline on kori228's 695-intent dropsetinfo mod:
    parse, validate (all should be supported), expand to v2 changes,
    apply via _apply_byte_patches, verify zero mismatches."""
    from cdumm.engine.format3_handler import (
        parse_format3_mod, validate_intents,
    )
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.json_patch_handler import (
        _apply_byte_patches, _build_name_offsets_for_v2,
    )

    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    target, intents = parse_format3_mod(_FIXTURE)
    assert target == "dropsetinfo.pabgb"
    assert len(intents) == 695

    validation = validate_intents(target, intents)
    assert len(validation.supported) == 695, (
        f"All 695 intents must validate as supported, got "
        f"{len(validation.supported)} / {len(validation.skipped)} skipped")

    changes = _intents_to_v2_changes(
        target, pabgb, pabgh, validation.supported)
    assert len(changes) == 695, (
        f"Expander must emit 695 changes, got {len(changes)}")

    # Build the name-offsets map the way the live apply path does.
    name_offsets = _build_name_offsets_for_v2(target, pabgb, pabgh)
    assert name_offsets is not None and len(name_offsets) > 0

    modified = bytearray(pabgb)
    applied, mismatched, relocated = _apply_byte_patches(
        modified, changes, signature=None, vanilla_data=pabgb,
        name_offsets=name_offsets)
    assert mismatched == 0, (
        f"Expected 0 mismatches, got {mismatched} (applied={applied})")
    assert applied == 695, (
        f"Expected 695 patches applied, got {applied}")
    # The output must differ from vanilla (we changed 695 records).
    assert bytes(modified) != pabgb, (
        "modified buffer is byte-equal to vanilla, no changes landed")
