"""v2 byte-offset mod -> Format 3 field-name mod (GitHub #191).

These run against the committed CD 1.13 iteminfo fixture -- the real
table, 6,508 records -- not a hand-made one. The decisive assertion in
every test is the same one the converter enforces on itself: applying the
CONVERTED mod must produce byte-for-byte what the ORIGINAL v2 mod
produced. A conversion that can't reproduce the mod isn't a conversion.
"""
import json

import pytest

from cdumm.engine.iteminfo_native_parser import (
    detect_iteminfo_layout, parse_iteminfo_from_bytes,
)
from cdumm.engine.v2_to_format3 import (
    ConversionRefused, _apply_v2, _field_spans, convert_iteminfo,
    is_convertible, verify, write_format3,
)
from cdumm.semantic.parser import parse_pabgh_index
from tests.fixture_loaders import has_vanilla113, load_vanilla113

#: `slow`: every test here parses and re-serializes the whole 6,508-record
#: item table (11m42s for this file locally, and the marker's own docs warn
#: 150-530s per test on CI hardware). The fast per-PR job has a 22-minute
#: cap it has blown before -- which is exactly why this marker exists.
#: Caught in QC: these were unmarked, and would have run in the fast job.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (has_vanilla113("iteminfo.pabgb")
             and has_vanilla113("iteminfo.pabgh")),
        reason="CD 1.13 iteminfo fixture not present"),
]


@pytest.fixture(scope="module")
def table():
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    layout = detect_iteminfo_layout(body, starts)
    items = parse_iteminfo_from_bytes(
        body, record_offsets=starts, fields=layout)
    key_at = {off: k for k, off in offsets.items()}
    by_key = {it["key"]: it for it in items}
    return body, header, starts, layout, key_at, by_key


def _pick(table, want_kind, want_field=None):
    """First record with a scalar field we can safely rewrite.

    Skips ``key``: that's the record's own ID, not an editable stat, and
    aiming at it is what a broken test does (this one did, first time
    round -- the writer refused it, which is the correct answer).
    """
    body, _, starts, layout, key_at, by_key = table
    for start in starts[:400]:
        key = key_at[start]
        rec = by_key[key]
        if rec.get("_opaque_record"):
            continue
        spans = _field_spans(rec, layout, start)
        for fname, (a, b, kind) in spans.items():
            if kind != want_kind or fname == "key":
                continue
            if want_field and fname != want_field:
                continue
            return key, fname, a, b, kind
    pytest.skip(f"no {want_kind} field found in the fixture")


def _change(body, a, b, new_bytes, at=None):
    off = a if at is None else at
    n = len(new_bytes)
    return {"offset": off, "original": body[off:off + n].hex(),
            "patched": new_bytes.hex()}


def test_converts_a_whole_field_write(table):
    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u16")
    ch = _change(body, a, b, (12345).to_bytes(2, "little"))

    rep = convert_iteminfo([ch], body, header)

    assert rep.converted == 1
    assert not rep.unconverted
    assert rep.intents == [{
        "entry": "", "key": key, "field": fname, "op": "set", "new": 12345,
    }]
    assert verify(rep, [ch], body, header) is True


def test_a_partial_byte_write_carries_the_whole_field_value(table):
    """The cooldown mods patch two bytes of a four-byte field. The intent
    must carry the resulting VALUE, not the byte fragment -- otherwise the
    converted mod writes a different number."""
    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u32")

    # rewrite only the top two bytes of the u32
    ch = _change(body, a, b, b"\xff\xff", at=a + 2)
    expect = int.from_bytes(body[a:a + 2] + b"\xff\xff", "little")

    rep = convert_iteminfo([ch], body, header)

    assert rep.converted == 1
    assert rep.intents[0]["field"] == fname
    assert rep.intents[0]["new"] == expect
    assert verify(rep, [ch], body, header) is True


def test_two_writes_to_one_field_collapse_into_one_intent(table):
    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u32")
    lo = _change(body, a, b, b"\x01\x02", at=a)
    hi = _change(body, a, b, b"\x03\x04", at=a + 2)

    rep = convert_iteminfo([lo, hi], body, header)

    assert rep.converted == 1, "two edits to one field are one intent"
    assert rep.intents[0]["new"] == int.from_bytes(
        b"\x01\x02\x03\x04", "little")
    assert verify(rep, [lo, hi], body, header) is True


def test_a_stale_mod_is_refused_not_guessed(table):
    """The whole point. If the mod's expected bytes aren't there, its
    offsets mean nothing on this game version, and naming an item from
    them would name the WRONG item."""
    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u16")
    ch = {"offset": a,
          "original": (int.from_bytes(body[a:b], "little") ^ 0xFFFF)
          .to_bytes(2, "little").hex(),
          "patched": (1).to_bytes(2, "little").hex()}

    with pytest.raises(ConversionRefused) as e:
        convert_iteminfo([ch], body, header)

    msg = str(e.value)
    assert "different version" in msg
    assert "only be converted while it still works" in msg


def test_unconvertible_changes_are_reported_not_dropped(table):
    """A change CDUMM can't name must show up in the report -- and it must
    block verification, because a partial conversion cannot reproduce the
    original mod."""
    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "cstring")
    ch = _change(body, a, b, body[a:a + 1])       # no-op byte, inside a string

    rep = convert_iteminfo([ch], body, header)

    assert rep.converted == 0
    assert len(rep.unconverted) == 1
    why = rep.unconverted[0][1]
    assert "cstring" in why or "no single value" in why
    assert verify(rep, [ch], body, header) is False
    assert "NOT verified" in rep.summary()


def test_converted_mod_reproduces_the_original_byte_for_byte(table):
    """Two fields, several records: the converted mod and the v2 mod must
    land on identical bytes."""
    body, header, starts, layout, key_at, by_key = table
    changes = []
    seen = 0
    for start in starts[:300]:
        rec = by_key[key_at[start]]
        if rec.get("_opaque_record"):
            continue
        spans = _field_spans(rec, layout, start)
        for fname, (a, b, kind) in spans.items():
            if kind == "u16":
                changes.append(
                    _change(body, a, b, (999).to_bytes(2, "little")))
                seen += 1
                break
        if seen >= 5:
            break
    assert seen >= 2, "fixture should have several u16 fields"

    rep = convert_iteminfo(changes, body, header)
    assert rep.converted == len(changes)
    assert verify(rep, changes, body, header) is True

    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    change = build_iteminfo_intent_change(
        body,
        [Format3Intent(entry=i["entry"], key=i["key"], field=i["field"],
                       op=i["op"], new=i["new"], old=None)
         for i in rep.intents],
        vanilla_header=header)
    assert bytes.fromhex(change["patched"]) == _apply_v2(body, changes)


def test_written_file_reads_back_as_a_real_format3_mod(table, tmp_path):
    """The user keeps this file and re-imports it later. It has to be a
    mod CDUMM actually accepts -- so parse it back with the real loader."""
    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u16")
    ch = _change(body, a, b, (4242).to_bytes(2, "little"))
    rep = convert_iteminfo([ch], body, header)
    assert verify(rep, [ch], body, header) is True

    out = write_format3(rep, tmp_path / "converted.field.json",
                        "Infinite Durability", author="pinapana")

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["format"] == 3
    assert doc["target"] == "iteminfo.pabgb"
    assert doc["modinfo"]["title"] == "Infinite Durability"

    from cdumm.engine.format3_handler import parse_format3_mod_targets
    pairs = parse_format3_mod_targets(out)
    assert len(pairs) == 1
    target, intents = pairs[0]
    assert target == "iteminfo.pabgb"
    assert len(intents) == 1
    assert intents[0].key == key
    assert intents[0].new == 4242


def test_a_change_that_rewrites_the_item_id_is_refused(table):
    """Found by a test of mine that aimed at the wrong field. A Format 3
    intent addresses an item BY its key, so "set key" can never apply --
    the writer refused it. Say so in words instead of emitting an intent
    that is guaranteed to do nothing."""
    body, header, starts, layout, key_at, by_key = table
    start = starts[0]
    rec = by_key[key_at[start]]
    spans = _field_spans(rec, layout, start)
    a, b, _kind = spans["key"]
    ch = _change(body, a, b, (1234).to_bytes(b - a, "little"))

    rep = convert_iteminfo([ch], body, header)

    assert rep.converted == 0
    assert len(rep.unconverted) == 1
    assert "item's ID" in rep.unconverted[0][1]
    assert verify(rep, [ch], body, header) is False


def test_nothing_converted_writes_nothing(table, tmp_path):
    body, header, *_ = table
    rep = convert_iteminfo([], body, header)
    with pytest.raises(ConversionRefused):
        write_format3(rep, tmp_path / "empty.field.json", "empty")


# ── the entry point (what the GUI action actually calls) ────────────────

def _v2_mod(tmp_path, changes, game_file="gamedata/iteminfo.pabgb"):
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "format": 2,
        "modinfo": {"title": "Infinite Durability", "author": "pinapana"},
        "patches": [{"game_file": game_file, "changes": changes}],
    }), encoding="utf-8")
    return p


def test_is_convertible_offers_the_action_only_when_it_can_deliver(tmp_path):
    """An action that greys out or errors on half the mods is worse than no
    action, so the menu asks this first."""
    v2 = _v2_mod(tmp_path, [{"offset": 100, "original": "6400",
                             "patched": "ffff"}])
    assert is_convertible(v2) is True

    f3 = tmp_path / "already.field.json"
    f3.write_text(json.dumps({
        "format": 3, "target": "iteminfo.pabgb",
        "intents": [{"entry": "", "key": 1, "field": "price",
                     "op": "set", "new": 2}],
    }), encoding="utf-8")
    assert is_convertible(f3) is False, "already a field-name mod"

    other = _v2_mod(tmp_path, [{"offset": 1, "original": "00",
                                "patched": "01"}],
                    game_file="gamedata/skill.pabgb")
    assert is_convertible(other) is False, "table CDUMM can't convert yet"

    assert is_convertible(tmp_path / "nope.json") is False, "missing file"


def test_convert_mod_file_end_to_end(table, tmp_path, monkeypatch):
    """The seam the GUI worker calls: read a v2 mod, convert, verify, write.
    Nothing here touches Qt -- if the entry point needed a UI to be tested,
    it would be the wrong entry point."""
    import cdumm.engine.v2_to_format3 as mod

    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u16")
    v2 = _v2_mod(tmp_path, [_change(body, a, b, (777).to_bytes(2, "little"))])

    # the real loader needs a game install; the conversion itself doesn't
    monkeypatch.setattr(
        mod, "_load_vanilla_table",
        lambda _g, name: body if name.endswith(".pabgb") else header)

    out = tmp_path / "converted.field.json"
    rep = mod.convert_mod_file(v2, tmp_path, out, mod_name="Infinite Durability")

    assert rep.verified is True
    assert rep.converted == 1
    assert out.exists()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["format"] == 3
    assert doc["modinfo"]["author"] == "pinapana", "author is carried over"
    assert doc["intents"][0]["key"] == key
    assert doc["intents"][0]["field"] == fname
    assert doc["intents"][0]["new"] == 777


def test_convert_mod_file_writes_nothing_when_it_cannot_verify(
        table, tmp_path, monkeypatch):
    """A stale mod must produce NO file. A half-written .field.json that
    silently edits the wrong items is the whole nightmare."""
    import cdumm.engine.v2_to_format3 as mod

    body, header, *_ = table
    key, fname, a, b, kind = _pick(table, "u16")
    wrong = (int.from_bytes(body[a:b], "little") ^ 0xFFFF).to_bytes(2, "little")
    v2 = _v2_mod(tmp_path, [{"offset": a, "original": wrong.hex(),
                             "patched": "0100"}])

    monkeypatch.setattr(
        mod, "_load_vanilla_table",
        lambda _g, name: body if name.endswith(".pabgb") else header)

    out = tmp_path / "converted.field.json"
    with pytest.raises(ConversionRefused) as e:
        mod.convert_mod_file(v2, tmp_path, out)

    assert "different version" in str(e.value)
    assert not out.exists(), "a refused conversion must leave no file behind"
