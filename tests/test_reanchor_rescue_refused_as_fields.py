"""#50: a refused byte-offset change is rescued as a field edit, not dropped.

falobos76 (GitHub #191) runs a Format 3 socket mod and a v2 "Infinite
Durability" mod that both edit the same armor. #296 re-anchors the durability
offsets that merely MOVED, but REFUSES the ones whose record the socket rebuild
rewrote -- a byte offset can't say which item it meant once the surrounding
bytes are gone. On his real files that dropped 76 of 231 durability changes.

But the refused change's ``original`` still matches VANILLA, and CDUMM can name
the item and field there. #50 does exactly that: it converts each refused
offset to a field edit and folds it onto the rebuilt table, so the socket edit
and the durability edit both land.

This runs against the REAL 1.13 iteminfo table (6,508 records), because the
rescue's whole value is that it parses the player's actual records to name the
field -- a synthetic buffer would prove nothing. It picks the record and fields
at runtime rather than pinning them, so a fixture refresh can't silently make
it test the wrong bytes.
"""
from __future__ import annotations

import struct

import pytest

from tests.fixture_loaders import has_vanilla113, load_vanilla113

pytestmark = pytest.mark.slow

FIXTURE = "iteminfo.pabgb"


def _find_adjacent_scalar_pair(body: bytes, header: bytes):
    """(key, (G,ga,gb,gk), (F,fa,fb,fk)) for a real record where scalar G sits
    inside the 48-byte back-window of a later scalar F.

    Rewriting G there is precisely what makes an offset on F un-re-anchorable,
    so this is the exact shape #50 has to rescue. Uses the converter's own
    ``_field_spans``, so any pair it finds the converter can also name.
    """
    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout, parse_iteminfo_from_bytes,
    )
    from cdumm.engine.v2_to_format3 import _SCALARS, _decode, _field_spans
    from cdumm.semantic.parser import parse_pabgh_index

    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    layout = detect_iteminfo_layout(body, starts)
    items = parse_iteminfo_from_bytes(
        body, record_offsets=starts[:200], fields=layout)
    for it in items:
        if it.get("_opaque_record"):
            continue
        base = offsets[it["key"]]
        spans = sorted(_field_spans(it, layout, base).items(),
                       key=lambda kv: kv[1][0])
        for (gn, (ga, gb, gk)), (fn, (fa, fb, fk)) in zip(spans, spans[1:]):
            if "key" in (gn, fn) or gk not in _SCALARS or fk not in _SCALARS:
                continue
            if fa - ga > 48:                 # G must land in F's back-window
                continue
            if (_decode(gk, body[ga:gb]) is None
                    or _decode(fk, body[fa:fb]) is None):
                continue
            return it["key"], (gn, ga, gb, gk), (fn, fa, fb, fk)
    return None


@pytest.mark.skipif(not has_vanilla113(FIXTURE),
                    reason="1.13 iteminfo fixture not present")
def test_refused_offset_is_rescued_as_a_field_edit():
    from cdumm.engine.apply_engine import _reanchor_offsets_onto_rebuilds
    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout, parse_iteminfo_from_bytes,
    )
    from cdumm.engine.offset_reanchor import reanchor_changes
    from cdumm.engine.v2_to_format3 import _SCALARS, _decode
    from cdumm.semantic.parser import parse_pabgh_index

    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")

    pick = _find_adjacent_scalar_pair(body, header)
    assert pick is not None, "no adjacent scalar pair in the first 200 records"
    key, (gn, ga, gb, gk), (fn, fa, fb, fk) = pick

    # The "socket" mod: a Format 3 rebuild that rewrites field G. Built as a
    # same-width byte patch so the table stays valid and the same size -- the
    # socket rebuild's shape without paying for a second whole-table serialize.
    # `_f3_rebuild` carries the vanilla header, exactly as the whole-table
    # dispatch attaches it (format3_apply, the iteminfo flush).
    old_g = _decode(gk, body[ga:gb])
    new_g = 0 if old_g else 1
    rebuilt = bytearray(body)
    rebuilt[ga:gb] = struct.pack(_SCALARS[gk][0], new_g)
    whole = {
        "offset": 0, "original": body.hex(), "patched": bytes(rebuilt).hex(),
        "_f3_rebuild": {"table": "iteminfo", "intents": [],
                        "header": header.hex()},
    }

    # The "durability" mod: a v2 offset on field F of the SAME record. Its
    # bytes match vanilla, so it anchors -- but the socket rebuild changed G
    # one field earlier, so the re-anchor cannot place it.
    old_f = _decode(fk, body[fa:fb])
    new_f = 0 if old_f else 1
    v2 = {"offset": fa, "original": body[fa:fb].hex(),
          "patched": struct.pack(_SCALARS[fk][0], new_f).hex(),
          "_needs_reanchor": "Armor Sockets (F3)",
          "_source_mod_name": "Infinite Durability (v2)"}

    # (a) load-bearing: the re-anchor ALONE refuses this change. If it didn't,
    #     the rescue would be untested -- the offset would simply move.
    _kept, dropped = reanchor_changes([whole, v2])
    assert len(dropped) == 1, (
        "the re-anchor must refuse this offset, or the rescue proves nothing")

    # (b) the fix: the full path rescues it instead of refusing.
    aggregated = {"iteminfo.pabgb": [whole, v2]}
    synth: dict = {}
    _reanchor_offsets_onto_rebuilds(aggregated, synth)
    assert not synth.get("_refused_offset_mods"), (
        "the change must be rescued, not refused: "
        f"{synth.get('_refused_offset_mods')}")

    # (c) both edits landed and the table is still whole (same size; the fold
    #     guard refuses anything that would resize a record).
    whole_out = next(c for c in aggregated["iteminfo.pabgb"]
                     if c.get("offset") == 0)
    body_out = bytes.fromhex(whole_out["patched"])
    assert len(body_out) == len(body), "the fold must not resize the table"

    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    layout = detect_iteminfo_layout(body, starts)
    idx = starts.index(offsets[key])
    items = parse_iteminfo_from_bytes(
        body_out, record_offsets=starts[:idx + 2], fields=layout)
    rec = next(it for it in items if it["key"] == key)
    assert rec[gn] == new_g, "the socket (Format 3) edit must survive"
    assert rec[fn] == new_f, "the durability (v2) edit must be folded in"
