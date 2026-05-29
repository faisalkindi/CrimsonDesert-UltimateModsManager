"""GitHub #171 (pinapana): DMM's exports name socket-equipment fields
with the binary-side underscored camelCase form
(`_addSocketMaterialItemList`, `_socketValidCount`, `_useSocket`),
while CDUMM's iteminfo native parser keeps these inside the
`drop_default_data` struct under their snake_case names
(`drop_default_data.add_socket_material_item_list` etc.).

The format3 parser rewrites the three DMM names to the canonical
dotted form at parse time so the existing iteminfo nested-path writer
handles them with no additional special-casing.
"""
from __future__ import annotations

import dataclasses

from cdumm.engine.format3_handler import (
    _apply_field_aliases,
    Format3Intent,
)


def _make_socket_intents() -> list[Format3Intent]:
    """The exact intent shape pinapana reported in #171."""
    return [
        Format3Intent(
            entry="Daeil_Band", key=1001129,
            field="_addSocketMaterialItemList", op="set",
            new=[
                {"item": 1, "value": 500},
                {"item": 1, "value": 1000},
                {"item": 1, "value": 2000},
                {"item": 1, "value": 3000},
                {"item": 1, "value": 4000},
            ],
        ),
        Format3Intent(
            entry="Daeil_Band", key=1001129,
            field="_socketValidCount", op="set", new=5),
        Format3Intent(
            entry="Daeil_Band", key=1001129,
            field="_useSocket", op="set", new=1),
    ]


def test_dmm_socket_field_aliases_rewritten():
    """The three DMM camelCase names become dotted paths under
    drop_default_data after _apply_field_aliases runs."""
    intents = _make_socket_intents()
    _apply_field_aliases("iteminfo.pabgb", intents)
    fields = [i.field for i in intents]
    assert "drop_default_data.add_socket_material_item_list" in fields
    assert "drop_default_data.socket_valid_count" in fields
    assert "drop_default_data.use_socket" in fields
    assert "_addSocketMaterialItemList" not in fields
    assert "_socketValidCount" not in fields
    assert "_useSocket" not in fields


def test_aliases_preserve_intent_payload():
    """The rewrite only touches `field`. entry, key, op and new
    survive intact, including the list-of-dicts payload for
    add_socket_material_item_list."""
    intents = _make_socket_intents()
    original_new = [dataclasses.replace(i, field=i.field).new
                    for i in intents]
    _apply_field_aliases("iteminfo.pabgb", intents)
    for orig_new, intent in zip(original_new, intents):
        assert intent.entry == "Daeil_Band"
        assert intent.key == 1001129
        assert intent.op == "set"
        assert intent.new == orig_new


def test_aliases_only_fire_on_iteminfo_target():
    """The DMM socket-field aliases must not fire when the target is
    a different table; the bare names belong to other tables in
    other contexts (e.g. characterinfo, skillinfo)."""
    intents = _make_socket_intents()
    _apply_field_aliases("buffinfo.pabgb", intents)
    fields = [i.field for i in intents]
    # Names should be untouched on a non-iteminfo target.
    assert "_addSocketMaterialItemList" in fields
    assert "_socketValidCount" in fields
    assert "_useSocket" in fields
