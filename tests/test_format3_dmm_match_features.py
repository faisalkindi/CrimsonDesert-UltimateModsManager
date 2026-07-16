"""DMM Mod Builder match features CDUMM now accepts:

  * ``"match": {}``  -> apply to EVERY record (DMM's "all items" selector,
    e.g. infinite durability on all items). Was rejected at parse.
  * ``"match": {"key": {"$in": [1, 4, 6]}}`` -> DMM's any-of operator on the
    record id. CDUMM's any-of previously needed a bare list and the field
    name ``_key``; DMM writes ``$in`` and ``key``.
"""
from __future__ import annotations

import json

import pytest

from cdumm.engine.format3_apply import _expand_match_intents, _match_value_equals
from cdumm.engine.format3_handler import (
    parse_format3_mod_targets,
    validate_intents,
)
from cdumm.semantic.parser import parse_pabgh_index

from tests.fixture_loaders import has_vanilla113, load_vanilla113

FIXTURE = "iteminfo.pabgb"


def _write_mod(tmp_path, intents):
    doc = {"format": 3, "format_minor": 1, "modinfo": {"title": "t"},
           "targets": [{"file": "iteminfo.pabgb", "intents": intents}]}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# ── fast unit: the $in operator ────────────────────────────────────────
def test_match_value_equals_in_operator():
    assert _match_value_equals(4, {"$in": [1, 4, 6]})
    assert not _match_value_equals(5, {"$in": [1, 4, 6]})
    assert _match_value_equals("x", {"$in": ["x", "y"]})
    assert _match_value_equals(5, {"$in": 5})          # scalar $in tolerated
    # a normal dict value is NOT treated as an operator
    assert not _match_value_equals(4, {"other": [4]})


def test_parser_accepts_empty_match(tmp_path):
    """The parse guard used to reject match: {} outright, killing the whole
    DMM preset on its first 'apply to all' tweak."""
    p = _write_mod(tmp_path, [
        {"field": "max_stack_count", "match": {}, "new": 999, "op": "set"}])
    pairs = parse_format3_mod_targets(p)
    assert pairs and len(pairs[0][1]) == 1
    assert pairs[0][1][0].match == {}


# ── real-table proofs ──────────────────────────────────────────────────
@pytest.mark.skipif(not has_vanilla113(FIXTURE),
                    reason="1.13 iteminfo fixture not present")
def test_match_all_expands_to_every_record(tmp_path):
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    _, offs = parse_pabgh_index(header, "iteminfo")
    n = len(offs)

    p = _write_mod(tmp_path, [
        {"field": "max_stack_count", "match": {}, "new": 999, "op": "set"}])
    target, intents = parse_format3_mod_targets(p)[0]
    v = validate_intents(target, intents)
    assert len(v.supported) == 1, v

    expanded = _expand_match_intents(target, body, header, v.supported)
    assert len(expanded) == n, (len(expanded), n)
    assert all(e.field == "max_stack_count" and e.new == 999
               and e.match is None for e in expanded)


@pytest.mark.skipif(not has_vanilla113(FIXTURE),
                    reason="1.13 iteminfo fixture not present")
def test_in_operator_on_key_selects_named_records(tmp_path):
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    _, offs = parse_pabgh_index(header, "iteminfo")
    keys = sorted(offs.keys())[:3]

    p = _write_mod(tmp_path, [
        {"field": "max_stack_count", "match": {"key": {"$in": keys}},
         "new": 999, "op": "set"}])
    target, intents = parse_format3_mod_targets(p)[0]
    v = validate_intents(target, intents)
    assert len(v.supported) == 1, v

    expanded = _expand_match_intents(target, body, header, v.supported)
    assert sorted(e.key for e in expanded) == keys
