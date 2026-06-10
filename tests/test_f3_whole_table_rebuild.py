"""falobos76's v3.3.19 retest on #191: the batched whole-table iteminfo
change (Stack 999,999 + 1 Socket On Accessories, 2,167 intents) was
skipped wholesale at apply with a byte mismatch, and the post-apply
warning dumped the full 5.5 MB expected hex.

Root cause: a whole-table change's ``original`` is the ENTIRE table
built from vanilla, so the strict compare fails if even one byte of
the apply buffer diverges from vanilla (contaminated vanilla backup,
or another mod's iteminfo edits already in the buffer). One stale
byte therefore dropped every Format 3 iteminfo mod at once, while
small v2 changes elsewhere kept applying, which is exactly the
confusing partial state he screenshotted.

Fix: the change carries its raw intents (``_f3_rebuild``); on
mismatch the apply loop re-runs the writer against the bytes actually
in the buffer, preserving whatever else is there and layering the
intents on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_V110 = (Path(__file__).resolve().parents[1]
         / "issue_repro" / "182" / "vanilla110" / "iteminfo.pabgb")


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str
    new: Any
    old: Any = None


def _build_change(vanilla: bytes):
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    intents = [_Intent(entry="Pyeonjeon_Arrow", key=2200,
                       field="max_stack_count", op="set", new=4321)]
    change = build_iteminfo_intent_change(vanilla, intents)
    assert change is not None
    change["_f3_rebuild"] = {
        "table": "iteminfo",
        "intents": [{"entry": i.entry, "key": i.key, "field": i.field,
                     "op": i.op, "new": i.new, "old": i.old}
                    for i in intents],
    }
    change.setdefault("offset", 0)
    return change


def _find_contamination_spot(vanilla: bytes) -> int:
    """A byte we can flip that belongs to an item the intent does not
    touch: inside the string_key of the SECOND record."""
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    first = parse_first_record_size(vanilla)
    # second record: u32 key + u32 strlen + name...; flip a name byte
    return first + 8 + 2


@pytest.mark.skipif(not _V110.exists(), reason="1.10 iteminfo fixture absent")
def test_contaminated_buffer_rebuilds_instead_of_skipping():
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes

    vanilla = _V110.read_bytes()
    change = _build_change(vanilla)

    spot = _find_contamination_spot(vanilla)
    contaminated = bytearray(vanilla)
    contaminated[spot] ^= 0xFF  # one stale byte, like a tainted backup

    data = bytearray(contaminated)
    skipped: list[dict] = []
    applied, mismatched, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 1, (
        f"rebuild did not fire: "
        f"skips={[(s.get('label'), s.get('reason')) for s in skipped]}")
    assert not skipped

    items = parse_iteminfo_from_bytes(bytes(data))
    target = next(i for i in items if i["key"] == 2200)
    assert target["max_stack_count"] == 4321, "intent value missing"
    # The rebuild's contract: the result is exactly what the writer
    # produces when run against the buffer's real bytes (so whatever
    # else lives in the buffer is carried through the rebuild, not
    # reset to vanilla).
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    expected = build_iteminfo_intent_change(
        bytes(contaminated),
        [_Intent(entry="Pyeonjeon_Arrow", key=2200,
                 field="max_stack_count", op="set", new=4321)])
    assert bytes(data) == bytes.fromhex(expected["patched"]), (
        "rebuilt bytes differ from writer-on-buffer output")


@pytest.mark.skipif(not _V110.exists(), reason="1.10 iteminfo fixture absent")
def test_clean_buffer_applies_without_rebuild():
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes

    vanilla = _V110.read_bytes()
    change = _build_change(vanilla)
    data = bytearray(vanilla)
    applied, mismatched, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=[])
    assert applied == 1 and mismatched == 0
    items = parse_iteminfo_from_bytes(bytes(data))
    assert next(i for i in items if i["key"] == 2200)[
        "max_stack_count"] == 4321
