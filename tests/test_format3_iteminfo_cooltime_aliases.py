"""GitHub #135 (Better Unique Gears, Luxxbell): NattKh's Format 3.1
exporter writes a few iteminfo fields as a three-element a/b/c group
(cooltime.a/.b/.c, max_charged_useable_count.a/.b/.c). CDUMM's
iteminfo native parser flattens those same on-disk i64 slots into
three separate flat fields (cooltime + unk_post_cooltime_a +
unk_post_cooltime_b, and the max_charged equivalents).

Verified 2026-05-20 against a vanilla 1.07.00 iteminfo.pabgb dump:
all three slots hold an identical value per record, confirming they
are one logical a/b/c triplet. The parser rewrites the dotted alias
names to CDUMM's flat names at parse time so the existing flat-field
writer handles them.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.format3_handler import parse_format3_mod_targets


FIXTURE = Path(__file__).parent / "fixtures" / "format3" / \
    "iteminfo_cooltime_abc.json"


def test_cooltime_abc_aliases_rewritten_to_flat_fields():
    """cooltime.a/.b/.c become cooltime / unk_post_cooltime_a /
    unk_post_cooltime_b after parse."""
    pairs = parse_format3_mod_targets(FIXTURE)
    assert len(pairs) == 1
    target, intents = pairs[0]
    assert target == "iteminfo.pabgb"
    fields = [i.field for i in intents]
    # The three cooltime.* aliases:
    assert "cooltime" in fields
    assert "unk_post_cooltime_a" in fields
    assert "unk_post_cooltime_b" in fields
    # No dotted alias names should survive.
    assert "cooltime.a" not in fields
    assert "cooltime.b" not in fields
    assert "cooltime.c" not in fields


def test_max_charged_abc_aliases_rewritten_to_flat_fields():
    """max_charged_useable_count.a/.b/.c become the three flat
    max_charged fields after parse."""
    pairs = parse_format3_mod_targets(FIXTURE)
    _target, intents = pairs[0]
    fields = [i.field for i in intents]
    assert "max_charged_useable_count" in fields
    assert "unk_post_max_charged_a" in fields
    assert "unk_post_max_charged_b" in fields
    assert "max_charged_useable_count.a" not in fields
    assert "max_charged_useable_count.b" not in fields
    assert "max_charged_useable_count.c" not in fields


def test_aliased_intents_keep_their_new_value():
    """The rewrite only touches the field name; entry, key, op and
    new are preserved."""
    pairs = parse_format3_mod_targets(FIXTURE)
    _target, intents = pairs[0]
    cool = [i for i in intents if i.field in (
        "cooltime", "unk_post_cooltime_a", "unk_post_cooltime_b")]
    assert len(cool) == 3
    for intent in cool:
        assert intent.entry == "WeatherWeaver_Necklace"
        assert intent.key == 1001182
        assert intent.op == "set"
        assert intent.new == 180000


def test_alias_map_only_applies_to_iteminfo(tmp_path):
    """A cooltime.a intent on a non-iteminfo target is left alone —
    the alias map is iteminfo-specific."""
    import json
    doc = {
        "format": 3,
        "format_minor": 1,
        "modinfo": {"title": "non-iteminfo probe", "version": "1.0"},
        "targets": [{
            "file": "skill.pabgb",
            "intents": [{
                "entry": "X", "key": 1, "field": "cooltime.a",
                "op": "set", "new": 5,
            }],
        }],
    }
    p = tmp_path / "skill_cooltime.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    pairs = parse_format3_mod_targets(p)
    _target, intents = pairs[0]
    # skill.pabgb is not iteminfo, so the dotted name is untouched.
    assert intents[0].field == "cooltime.a"
