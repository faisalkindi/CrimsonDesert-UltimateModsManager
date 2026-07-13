"""Regression guard for GitHub #254 (Fat Stacks — iteminfo max_stack_count).

Fat Stacks is a stackable-item mod carrying thousands of ``max_stack_count``
edits on ``iteminfo.pabgb``. On game 1.13 the whole-table apply used to spin
until the 180s watchdog killed it (Pearl Abyss relocated two record-tail
fields, so the parser misaligned) — reported as "cannot apply the mod".

Fixed by #252 (version-adaptive 1.13 iteminfo decoder) + #248 (fast-fail the
parser spin), both merged in v3.5.0. The full byte-exact apply is exercised by
``test_iteminfo_native_apply_e2e.py`` (gated on a live game fixture).

This test is the CI-runnable half: it pins that a *batch* of ``max_stack_count``
set-intents on iteminfo classifies as SUPPORTED (not skipped) — i.e. the path
Fat Stacks needs stays open, so a future change can't silently regress #254
back to "cannot apply".
"""
from __future__ import annotations

from cdumm.engine.format3_handler import Format3Intent, validate_intents


def test_batch_max_stack_count_intents_are_supported():
    intents = [
        Format3Intent(entry=f"Item{i}", key=i,
                      field="max_stack_count", op="set", new=9999)
        for i in range(1, 51)
    ]
    result = validate_intents("iteminfo.pabgb", intents)
    assert len(result.supported) == 50, (
        "all iteminfo max_stack_count set-intents must be supported "
        f"(#254); got {len(result.supported)} supported, "
        f"{len(result.skipped)} skipped")
    assert not result.skipped


def test_single_max_stack_count_intent_supported():
    result = validate_intents(
        "iteminfo.pabgb",
        [Format3Intent(entry="GoldBar", key=53,
                       field="max_stack_count", op="set", new=1_000_000)])
    assert len(result.supported) == 1 and not result.skipped
