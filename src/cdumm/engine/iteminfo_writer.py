"""Iteminfo Format 3 list-of-dict field writer.

Uses the vendored crimson_rs Rust extension (NattKh's parser) to
parse the full iteminfo.pabgb table to dicts, apply Format 3
intents in-memory, and serialize the result back to bytes.

Whole-table approach: the iteminfo binary is 5+ MB with 6300+
records and inter-record offset/index dependencies. Per-record
serialize would require crimson_rs to expose record boundaries,
which it doesn't. Whole-table parse + apply + serialize processes
the full vanilla file in ~0.3 seconds.

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

try:
    from cdumm._vendor.iteminfo_parser import (
        IteminfoFile,
        PassiveSkill,
        EquipBuff,
    )
    _HAVE_CUSTOM_PARSER = True
except Exception as _e:
    _HAVE_CUSTOM_PARSER = False
    logger = __import__("logging").getLogger(__name__)
    logger.warning("iteminfo custom parser not available: %s", _e)

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

    Field-names dialect exports use snake_case-without-underscore-
    prefix (`enchant_data_list`, `is_blocked`); crimson_rs's
    TypedDict matches that convention. Other tools may emit
    schema-style underscore-prefixed camelCase (`_isBlocked`); we
    bridge both.
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


def _build_with_custom_parser(
    vanilla_body: bytes,
    vanilla_hdr: bytes,
    intents: "list[Format3Intent]",
) -> Optional[dict]:
    """Apply intents using the pure-Python iteminfo_parser, which
    supports the current game format that crimson_rs cannot parse."""
    try:
        f = IteminfoFile(vanilla_body, vanilla_hdr)
    except Exception as e:
        logger.error("custom iteminfo parser init failed: %s", e)
        return None

    applied = 0
    for intent in intents:
        if intent.op != "set":
            logger.warning(
                "iteminfo custom writer: op %r not supported on key=%d field=%r",
                intent.op, intent.key, intent.field)
            continue
        if intent.key not in f.idx:
            logger.debug(
                "iteminfo custom writer: key %d not in table", intent.key)
            continue

        try:
            if intent.field == "equip_passive_skill_list":
                passives = [
                    PassiveSkill(s["skill"], s["level"])
                    for s in intent.new
                ]
                f.write(intent.key, passives=passives)
                applied += 1

            elif intent.field == "enchant_data_list":
                # Pull equip_buffs out of the full list replacement.
                # Stats and prices are preserved from existing binary;
                # only equip_buffs are replaced.
                new_buffs = [
                    EquipBuff(b["buff"], b["level"])
                    for ed in intent.new
                    for b in ed.get("equip_buffs", [])
                ]
                f.write(intent.key, buffs=new_buffs)
                applied += 1

            elif intent.field in ("cooltime", "_cooltime"):
                f.write(intent.key, cooltime=int(intent.new))
                applied += 1

            elif intent.field in ("gimmick_info", "_gimmickInfo"):
                f.write(intent.key, gimmick_info=int(intent.new))
                applied += 1

            elif intent.field in ("equip_type_info", "_equipTypeInfo"):
                f.write(intent.key, equip_type_info=int(intent.new))
                applied += 1

            elif intent.field in ("item_type", "_itemType"):
                f.write(intent.key, item_type=int(intent.new))
                applied += 1

            else:
                logger.warning(
                    "iteminfo custom writer: field %r not handled "
                    "(key=%d) — add to _build_with_custom_parser if needed",
                    intent.field, intent.key)

        except Exception as e:
            logger.warning(
                "iteminfo custom writer: intent key=%d field=%r failed: %s",
                intent.key, intent.field, e)

    if applied == 0:
        return None

    new_body = f.get_body()
    if new_body == vanilla_body:
        return None

    logger.info(
        "iteminfo custom writer: %d intent(s) applied, "
        "body %d -> %d bytes",
        applied, len(vanilla_body), len(new_body))

    return {
        "offset":   0,
        "original": vanilla_body.hex(),
        "patched":  new_body.hex(),
        "label":    f"iteminfo custom parser ({applied} intent(s) applied)",
    }


def build_iteminfo_intent_change(
    vanilla_body: bytes,
    intents: "list[Format3Intent]",
    vanilla_hdr: bytes | None = None,
) -> Optional[dict]:
    """Apply all provided intents to a parsed copy of vanilla
    iteminfo.pabgb and return a single whole-file v2 change dict.

    Tries our Python parser first (handles the current game format).
    Falls back to crimson_rs if the custom parser is unavailable.

    Returns None if no intents applied or all failed.
    """
    # --- custom Python parser path ---
    if _HAVE_CUSTOM_PARSER and vanilla_hdr is not None:
        return _build_with_custom_parser(vanilla_body, vanilla_hdr, intents)

    if vanilla_hdr is None:
        logger.warning(
            "iteminfo writer: pabgh header not supplied, "
            "falling back to crimson_rs")

    # --- crimson_rs fallback ---
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
    skipped_op = 0
    skipped_key = 0
    skipped_field = 0
    for intent in intents:
        if intent.key not in by_key:
            skipped_key += 1
            logger.debug(
                "iteminfo writer: key %d not in table, skipping intent",
                intent.key)
            continue
        if intent.op != "set":
            # WARNING (not debug) because it's a real intent the mod
            # author wrote that gets silently dropped — bug reports
            # should capture this. Users targeting `max_stack_count
            # op="add" new=10` (relative bump) need to know it ran as
            # nothing.
            skipped_op += 1
            logger.warning(
                "iteminfo writer: op %r not supported (only 'set'); "
                "intent on key=%d field=%r dropped",
                intent.op, intent.key, intent.field)
            continue
        item = by_key[intent.key]
        # Resolve field name: try direct (snake_case-no-prefix as the
        # field-names dialect emits) first, then underscore-stripped +
        # snake-case of camelCase for schema-style names like
        # `_isBlocked`.
        target_field = _resolve_field_name(intent.field, item)
        if target_field is None:
            skipped_field += 1
            logger.warning(
                "iteminfo writer: field %r not in ItemInfo dict for "
                "key=%d, skipping (likely a primitive name the writer "
                "doesn't expose; per-record path would handle it but "
                "this intent was force-batched into the whole-table "
                "writer because another intent on iteminfo uses a "
                "list-of-dict field)", intent.field, intent.key)
            continue
        try:
            item[target_field] = intent.new
            applied += 1
        except Exception as e:
            logger.warning(
                "iteminfo writer: applying intent on key=%d field=%r "
                "failed: %s", intent.key, intent.field, e)

    if applied == 0:
        skip_total = skipped_op + skipped_key + skipped_field
        if skip_total:
            logger.warning(
                "iteminfo writer: 0 of %d intent(s) applied "
                "(%d non-'set' op, %d unknown key, %d unknown field). "
                "No change emitted.",
                skip_total, skipped_op, skipped_key, skipped_field)
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

    skip_total = skipped_op + skipped_key + skipped_field
    if skip_total:
        skip_summary_parts = []
        if skipped_op:
            skip_summary_parts.append(f"{skipped_op} non-'set' op")
        if skipped_key:
            skip_summary_parts.append(f"{skipped_key} unknown key")
        if skipped_field:
            skip_summary_parts.append(f"{skipped_field} unknown field")
        skip_summary = ", ".join(skip_summary_parts)
        label = (
            f"iteminfo Format 3 intents ({applied} applied, "
            f"{skip_total} skipped: {skip_summary})"
        )
    else:
        label = f"iteminfo Format 3 intents ({applied} applied)"

    return {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_bytes.hex(),
        "label": label,
    }
