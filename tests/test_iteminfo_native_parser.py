"""CDUMM-native iteminfo parser, clean-room replacement for the
crimson_rs Rust extension's parse_iteminfo_from_bytes /
serialize_iteminfo functions.

The vendored crimson_rs.pyd parses the pre-1.0.4.1 game iteminfo
layout. After Pearl Abyss shipped a post-1.0.4.1 patch (visible in
Faisal's 2026-04-29 game update), each iteminfo record grew by 10
bytes and the .pyd parser misaligns with "CArray count 15386081
exceeds remaining bytes" on the first record.

We need our own parser that walks the current layout. Trust anchor:
parse + serialize on the live extracted iteminfo.pabgb must produce
byte-identical output. Without that, applying any list-of-dict
intent will corrupt the file.
"""
from __future__ import annotations


import pytest

from tests.fixture_loaders import vanilla113_file


_LIVE_BODY = vanilla113_file("iteminfo.pabgb")


def _have_live_fixture() -> bool:
    return _LIVE_BODY.exists()


def _layout_and_index():
    """The layout this fixture is actually in, plus its record offsets."""
    from cdumm.engine.iteminfo_native_parser import detect_iteminfo_layout
    from cdumm.semantic.parser import parse_pabgh_index

    body = _LIVE_BODY.read_bytes()
    header = _LIVE_BODY.with_suffix(".pabgh").read_bytes()
    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    fields = detect_iteminfo_layout(body, starts)
    assert fields is not None, "no iteminfo layout round-trips this fixture"
    return body, offsets, starts, fields


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="CD 1.13 iteminfo fixture not present",
)
def test_native_parser_first_record_size_matches_pabgh_index():
    """The first record's on-disk size must equal what the .pabgh index
    says (offset of record 1 minus record 0). Catches misalignment where
    the parser walks fewer or more bytes than the record really occupies.

    Was permanently skipped: it called the parser without a layout, so it
    could only ever pass against the module default -- a 1.11-era shape no
    committed fixture is in. The invariant is real, so the parser learned
    to take a layout rather than the test being deleted.
    """
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size

    body, offsets, starts, fields = _layout_and_index()
    expected = starts[1] - starts[0]

    actual = parse_first_record_size(body, fields=fields)
    assert actual == expected, (
        f"first record: parser walked {actual} bytes, "
        f"the .pabgh index says {expected}")


@pytest.mark.slow
@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="CD 1.13 iteminfo fixture not present",
)
def test_native_parser_walks_every_record_to_correct_boundary():
    """For EVERY record in the index, the parser's walked size must equal
    (next_offset - this_offset). One drift on one record and serializing
    corrupts the file.

    This is the strictest statement of the 1.13 decode being complete: a
    record whose fields we don't fully understand stops short of its
    boundary and the leftover is carried as opaque `_tail_slack`. Zero
    drift means zero opaque tail -- which is exactly what #285 was about
    (76-139 bytes per record silently carried through, undecoded, while a
    whole-table round-trip stayed byte-perfect and said nothing).
    """
    from cdumm.engine.iteminfo_native_parser import parse_record_at

    body, offsets, starts, fields = _layout_and_index()
    by_start = {off: k for k, off in offsets.items()}

    drifts: list[tuple[int, int, int]] = []
    for i, off in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        expected = end - off
        try:
            actual = parse_record_at(body, off, rec_end=end,
                                     fields=fields) - off
        except Exception:
            drifts.append((by_start[off], expected, -1))
            continue
        if actual != expected:
            drifts.append((by_start[off], expected, actual))

    assert not drifts, (
        f"{len(drifts)}/{len(starts)} records do not walk to their index "
        f"boundary (key, expected, walked): {drifts[:3]}")


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
def test_native_parser_round_trips_byte_identical():
    """The trust anchor: parse + serialize on live iteminfo bytes
    must produce identical output. Anything less means writing a
    Format 3 list intent through this parser will corrupt the
    iteminfo binary."""
    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout, parse_iteminfo_from_bytes, serialize_iteminfo,
    )
    from cdumm.semantic.parser import parse_pabgh_index

    body = _LIVE_BODY.read_bytes()
    header = _LIVE_BODY.with_suffix(".pabgh").read_bytes()
    _key_size, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())

    # Select the layout instead of assuming the module default. The default
    # only ever matched one maintainer's 1.11-era extract; against any real
    # committed table it desyncs (and, on a bad count, spins). This is the
    # same detect step every non-test caller already does.
    fields = detect_iteminfo_layout(body, starts)
    assert fields is not None, "no iteminfo layout round-trips this fixture"

    items = parse_iteminfo_from_bytes(body, starts, fields=fields)
    re_encoded = serialize_iteminfo(items, fields=fields)
    if re_encoded != body:
        n = min(len(re_encoded), len(body))
        i = 0
        while i < n and re_encoded[i] == body[i]:
            i += 1
        pytest.fail(
            f"round-trip diverged at byte {i} (0x{i:X}). "
            f"orig size={len(body)} new size={len(re_encoded)}.")
