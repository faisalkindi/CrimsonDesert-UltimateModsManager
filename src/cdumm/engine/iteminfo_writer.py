"""Iteminfo Format 3 list-of-dict field writer.

Uses CDUMM's native iteminfo parser (cdumm.engine.iteminfo_native_parser)
to parse the full iteminfo.pabgb table to dicts, apply Format 3
intents in-memory, and serialize the result back to bytes.

Whole-table approach: the iteminfo binary is 5+ MB with 6300+
records and inter-record offset/index dependencies. Whole-table
parse + apply + serialize processes the full vanilla file in a
few seconds in Python.

Bug from UnLuckyLust on GitHub #55: enchant_data_list and other
list-of-dict fields on iteminfo were skipped at validation time
with a "list writer needed" message. Now writable. v3.2.10 swapped
the parser to a clean-room native implementation that handles the
post-2026-04-29 game patch layout.
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Optional

from cdumm.engine.iteminfo_native_parser import (
    parse_iteminfo_from_bytes, serialize_iteminfo,
)

if TYPE_CHECKING:
    from cdumm.engine.format3_handler import Format3Intent

logger = logging.getLogger(__name__)


# Iteminfo fields the writer accepts. The native parser's ItemInfo
# dict carries all of these as native Python types we can replace
# wholesale via dict assignment. List shapes match the JSON
# Format 3 intent's `new` value verbatim.
SUPPORTED_FIELDS = {
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

# Fields that mod authors target on iteminfo but the native parser's
# ``_ITEM_FIELDS`` schema currently doesn't emit. Adding one of these
# to a parsed record is silently dropped by ``_write_item``, so the
# additive-write branch below cannot honour them. Surface a clear,
# user-actionable skip reason instead of pretending to apply.
#
# Re-add a field to ``SUPPORTED_FIELDS`` (and remove it from this set)
# only after a paired ``_read_X`` / ``_write_X`` round-trip lands in
# ``iteminfo_native_parser._ITEM_FIELDS`` and survives the 6235-record
# vanilla walk in ``test_iteminfo_walk_real_game.py``.
UNWRITEABLE_KNOWN_FIELDS = {
    # Live-binary EnchantData layout has not been reverse-engineered;
    # the parser comment in iteminfo_native_parser.py marks it
    # "likely removed in the live schema along with other layout
    # shifts". GitHub #79 (UnLuckyLust 2026-05-10): a mod that adds
    # enchants to an item without any in vanilla used to silently
    # report "0 byte changes". The writer now logs a clear skip
    # naming the field so the user understands why.
    "enchant_data_list",
}


def _resolve_field_name(intent_field: str, item: dict) -> Optional[str]:
    """Map a Format 3 intent field name to a key in the native
    parser's ItemInfo dict, or None if no match.

    Field-names dialect exports use snake_case-without-underscore-
    prefix (`enchant_data_list`, `is_blocked`); the parser's
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


def _resolve_path_target(
    item: dict, path: str,
) -> Optional[tuple]:
    """Walk a Format 3 dotted/indexed path on a parsed ItemInfo dict.

    Path syntax: ``field``, ``field.subfield``, ``field[N]``,
    ``field[N].subfield``, ``a.b[N].c.d``. Returns
    ``(parent_container, final_segment)`` so the caller can do
    ``parent[final_segment] = new_value`` (works whether parent is
    a dict and segment is a key, or parent is a list and segment is
    an int index).

    Returns None if any segment fails to resolve (missing dict key,
    list index out of range, type mismatch).

    Used by the iteminfo writer to apply Format 3 intents that
    target nested struct sub-fields. Bug confirmed 2026-05-08
    against gmVIP233 / niyaruza prefab_data_list[N].tribe_gender_list
    and floozo drop_default_data.X paths.
    """
    import re
    # Tokenize: identifier OR [N]
    tokens: list[tuple[str, object]] = []
    for m in re.finditer(r"([A-Za-z_]\w*)|\[(\d+)\]", path):
        name, idx = m.groups()
        if name is not None:
            tokens.append(("key", name))
        else:
            tokens.append(("idx", int(idx)))
    if not tokens:
        return None

    cur: object = item
    for kind, val in tokens[:-1]:
        try:
            if kind == "key":
                # First-segment key may need camelCase / underscore
                # bridging via _resolve_field_name for round-1 lookup.
                if isinstance(cur, dict) and val not in cur:
                    resolved = _resolve_field_name(str(val), cur)
                    if resolved is None:
                        return None
                    cur = cur[resolved]
                else:
                    cur = cur[val]  # type: ignore[index]
            else:
                cur = cur[val]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return None

    last_kind, last_val = tokens[-1]
    # Final-segment key bridging: same dialect handling as multi-segment
    if last_kind == "key" and isinstance(cur, dict) and last_val not in cur:
        resolved = _resolve_field_name(str(last_val), cur)
        if resolved is not None:
            return (cur, resolved)
        return None
    return (cur, last_val)


def build_iteminfo_intent_change(
    vanilla_body: bytes,
    intents: "list[Format3Intent]",
) -> Optional[dict]:
    """Apply all provided intents to a parsed copy of vanilla
    iteminfo.pabgb and return a single whole-file v2 change dict.

    Returns None if no intents touched a real record or all intents
    failed (so the caller can fall back to the regular per-intent
    path).

    Per-intent failures (unknown key, unsupported field) are logged
    and skipped; surviving intents still produce their effect.
    """
    try:
        items = parse_iteminfo_from_bytes(vanilla_body)
    except Exception as e:
        # GitHub #182 (CD 1.09): the new game patch shifted the
        # iteminfo layout in ways the parser does not yet model.
        # Surface a recognisable hint in the log so the bug report
        # bundle that ends up on the issue tracker is easier to
        # triage, instead of just the raw struct.error.
        logger.error(
            "iteminfo parse failed (%s). On Crimson Desert 1.09 this "
            "is the known schema shift tracked under GitHub #182. "
            "Format 3 list-of-dict intents on iteminfo will be skipped "
            "until the parser catches up. Format 2 / offset-based "
            "byte patches still apply.",
            e, exc_info=True)
        return None

    by_key = {it["key"]: it for it in items}
    applied = 0
    skipped_op = 0
    skipped_key = 0
    skipped_field = 0
    for intent in intents:
        # Field-level early skip: some intent fields are known not
        # writeable by the current iteminfo serialiser even though
        # mod authors target them. The additive-write branch below
        # used to accept these (and bumped `applied`), but the
        # serialiser dropped the new key on its way out, producing
        # silent zero-byte changes (UnLuckyLust GitHub #79).
        if intent.field in UNWRITEABLE_KNOWN_FIELDS:
            skipped_field += 1
            logger.warning(
                "iteminfo writer: field %r is not currently "
                "writeable by CDUMM (the iteminfo serializer's "
                "schema does not emit this field). Intent on "
                "key=%d dropped. This needs schema support in "
                "iteminfo_native_parser._ITEM_FIELDS before "
                "modded values can land in game bytes.",
                intent.field, intent.key)
            continue
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
        # The v3.2.12 defensive guard for misaligned cooltime intents
        # is no longer needed: the parser's _read_DefaultSubItem now
        # consumes the trailing 13-byte block (i64 + u32 + u8) that
        # used to be misattributed to sharpness_data.p_prefix on PW
        # shape. cooltime / unk_post_cooltime_a / unk_post_cooltime_b
        # now read at the correct on-disk byte offsets across all
        # 6235 vanilla records (byte-perfect round-trip verified).
        # Format 3 intents on these fields land at the right bytes.
        # Nested path (dotted / indexed): walk the parsed dict to the
        # assignment target. Used for prefab_data_list[N].xxx,
        # drop_default_data.xxx, etc. The path-resolver returns
        # (parent_container, final_segment) for assignment.
        if "." in intent.field or "[" in intent.field:
            target = _resolve_path_target(item, intent.field)
            if target is None:
                skipped_field += 1
                logger.warning(
                    "iteminfo writer: nested path %r did not resolve "
                    "for key=%d, skipping (segment missing or index "
                    "out of range)", intent.field, intent.key)
                continue
            parent, last_seg = target
            try:
                parent[last_seg] = intent.new
                applied += 1
            except (KeyError, IndexError, TypeError) as e:
                logger.warning(
                    "iteminfo writer: nested-path assignment on "
                    "key=%d field=%r failed: %s",
                    intent.key, intent.field, e)
            continue

        # Resolve field name: try direct (snake_case-no-prefix as the
        # field-names dialect emits) first, then underscore-stripped +
        # snake-case of camelCase for schema-style names like
        # `_isBlocked`.
        target_field = _resolve_field_name(intent.field, item)
        if target_field is None:
            # Field name is supported by the writer but the parsed
            # ItemInfo dict for THIS specific key doesn't carry it
            # (vanilla item has no enchants → `enchant_data_list` is
            # absent from the parsed record). The intent wants to ADD
            # the field, not overwrite. Bug 2026-05-10 (hhkbble #79):
            # Oh_My_Thief.field.json adds enchant_data_list to Axiom
            # Bracelet (key=1001129) which has no enchants in vanilla,
            # so resolving the field name failed and the intent was
            # silently dropped. Treat any SUPPORTED_FIELDS name as an
            # additive key on the existing record.
            if intent.field in SUPPORTED_FIELDS:
                target_field = intent.field
            else:
                skipped_field += 1
                logger.warning(
                    "iteminfo writer: field %r not in ItemInfo dict "
                    "for key=%d, skipping (likely a primitive name "
                    "the writer doesn't expose; per-record path "
                    "would handle it but this intent was force-"
                    "batched into the whole-table writer because "
                    "another intent on iteminfo uses a list-of-dict "
                    "field)", intent.field, intent.key)
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
        new_bytes = serialize_iteminfo(items)
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
