"""A DMM Mod Builder preset mixes ops CDUMM supports (set) with op:"scale"
(which CDUMM does not apply yet). Scale carries a ``factor`` instead of a
``new``; the parser used to require ``new`` on every intent, so a single scale
intent raised and the WHOLE mod was dropped at import -- losing the supported
intents too (the real BrizMod behaviour: 0 of its ~26 changes applied).

The parser is contractually lenient on op (#66): unsupported ops are meant to
be skipped per-intent in validate_intents, not fail the import. These tests
lock that in for scale.
"""
from __future__ import annotations

import json

import pytest

from cdumm.engine.format3_handler import (
    parse_format3_mod_targets,
    validate_intents,
)


def _mod(tmp_path, file, intents):
    doc = {"format": 3, "format_minor": 1, "modinfo": {"title": "t"},
           "targets": [{"file": file, "intents": intents}]}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_parser_accepts_scale_intent(tmp_path):
    p = _mod(tmp_path, "dropsetinfo.pabgb", [
        {"field": "list[*].raw_40", "op": "scale", "factor": 100, "match": {}}])
    (target, intents), = parse_format3_mod_targets(p)
    assert len(intents) == 1
    it = intents[0]
    assert it.op == "scale" and it.factor == 100 and it.new is None


def test_scale_missing_factor_is_rejected(tmp_path):
    p = _mod(tmp_path, "dropsetinfo.pabgb", [
        {"field": "x", "op": "scale", "match": {}}])   # no factor, no new
    with pytest.raises(ValueError):
        parse_format3_mod_targets(p)


def test_mixed_mod_scale_skipped_but_set_still_applies(tmp_path):
    # The core regression: a supported set + an unsupported scale on the same
    # target. The mod must parse; validate keeps the set and skips the scale.
    p = _mod(tmp_path, "iteminfo.pabgb", [
        {"field": "max_stack_count", "match": {}, "new": 999, "op": "set"},
        {"field": "drop_rate", "op": "scale", "factor": 2, "match": {}}])
    (target, intents), = parse_format3_mod_targets(p)
    assert len(intents) == 2                       # both parsed, no crash

    v = validate_intents(target, intents)
    assert len(v.supported) == 1, v                # the set survives
    assert v.supported[0].op == "set"
    assert len(v.skipped) == 1                     # only the scale is dropped
    assert v.skipped[0][0].op == "scale"


def test_set_intent_still_requires_new(tmp_path):
    # The lenient scale path must not weaken the set-intent contract.
    p = _mod(tmp_path, "iteminfo.pabgb", [
        {"field": "max_stack_count", "match": {}, "op": "set"}])  # no new
    with pytest.raises(ValueError):
        parse_format3_mod_targets(p)
