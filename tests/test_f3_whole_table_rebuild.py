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

from tests.fixture_loaders import has_vanilla110, load_vanilla110


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
    touch: the low byte of the SECOND record's max_stack_count.

    A VALUE byte, deliberately. Flipping a byte inside the record's
    NAME breaks the parser's record-sniffing (the name-charset
    check), the identity round-trip then comes back lossy, and the
    writer's pre-flight refuses the rebuild, which is the correct,
    safe behavior for that case (pinned in
    test_name_corruption_refuses_rebuild_safely below). Real-world
    contamination (another mod's edits composed into the buffer, a
    tainted vanilla backup) flips values."""
    import struct
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    first = parse_first_record_size(vanilla)
    # second record: u32 key + u32 strlen + name + u8 is_blocked +
    # u64 max_stack_count; flip the max_stack LOW byte (stays well
    # under the sniffing heuristic's 1e8 ceiling).
    strlen = struct.unpack_from("<I", vanilla, first + 4)[0]
    return first + 8 + strlen + 1


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"), reason="1.10 iteminfo fixture absent")
def test_contaminated_buffer_rebuilds_instead_of_skipping():
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes

    vanilla = load_vanilla110("iteminfo.pabgb")
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


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"), reason="1.10 iteminfo fixture absent")
def test_name_corruption_refuses_rebuild_safely():
    """Contamination inside a record NAME breaks the sniff-walk
    framing, so the rebuild's identity round-trip is lossy. The
    pre-flight must REFUSE (skip with a readable reason) instead of
    emitting a quietly-wrong table, and the buffer must be left
    untouched. Before the pre-flight existed, the rebuild emitted a
    table 2 bytes off vanilla in this scenario, and the old test
    only asserted self-consistency, so it never noticed."""
    import struct
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    vanilla = load_vanilla110("iteminfo.pabgb")
    change = _build_change(vanilla)

    first = parse_first_record_size(vanilla)
    name_spot = first + 8 + 2  # inside the second record's name
    contaminated = bytearray(vanilla)
    contaminated[name_spot] ^= 0xFF

    data = bytearray(contaminated)
    skipped: list[dict] = []
    applied, mismatched, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 0, (
        "rebuild emitted bytes from a lossy parse instead of refusing")
    assert skipped, "refusal must surface as a recorded skip"
    assert bytes(data) == bytes(contaminated), (
        "refusal must leave the buffer untouched")


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgh"), reason="1.10 iteminfo fixtures absent")
def test_name_corruption_refuses_even_with_header():
    """Name corruption is inherently un-round-trippable: the parser
    decodes record names to str, an invalid-UTF-8 byte becomes U+FFFD
    and re-encodes 3 bytes wide ('Arrow' -> 'Ar?ow', +2 bytes), so
    the identity pre-flight refuses regardless of index framing. The
    header in _f3_rebuild does NOT change this outcome; its value is
    pinned in test_header_lets_rebuild_reach_swallowed_records."""
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    vanilla = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    change = _build_change(vanilla)
    change["_f3_rebuild"]["header"] = header.hex()

    first = parse_first_record_size(vanilla)
    name_spot = first + 8 + 2  # inside the second record's name
    contaminated = bytearray(vanilla)
    contaminated[name_spot] ^= 0xFF

    data = bytearray(contaminated)
    skipped: list[dict] = []
    applied, _, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 0, (
        "rebuild emitted bytes from a lossy parse instead of refusing")
    assert bytes(data) == bytes(contaminated), (
        "refusal must leave the buffer untouched")


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgh"), reason="1.10 iteminfo fixtures absent")
def test_header_lets_rebuild_reach_swallowed_records():
    """What the _f3_rebuild header actually buys (release-review
    finding 3): index-framed parsing during the live rebuild. The
    sniff walk swallows Delesyian_Flag (key 254M, above the sniff
    heuristic's key ceiling) into the previous record's tail, so a
    header-less rebuild cannot apply intents to it; with the header
    the record is framed exactly and the intent lands."""
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.semantic.parser import parse_pabgh_index

    vanilla = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    _, idx = parse_pabgh_index(header, "iteminfo")
    offs = list(idx.values())

    intents = [_Intent(entry="Delesyian_Flag", key=254143257,
                       field="max_stack_count", op="set", new=77)]
    change = build_iteminfo_intent_change(
        vanilla, intents, vanilla_header=header)
    assert change is not None, (
        "import-time build with header failed on the swallowed record")
    change.pop("_pabgh_companion", None)
    change["_f3_rebuild"] = {
        "table": "iteminfo",
        "intents": [{"entry": i.entry, "key": i.key, "field": i.field,
                     "op": i.op, "new": i.new, "old": i.old}
                    for i in intents],
        "header": header.hex(),
    }
    change.setdefault("offset", 0)

    # Force the rebuild path with a VALUE contamination elsewhere.
    import struct
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    first = parse_first_record_size(vanilla)
    strlen = struct.unpack_from("<I", vanilla, first + 4)[0]
    spot = first + 8 + strlen + 1  # rec2 max_stack low byte
    contaminated = bytearray(vanilla)
    contaminated[spot] ^= 0xFF

    data = bytearray(contaminated)
    skipped: list[dict] = []
    applied, _, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 1, (
        f"header-carrying rebuild did not fire: "
        f"{[(s.get('label'), s.get('reason')) for s in skipped]}")
    items = parse_iteminfo_from_bytes(
        bytes(data), record_offsets=offs)
    flag = next(i for i in items if i["key"] == 254143257)
    assert flag["max_stack_count"] == 77, (
        "intent on the sniff-swallowed record did not land")


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"), reason="1.10 iteminfo fixture absent")
def test_clean_buffer_applies_without_rebuild():
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes

    vanilla = load_vanilla110("iteminfo.pabgb")
    change = _build_change(vanilla)
    data = bytearray(vanilla)
    applied, mismatched, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=[])
    assert applied == 1 and mismatched == 0
    items = parse_iteminfo_from_bytes(bytes(data))
    assert next(i for i in items if i["key"] == 2200)[
        "max_stack_count"] == 4321
