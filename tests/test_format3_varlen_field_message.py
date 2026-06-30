"""stringinfo.pabgb ``_buffer`` Format 3 handling.

GitHub #224: the Female Armor Module ships Format 3 intents on
stringinfo.pabgb with field "buffer" (DMM name; CDUMM's schema calls it
"_buffer" and marks it variable-length, stream=None, so it is dropped
from the loaded field_specs).

History:
  * First a stopgap (PR #227) fixed only the skip *message*: the
    classifier's raw-metadata fallback now tries the same
    underscore/camelCase candidates the field_specs lookup uses, so
    "buffer" resolves to "_buffer" and reports the accurate
    "variable-length, lands in a later phase" message instead of the
    dead-end "add a field_schema entry" advice.
  * Then the stringinfo writer (GitHub #224 proper) made the write
    actually apply: a buffer intent with a string value is routed to
    stringinfo_writer.build_stringinfo_changes, located by key, and the
    record's length-prefixed string is rewritten with a companion
    .pabgh offset rebuild.

So a string-valued buffer write is now SUPPORTED. The message fix still
matters for values the writer cannot handle (e.g. a non-string), which
stay skipped with the accurate variable-length message, never the
misleading field_schema one.
"""
from __future__ import annotations

from cdumm.engine.format3_handler import Format3Intent, validate_intents


def _buffer_intent(key: int = 2253925176, value=None) -> Format3Intent:
    return Format3Intent(
        entry="", key=key, field="buffer", op="set",
        new="khione_test" if value is None else value, old=None,
    )


def test_stringinfo_string_buffer_write_is_supported() -> None:
    # The #224 writer accepts a string-valued buffer write.
    res = validate_intents("stringinfo.pabgb", [_buffer_intent()])
    assert len(res.supported) == 1
    assert not res.skipped


def test_stringinfo_non_string_buffer_reports_variable_length() -> None:
    # A buffer value the writer cannot write (non-string) is still
    # skipped, with the accurate variable-length message, never the
    # misleading "add a field_schema entry" advice.
    res = validate_intents(
        "stringinfo.pabgb", [_buffer_intent(value=12345)])
    assert not res.supported
    assert len(res.skipped) == 1
    _, reason = res.skipped[0]
    assert "variable-length" in reason
    assert "_buffer" in reason
    assert "add a field_schema" not in reason
