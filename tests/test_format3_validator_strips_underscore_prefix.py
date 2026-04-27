"""Format 3 validator must accept NattKh-naming field intents.

Bug from Faisal 2026-04-27: NoCooldownForALLItems Format 3 mod has
201 intents using field names ``cooltime``, ``unk_post_cooltime_a``,
``unk_post_cooltime_b`` (NattKh strips the leading underscore from
the CDUMM-internal Pearl Abyss field names). CDUMM's loaded schema
uses underscore-prefixed names (``_cooltime``) because that's what
NattKh's IDA-MCP schema dumper produces.

The apply path at ``format3_apply.py:324`` already handles this with
a fallback lookup (``field_specs.get(intent.field) or
field_specs.get(f"_{intent.field}")``). The validator at
``format3_handler.py:312`` does NOT — it only does a direct
``field_specs.get(intent.field)``, returns None for ``cooltime``,
and reports "no field_schema entry / not in PABGB record schema"
even though the apply path would happily resolve it.

Same gap in ``_field_walker_reachable`` which uses
``spec.name == target_field`` direct compare with no prefix fallback.

Validator + walker-reachable must do the same fallback the writer
does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from cdumm.engine.format3_handler import (
    Format3Intent, _field_walker_reachable, validate_intents,
)


@dataclass
class _FakeSpec:
    name: str
    stream_size: int = 0
    type_descriptor: Optional[str] = None
    field_type: str = ""
    struct_fmt: Optional[str] = None


@dataclass
class _FakeSchema:
    fields: list


def test_walker_reachable_accepts_unprefixed_name_for_prefixed_schema_field():
    """Schema has `_cooltime` (NattKh schema-dumper naming);
    intent uses `cooltime` (NattKh mod naming). Reachable check
    must succeed via prefix fallback."""
    schema = _FakeSchema(fields=[
        _FakeSpec(name="_isBlocked", stream_size=1, type_descriptor="u8"),
        _FakeSpec(name="_cooltime", stream_size=8, type_descriptor="i64"),
    ])
    # Direct lookup with prefixed name still works
    assert _field_walker_reachable(schema, "_cooltime") is True
    # Stripped lookup must ALSO work via prefix fallback
    assert _field_walker_reachable(schema, "cooltime") is True, (
        "Walker-reachable check must accept the unprefixed mod-naming "
        "form by falling back to `_<name>` lookup, matching the "
        "apply path's existing fallback at format3_apply.py:324")


def test_validator_accepts_unprefixed_name_for_prefixed_schema_field(
        monkeypatch):
    """Real-world scenario: NoCooldownForALLItems mod sends 67 intents
    each on `cooltime`, `unk_post_cooltime_a`, `unk_post_cooltime_b`.
    Validator must mark them supported (not skipped with "no
    field_schema entry") so the apply path gets to run."""
    # Use the real iteminfo schema so we exercise the actual loader
    from cdumm.semantic import parser as parser_mod
    parser_mod._loaded_schemas = None

    # Synthetic intent on an arbitrary entry — validator doesn't check
    # that the entry exists; it only checks field reachability /
    # writability.
    intents = [
        Format3Intent(entry="Pyeonjeon_Arrow", key=2200,
                       field="cooltime", op="set", new=0),
    ]
    result = validate_intents("iteminfo.pabgb", intents)
    assert len(result.supported) == 1, (
        "Intent on `cooltime` must be marked supported because the "
        "schema has `_cooltime` and the apply path handles the "
        "prefix-strip fallback. Got skipped reasons: "
        f"{[reason for _, reason in result.skipped]}")


def test_validator_accepts_three_unk_post_cooltime_intents():
    """The exact three field names from the NoCooldownForALLItems
    error report. All three must validate."""
    from cdumm.semantic import parser as parser_mod
    parser_mod._loaded_schemas = None

    intents = [
        Format3Intent(entry="X", key=2200, field="cooltime",
                       op="set", new=0),
        Format3Intent(entry="X", key=2200, field="unk_post_cooltime_a",
                       op="set", new=0),
        Format3Intent(entry="X", key=2200, field="unk_post_cooltime_b",
                       op="set", new=0),
    ]
    result = validate_intents("iteminfo.pabgb", intents)
    assert len(result.supported) == 3, (
        f"All 3 NattKh-named intents must validate. "
        f"Skipped: {[(i.field, r) for i, r in result.skipped]}")
