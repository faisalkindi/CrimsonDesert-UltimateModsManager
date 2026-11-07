"""The pabgh count width is derived, not looked up by name.

A pabgh index is ``[count][count x (key, u32 offset)]``, and the count
prefix is 1, 2 or 4 bytes depending on the table. Which one it was came
from ``UINT_COUNT_TABLES``, a hand-maintained set of table names — so it
was simply wrong for any table nobody had added, and a table whose index
does not parse is invisible to every writer, silently.

``aimemoryinfo`` is exactly that: it needs a u32 count, is not on the
list, and could not be read at all.

The width is now derived from the header. Measured over a full current
install: 133 tables parse identically to before, 2 that could not be
read now can, and none disagree.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from cdumm.semantic.parser import (
    UINT_COUNT_TABLES,
    _pabgh_offsets_well_formed,
    _pabgh_tilings,
    parse_pabgh_index,
)
from cdumm.semantic.record_header import (
    KEY_FIRST,
    NAME_FIRST,
    read_header,
    table_header_order,
)

_FX = Path(__file__).resolve().parent / "fixtures"


def _index(count: int, entries: list[tuple[int, int]], *,
           count_size: int, key_size: int) -> bytes:
    """Build a pabgh index in a given shape."""
    out = bytearray(count.to_bytes(count_size, "little"))
    for key, off in entries:
        out += key.to_bytes(key_size, "little") + struct.pack("<I", off)
    return bytes(out)


# ── the shapes that exist in a real install ─────────────────────────────

@pytest.mark.parametrize(("count_size", "key_size", "n"), [
    (2, 2, 20),     # inventory
    (2, 4, 66),     # the commonest shape
    (4, 4, 3),      # aimemoryinfo — the one the name list missed
    (1, 1, 1),      # gamestartinfo
    (2, 1, 11),     # mercenarygroupinfo
    (4, 8, 12),     # characterappearanceindexinfo
    (2, 12, 5),     # aieventtableinfo's composite key
])
def test_every_real_shape_is_derived(count_size, key_size, n):
    entries = [(i + 1, i * 64) for i in range(n)]
    hdr = _index(n, entries, count_size=count_size, key_size=key_size)
    got_ks, got = parse_pabgh_index(hdr, "whatever")
    assert got_ks == key_size
    assert got == dict(entries)


def test_the_u32_count_shape_needs_no_name():
    """The bug, stated directly: a u32-count table that is NOT on the
    hand-maintained list must still parse."""
    n = 3
    entries = [(1_000_000 + i, i * 24) for i in range(n)]
    hdr = _index(n, entries, count_size=4, key_size=4)
    assert "aimemoryinfo" not in UINT_COUNT_TABLES
    key_size, offsets = parse_pabgh_index(hdr, "aimemoryinfo")
    assert (key_size, offsets) == (4, dict(entries))


# ── ambiguity and refusal ───────────────────────────────────────────────

def test_offsets_invariant_breaks_ties():
    """Some small headers tile two ways. Entry offsets start at 0 and
    strictly increase — verified to hold for every table whose tiling is
    unambiguous — so that picks the right one."""
    n = 3
    entries = [(1_000_000 + i, i * 24) for i in range(n)]
    hdr = _index(n, entries, count_size=4, key_size=4)
    tilings = _pabgh_tilings(hdr)
    assert len(tilings) > 1, "expected this header to be ambiguous"
    well = [t for t in tilings if _pabgh_offsets_well_formed(hdr, *t)]
    assert len(well) == 1
    assert well[0] == (4, n, 4)


def test_a_header_that_tiles_no_way_is_refused():
    """Refusing is the correct outcome — the alternative is handing every
    writer a garbage offset table."""
    assert parse_pabgh_index(b"\x05\x00\x01", "x") == (0, {})
    assert parse_pabgh_index(b"\xff" * 9, "x") == (0, {})


@pytest.mark.parametrize("hdr", [b"", b"\x01", b"\x00\x00"])
def test_degenerate_headers_are_quiet(hdr):
    """An empty table is valid, not an error."""
    assert parse_pabgh_index(hdr, "x") == (0, {})


# ── the fixtures themselves ─────────────────────────────────────────────

def _fixture_pairs():
    for d in sorted(_FX.glob("vanilla*")):
        for p in sorted(d.glob("*.pabgh.zlib")):
            yield d.name, p.name.replace(".pabgh.zlib", ""), p


@pytest.mark.parametrize(("ver", "name", "path"), list(_fixture_pairs()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_every_committed_fixture_index_parses_and_anchors(ver, name, path):
    """A guard that would have caught a real bug.

    ``vanilla113/inventory.pabgh`` was committed as ciphertext — 122
    bytes of noise that parsed to nothing. Its body was fine and the
    tests that used it happened to search the body by name, so nothing
    went red, and the fixture sat there wrong.

    Every committed index must parse AND agree with its own body: each
    entry's key has to be the key stored in the record at that entry's
    offset. Up to 2.03 a record opens with its key; on 2.04 the key
    follows the name (vanilla20408), so the key is read from the record
    header in the order the whole table uses.
    """
    header = zlib.decompress(path.read_bytes())
    body = zlib.decompress(
        path.with_name(f"{name}.pabgb.zlib").read_bytes())

    key_size, offsets = parse_pabgh_index(header, name)
    assert key_size, f"{ver}/{name}: index does not parse"
    assert offsets

    order = (NAME_FIRST if table_header_order(body, offsets, key_size)
             == NAME_FIRST else KEY_FIRST)
    assert (order == NAME_FIRST) == (ver == "vanilla20408"), (ver, name)
    mismatched = [
        (k, off) for k, off in offsets.items()
        if (order == KEY_FIRST
            and (off + key_size > len(body)
                 or int.from_bytes(body[off:off + key_size], "little") != k))
        or (order == NAME_FIRST
            and (read_header(body, off, key_size, NAME_FIRST) or (None,))[0]
            != k)
    ]
    assert not mismatched, (
        f"{ver}/{name}: {len(mismatched)} of {len(offsets)} entries do not "
        f"carry their own key at their own offset: {mismatched[:3]}")
