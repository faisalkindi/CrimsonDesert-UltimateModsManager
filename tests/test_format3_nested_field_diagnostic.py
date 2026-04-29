"""Format 3 nested-field intents get a clear "not yet supported"
message instead of the generic "no field_schema entry" wall.

Bug from UnLuckyLust on GitHub #55, 2026-04-28: NattKh's
CrimsonGameMods v3 exports targeting `iteminfo.pabgb` with fields
like `enchant_data_list` (a list of dicts) and dotted paths like
`docking_child_data.attach_parent_socket_name` get rejected with:

    "field 'enchant_data_list' has no field_schema entry and isn't
     in the PABGB record schema — author needs to add a
     field_schema/iteminfo.json entry mapping 'enchant_data_list'
     to a tid or rel_offset"

The author CAN'T add a field_schema for these — they're
fundamentally out of scope for v3.2.x's writer (variable-length
nested structures need byte-shift propagation that lands in v3.3).
The current message wrongly tells the author it's their problem to
fix.

Fix: detect the two unsupported shapes at validation time and
return a message that names the limitation honestly:
  * Dotted-path fields (e.g. `docking_child_data.X`)
  * List-valued fields (e.g. `enchant_data_list`)
"""
from __future__ import annotations


def test_dotted_path_intent_message_names_the_limitation():
    """An intent with a dotted field name (`parent.child`) gets a
    message that says nested writes aren't supported yet, not a
    misleading 'add a field_schema entry'."""
    from cdumm.engine.format3_handler import _diagnose_unsupported_intent

    msg = _diagnose_unsupported_intent(
        field="docking_child_data.attach_parent_socket_name",
        new_value="Gimmick_Hand_L_00_Socket",
    )
    assert msg is not None
    msg_lower = msg.lower()
    assert "nested" in msg_lower or "dotted" in msg_lower or "sub-field" in msg_lower, (
        f"Message should explain dotted-path is not yet supported. "
        f"Got: {msg!r}")
    assert "v3.3" in msg or "future" in msg_lower or "coming" in msg_lower, (
        f"Message should set the timeline expectation. Got: {msg!r}")


def test_list_of_dicts_intent_message_names_the_limitation():
    """An intent setting a list-of-dicts (e.g. enchant_data_list)
    gets a message that explains variable-length list rewriting
    needs byte-shift propagation that lands in v3.3."""
    from cdumm.engine.format3_handler import _diagnose_unsupported_intent

    msg = _diagnose_unsupported_intent(
        field="enchant_data_list",
        new_value=[{"level": 0, "buy_price_list": []}],
    )
    assert msg is not None
    msg_lower = msg.lower()
    assert ("list" in msg_lower or "array" in msg_lower
            or "variable-length" in msg_lower), (
        f"Message should explain list/array rewriting limitation. "
        f"Got: {msg!r}")
    assert "v3.3" in msg or "future" in msg_lower or "coming" in msg_lower, (
        f"Got: {msg!r}")


def test_primitive_intent_returns_none():
    """A primitive int/float/string intent is supported (or has
    its own existing diagnostic). The new helper only fires for
    the two unsupported nested shapes."""
    from cdumm.engine.format3_handler import _diagnose_unsupported_intent

    assert _diagnose_unsupported_intent(field="cooltime", new_value=1) is None
    assert _diagnose_unsupported_intent(
        field="some_field", new_value=3.14) is None
    assert _diagnose_unsupported_intent(
        field="another_field", new_value="hello") is None
    # Fixed-size byte arrays (like 4-byte hash) are NOT list-of-dicts;
    # they're either supported via existing array path or fall through
    # to the existing schema-not-found message.
    assert _diagnose_unsupported_intent(
        field="docking_tag_name_hash",
        new_value=[0, 0, 0, 0]) is None
