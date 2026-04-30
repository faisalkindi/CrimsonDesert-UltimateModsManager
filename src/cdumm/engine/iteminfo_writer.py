"""Iteminfo Format 3 list-of-dict field writer.

Uses the vendored crimson_rs Rust extension (NattKh's parser) to
parse the full iteminfo.pabgb table to dicts, apply Format 3
intents in-memory, and serialize the result back to bytes.

Whole-table approach: the iteminfo binary is 5+ MB with 6300+
records and inter-record offset/index dependencies. Per-record
serialize would require crimson_rs to expose record boundaries,
which it doesn't. Whole-table parse + apply + serialize is what
NattKh's own tools use, and crimson_rs does it in ~0.3 seconds
on the full vanilla file.

Bug from UnLuckyLust on GitHub #55: enchant_data_list and other
list-of-dict fields on iteminfo were skipped at validation time
with a "list writer needed" message. Now writable.
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Optional

from cdumm.engine.crimson_rs_loader import get_crimson_rs

if TYPE_CHECKING:
    from cdumm.engine.format3_handler import Format3Intent

logger = logging.getLogger(__name__)


# Iteminfo fields the writer accepts. crimson_rs's ItemInfo dict
# carries all of these as native Python types we can replace
# wholesale via dict assignment. List shapes match the JSON
# Format 3 intent's `new` value verbatim.
SUPPORTED_FIELDS = {
    "enchant_data_list",
    "equip_passive_skill_list",
    "occupied_equip_slot_data_list",
    "item_tag_list",
    "consumable_type_list",
    "item_use_info_list",
    "item_icon_list",
    "sealable_item_info_list",
    "sealable_character_info_list",
    "sealable_gimmick_info_list",
    "sealable_gimmick_tag_list",
    "sealable_tribe_info_list",
    "sealable_money_info_list",
    "transmutation_material_gimmick_list",
    "transmutation_material_item_list",
    "transmutation_material_item_group_list",
    "multi_change_info_list",
    "gimmick_tag_list",
}


def _resolve_field_name(intent_field: str, item: dict) -> Optional[str]:
    """Map a Format 3 intent field name to a key in crimson_rs's
    ItemInfo dict, or None if no match.

    NattKh's exports use snake_case-without-underscore-prefix
    (`enchant_data_list`, `is_blocked`); crimson_rs's TypedDict
    matches that convention. Other tools may emit schema-style
    underscore-prefixed camelCase (`_isBlocked`); we bridge both.
    """
    if intent_field in item:
        return intent_field
    if intent_field.startswith("_"):
        stripped = intent_field.lstrip("_")
        if stripped in item:
            return stripped
        # camelCase -> snake_case
        import re
        snake = re.sub(r"(?<!^)([A-Z])", r"_\1", stripped).lower()
        if snake in item:
            return snake
    return None


def build_iteminfo_intent_change(
    vanilla_body: bytes,
    intents: "list[Format3Intent]",
) -> Optional[dict]:
    """Apply all provided intents to a parsed copy of vanilla
    iteminfo.pabgb and return a single whole-file v2 change dict.

    Returns None if crimson_rs is unavailable, no intents touched
    a real record, or all intents failed (so the caller can fall
    back to the regular per-intent path).

    Per-intent failures (unknown key, unsupported field) are logged
    and skipped; surviving intents still produce their effect.
    """
    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        logger.warning("iteminfo writer unavailable (crimson_rs not loaded)")
        return None

    try:
        items = crimson_rs.parse_iteminfo_from_bytes(vanilla_body)
    except Exception as e:
        logger.error("iteminfo parse failed: %s", e, exc_info=True)
        return None

    by_key = {it["key"]: it for it in items}
    applied = 0
    for intent in intents:
        if intent.key not in by_key:
            logger.debug(
                "iteminfo writer: key %d not in table, skipping intent",
                intent.key)
            continue
        if intent.op != "set":
            logger.debug(
                "iteminfo writer: op %r not supported (only 'set')",
                intent.op)
            continue
        item = by_key[intent.key]
        # Resolve field name: try direct (snake_case-no-prefix from
        # NattKh tools) first, then underscore-stripped + snake-case
        # of camelCase for schema-style names like `_isBlocked`.
        target_field = _resolve_field_name(intent.field, item)
        if target_field is None:
            logger.debug(
                "iteminfo writer: field %r not in ItemInfo dict for "
                "key=%d, skipping", intent.field, intent.key)
            continue
        try:
            item[target_field] = intent.new
            applied += 1
        except Exception as e:
            logger.warning(
                "iteminfo writer: applying intent on key=%d field=%r "
                "failed: %s", intent.key, intent.field, e)

    if applied == 0:
        return None

    try:
        new_bytes = crimson_rs.serialize_iteminfo(items)
    except Exception as e:
        logger.error("iteminfo serialize failed: %s", e, exc_info=True)
        return None

    if new_bytes == vanilla_body:
        # Intents resolved but produced no byte difference (e.g.,
        # `set` to the same value). No change to emit.
        return None

    return {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_bytes.hex(),
        "label": f"iteminfo Format 3 intents ({applied} applied)",
    }
