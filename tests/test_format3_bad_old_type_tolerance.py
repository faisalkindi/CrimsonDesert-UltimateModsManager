"""GitHub #467 (turiyski): a Format 3 mod ("Quest Givers On Map -
Residents Corners and Tales After") ships an intent with 'old' as a
JSON integer instead of a hex string. The parser used to raise
ValueError for that single intent, which import_from_natt_format_3
turns into "Invalid Format 3 mod: ..." and refuses to import the file
at all -- taking every OTHER intent in the mod down with it, even
though 'old' is either ignored outright (tables with a PABGB schema)
or already fails gracefully per-intent at apply time (format3_apply's
bytes.fromhex is wrapped in try/except for exactly this case).

These tests pin the fix: a non-string 'old' must not abort parsing.
"""
from __future__ import annotations

import json

from cdumm.engine.format3_handler import parse_format3_mod_targets


def test_non_string_old_does_not_abort_the_whole_mod(tmp_path):
    """A sibling valid intent in the same target must still parse
    when another intent in the same file has an int 'old'."""
    doc = {
        "format": 3,
        "modinfo": {"title": "Quest Givers On Map", "version": "1.3.0"},
        "target": "missioninfo.pabgb",
        "intents": [
            {
                "entry": "Some_Mission",
                "key": 1,
                "field": "some_field",
                "op": "set",
                "new": "00",
                "old": 0,
            },
            {
                "entry": "Other_Mission",
                "key": 2,
                "field": "other_field",
                "op": "set",
                "new": 1,
            },
        ],
    }
    p = tmp_path / "quest_givers.json"
    p.write_text(json.dumps(doc), encoding="utf-8")

    pairs = parse_format3_mod_targets(p)

    assert len(pairs) == 1
    target, intents = pairs[0]
    assert target == "missioninfo.pabgb"
    assert len(intents) == 2
    assert intents[0].old == 0
    assert intents[1].old is None
    assert intents[1].new == 1
