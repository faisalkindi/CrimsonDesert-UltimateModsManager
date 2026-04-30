"""End-to-end Format 3 integration: a `field=drops, op=set` intent
on `dropsetinfo.pabgb` validates as supported AND expands into a
v2-style change dict that the apply pipeline can land.

This is the bridge from the dropset_writer module to the rest of
CDUMM. Without this, the writer is reachable from tests but not
from real mod imports.
"""
from __future__ import annotations
import struct
from pathlib import Path

import pytest


_VANILLA_PABGB = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgh")


def _have_vanilla() -> bool:
    return _VANILLA_PABGB.exists() and _VANILLA_PABGH.exists()


def test_validate_intents_accepts_drops_field_on_dropsetinfo():
    """The validator must classify `drops` on dropsetinfo as SUPPORTED
    instead of skipping it with a 'list-of-dicts coming later' message."""
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )

    intents = [
        Format3Intent(
            entry="DropSet_Faction_Graymane",
            key=175001,
            field="drops",
            op="set",
            new=[{"item_key": 30010, "rates": 1000000, "rates_100": 100,
                  "min_amt": 3, "max_amt": 2}],
        )
    ]
    validation = validate_intents("dropsetinfo.pabgb", intents)
    assert len(validation.supported) == 1, (
        f"Expected drops intent to be supported on dropsetinfo, "
        f"got skipped: {validation.skipped}")
    assert len(validation.skipped) == 0, validation.skipped


@pytest.mark.skipif(not _have_vanilla(), reason="vanilla extract not present")
def test_format3_expander_emits_drops_change():
    """`_intents_to_v2_changes` must emit a record-replacement change
    for a drops intent on dropsetinfo.pabgb."""
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.format3_apply import _intents_to_v2_changes

    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()

    intents = [
        Format3Intent(
            entry="DropSet_Faction_Graymane",
            key=175001,
            field="drops",
            op="set",
            new=[{"item_key": 30010, "rates": 1000000, "rates_100": 100,
                  "min_amt": 3, "max_amt": 2},
                 {"item_key": 103, "rates": 1000000, "rates_100": 100,
                  "min_amt": 2, "max_amt": 1}],
        )
    ]
    changes = _intents_to_v2_changes(
        "dropsetinfo.pabgb", pabgb, pabgh, intents)
    assert len(changes) == 1, (
        f"Expected 1 change for the drops intent, got {len(changes)}")
    c = changes[0]
    assert c["entry"] == "DropSet_Faction_Graymane"
    assert c["label"].endswith(".drops")
    # Patched bytes must differ from original (we changed the drops list)
    assert c["patched"] != c["original"]
