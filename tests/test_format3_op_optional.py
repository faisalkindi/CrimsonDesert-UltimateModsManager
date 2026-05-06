"""GitHub #66 (deadriver35): the skill_inf_stamina_mod .field.json
schema OMITS the 'op' key. CDUMM's parser was raising 'intent #0
is missing required key op' on import, so the user couldn't even
get past the import dialog.

The newer skill .field.json files use:
  entry, key, field, old, new  (NO 'op')

The older DropSets variant uses:
  entry, key, field, op, new   (HAS 'op')

Both should parse. Default the missing 'op' to 'set' (the only op
CDUMM's apply path supports anyway). Validation downstream still
rejects unsupported fields, so this just lets the import succeed
and surfaces the real reason via the 'X intent(s) skipped: ...'
summary message instead of failing import outright.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdumm.engine.format3_handler import parse_format3_mod


def _write(p: Path, body: dict) -> Path:
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_intent_without_op_parses_with_default_set(tmp_path):
    """The skill_inf_stamina_mod variant has no 'op'. Parser must
    accept it and default op='set' so import succeeds and the
    validator can report any unsupported-field skips."""
    p = _write(tmp_path / "mod.field.json", {
        "format": 3,
        "target": "skill.pabgb",
        "intents": [
            {
                "entry": "Some_Entry",
                "key": 77,
                "field": "_buff_data_raw",
                "old": "deadbeef",
                "new": "cafebabe",
            }
        ],
    })
    target, intents = parse_format3_mod(p)
    assert target == "skill.pabgb"
    assert len(intents) == 1
    assert intents[0].op == "set", (
        f"missing 'op' should default to 'set' (the only op CDUMM "
        f"applies anyway), got {intents[0].op!r}")


def test_intent_with_explicit_op_keeps_its_value(tmp_path):
    """Backwards-compat: the older DropSets variant explicitly sets
    op='set'. That should still be honored."""
    p = _write(tmp_path / "mod.field.json", {
        "format": 3,
        "target": "dropsetinfo.pabgb",
        "intents": [
            {
                "entry": "DropSet_Faction_Graymane",
                "key": 175001,
                "field": "drops",
                "op": "set",
                "new": [],
            }
        ],
    })
    _, intents = parse_format3_mod(p)
    assert intents[0].op == "set"


def test_intent_with_unknown_op_keeps_its_value_for_validator(tmp_path):
    """If a future op (e.g. 'append') appears, parse should still
    accept it. The downstream validator decides whether to skip the
    intent. We don't pre-judge ops at parse time."""
    p = _write(tmp_path / "mod.field.json", {
        "format": 3,
        "target": "x.pabgb",
        "intents": [
            {"entry": "x", "key": 1, "field": "y",
             "op": "append", "new": []}
        ],
    })
    _, intents = parse_format3_mod(p)
    assert intents[0].op == "append"


def test_missing_required_key_other_than_op_still_raises(tmp_path):
    """'op' is the only key we relax. entry, key, field, new must
    still be present , dropping any of them is malformed JSON."""
    base = {
        "format": 3,
        "target": "x.pabgb",
        "intents": [
            {"entry": "x", "key": 1, "field": "y", "new": 0}
        ],
    }
    # Strip 'entry' , should still raise.
    body = json.loads(json.dumps(base))
    del body["intents"][0]["entry"]
    p = _write(tmp_path / "noentry.field.json", body)
    with pytest.raises(ValueError, match="missing required key 'entry'"):
        parse_format3_mod(p)
