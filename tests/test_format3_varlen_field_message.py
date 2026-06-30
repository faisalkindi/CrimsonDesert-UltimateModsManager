"""Variable-length fields exported under their DMM (no-underscore) name
must get the accurate "variable-length, lands in a later phase" skip
message, not the misleading "add a field_schema entry" one.

GitHub #224: the Female Armor Module ships Format 3 intents on
stringinfo.pabgb with field "buffer". CDUMM's schema calls that field
"_buffer" and marks it variable-length (stream=None), so it is dropped
from the loaded field_specs. The classifier's raw-metadata fallback
only probed the bare name "buffer" (a miss), so every intent was
rejected with "no field_schema entry, author needs to add one" -- advice
that cannot work for a variable-length string field, and that sent a
community contributor down a dead-end reader_4B schema. The fallback now
tries the same underscore/camelCase candidates the field_specs lookup
uses, so "buffer" resolves to "_buffer" and the accurate message fires.
"""
from __future__ import annotations

from cdumm.engine.format3_handler import Format3Intent, validate_intents


def _buffer_intent(key: int = 2253925176, value: str = "khione_test") -> Format3Intent:
    return Format3Intent(
        entry="", key=key, field="buffer", op="set", new=value, old=None
    )


def test_stringinfo_buffer_reports_variable_length_not_field_schema() -> None:
    res = validate_intents("stringinfo.pabgb", [_buffer_intent()])
    assert not res.supported
    assert len(res.skipped) == 1
    _, reason = res.skipped[0]
    assert "variable-length" in reason
    assert "_buffer" in reason
    assert "field_schema" not in reason
    assert "add a field_schema" not in reason