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
import struct
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

# Element kind each SUPPORTED_FIELDS list carries on disk, used to
# shape-check additive writes (vanilla record lacks the field, or has
# it empty, so there is no existing value to compare against). Derived
# from iteminfo_native_parser._ITEM_FIELDS: carray_u32/u16 lists hold
# ints, carray_cstring holds strs, struct carrays hold dicts.
_LIST_ELEMENT_KINDS: "dict[str, type]" = {
    "equip_passive_skill_list": dict,
    "occupied_equip_slot_data_list": dict,
    "item_tag_list": int,
    "consumable_type_list": int,
    "item_use_info_list": int,
    "item_icon_list": dict,
    "sealable_item_info_list": dict,
    "sealable_character_info_list": dict,
    "sealable_gimmick_info_list": dict,
    "sealable_gimmick_tag_list": dict,
    "sealable_tribe_info_list": dict,
    "sealable_money_info_list": int,
    "transmutation_material_gimmick_list": int,
    "transmutation_material_item_list": int,
    "transmutation_material_item_group_list": int,
    "multi_change_info_list": int,
    "gimmick_tag_list": str,
}


def _elements_match_kind(values: list, kind: type) -> bool:
    if kind is int:
        # bool is an int subclass and packs fine; exclude floats/strs.
        return all(isinstance(v, int) for v in values)
    return all(isinstance(v, kind) for v in values)


def shape_matches(existing, new) -> bool:
    """Cheap per-intent shape gate before dict assignment.

    The serializer walks the parsed dicts with struct.pack and raises
    deep inside serialize when a mod ships e.g. a list of ints for a
    list-of-dicts field, which used to abort the WHOLE multi-mod batch.
    Comparing the new value's shape against the existing parsed value
    keeps a malformed intent a per-intent skip instead.
    """
    if isinstance(existing, list):
        if not isinstance(new, list):
            return False
        if existing and new:
            e0 = existing[0]
            if isinstance(e0, dict):
                return all(isinstance(x, dict) for x in new)
            if isinstance(e0, list):
                return all(isinstance(x, list) for x in new)
            if isinstance(e0, bool):
                return all(isinstance(x, (bool, int)) for x in new)
            if isinstance(e0, int):
                return all(isinstance(x, int) for x in new)
            if isinstance(e0, float):
                return all(isinstance(x, (int, float)) for x in new)
            if isinstance(e0, str):
                return all(isinstance(x, str) for x in new)
            if isinstance(e0, (bytes, bytearray)):
                return all(isinstance(x, (bytes, bytearray)) for x in new)
        return True
    if isinstance(existing, dict):
        return isinstance(new, dict)
    if isinstance(existing, bool):
        return isinstance(new, (bool, int))
    if isinstance(existing, int):
        return isinstance(new, int)
    if isinstance(existing, float):
        return isinstance(new, (int, float)) and not isinstance(new, bool)
    if isinstance(existing, str):
        return isinstance(new, str)
    if isinstance(existing, (bytes, bytearray)):
        return isinstance(new, (bytes, bytearray))
    # None / unknown existing value: nothing to compare against.
    return True


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
        # Separator-insensitive fallback. camelCase word boundaries do
        # not always line up with the parser's snake_case: the mechanical
        # split of `_equipAbleHash` gives `equip_able_hash`, but the real
        # parser field is the one-word `equipable_hash`. Compare on a
        # normalized form that drops underscores and case so the two meet
        # at `equipablehash`. Accept only an unambiguous single match so
        # we never silently pick the wrong field. Found via pinapana's
        # AbyssGearUnlock report on GitHub (#191): 190 _equipAbleHash
        # intents all skipped with "field not in ItemInfo dict", which
        # looked like the 1.11 parser break but was this resolver gap.
        norm = stripped.replace("_", "").lower()
        matches = [k for k in item if k.replace("_", "").lower() == norm]
        if len(matches) == 1:
            return matches[0]
    return None


def is_nested_path(field: str) -> bool:
    """True when a Format 3 ``field`` addresses a nested target."""
    return "." in field or "[" in field


def _resolve_path_target(
    item: dict, path: str,
) -> Optional[tuple]:
    """Walk a Format 3 dotted/indexed path on a parsed ItemInfo dict.

    Path syntax: ``field``, ``field.subfield``, ``field[N]``,
    ``field[N].subfield``, ``a.b[N].c.d``, and dotted integer indices
    (``a.0.b``). Returns ``(parent_container, final_segment)`` so the
    caller can do ``parent[final_segment] = new_value`` (works whether
    parent is a dict and segment is a key, or parent is a list and
    segment is an int index).

    Returns None if any segment fails to resolve (missing dict key,
    list index out of range, type mismatch).

    Used by the iteminfo writer to apply Format 3 intents that
    target nested struct sub-fields. Bug confirmed 2026-05-08
    against gmVIP233 / niyaruza prefab_data_list[N].tribe_gender_list
    and floozo drop_default_data.X paths.

    Dotted integer indices are accepted because the `match` selector
    already accepts them, and a mod author has no way to guess that
    `set` wanted `list[0].x` while `match` wanted `list.0.x`. One
    dialect, both sides.
    """
    import re
    # Tokenize: identifier | [N] | bare integer segment (a.0.b).
    # Identifiers are tried first, so a name like `x2` stays a name.
    tokens: list[tuple[str, object]] = []
    for m in re.finditer(r"([A-Za-z_]\w*)|\[(\d+)\]|(\d+)", path):
        name, br_idx, dot_idx = m.groups()
        if name is not None:
            tokens.append(("key", name))
        else:
            tokens.append(("idx", int(br_idx if br_idx is not None
                                      else dot_idx)))
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


def apply_nested_intent(item: dict, field: str, new) -> str:
    """Assign ``new`` at the nested ``field`` path on a decoded record.

    Returns "ok", "unresolved", or "shape" so the caller can attribute
    the skip. Shared by BOTH iteminfo writers on purpose: the default
    (pre-1.13) path and the 1.13 relocated path must accept exactly the
    same paths, or a mod works on one game version and silently no-ops
    on the other. That divergence is precisely what kept gear stats
    unwritable on 1.13 -- the relocated writer only ever did flat field
    resolution, so every `sharpness_data.*` / `enchant_data_list[*].*`
    intent was dropped as an "unwritable field" even though the record
    decoded perfectly.
    """
    target = _resolve_path_target(item, field)
    if target is None:
        return "unresolved"
    parent, last_seg = target
    try:
        existing = parent[last_seg]
    except (KeyError, IndexError, TypeError):
        existing = None
    if not shape_matches(existing, new):
        return "shape"
    try:
        parent[last_seg] = new
    except (KeyError, IndexError, TypeError):
        return "unresolved"
    return "ok"


# ── Leading-field byte-patch fallback (unsupported game versions) ────
#
# When a game patch shifts the iteminfo record layout the full parser
# doesn't model yet (e.g. CD 1.13 changed prefab_data_list — GitHub
# #247), a full whole-table decode misaligns every record and the
# apply stalls the 180s watchdog. But the record HEADER is stable
# across every version seen: u32 key, length-prefixed string_key,
# u8 is_blocked, u64 max_stack_count. Those fixed-offset leading
# scalars can be patched directly in the record bytes without decoding
# the rest — so stack-size mods (the overwhelmingly common iteminfo
# Format 3 mod) keep working, safely, on versions we can't fully parse.
# The patch is same-width, so record sizes and the .pabgh index are
# untouched and every unrelated byte is preserved.
_LEADING_SCALAR_FIELDS = {
    # canonical_name: (struct_fmt, offset_added_to_string_key_len, width)
    "is_blocked": ("<B", 8, 1),
    "max_stack_count": ("<Q", 9, 8),
}


def _canon_field(name: str) -> str:
    """Normalise a Format 3 field name to compare across the dialects
    the writer accepts (snake_case, camelCase, _schemaCase)."""
    return name.lstrip("_").replace("_", "").lower()


_LEADING_BY_CANON = {_canon_field(k): k for k in _LEADING_SCALAR_FIELDS}


def _iteminfo_record_starts(body: bytes, header: bytes):
    """Return ``(by_key, by_name)`` mapping key/string_key -> record
    start offset, read from the .pabgh index. Reads only each record's
    key and string_key — no field decode — so it is layout-independent
    and fast even when the full schema can't decode the version."""
    from cdumm.semantic.parser import parse_pabgh_index
    _, off = parse_pabgh_index(header, "iteminfo")
    if not off:
        return None, None
    by_key: dict[int, int] = {}
    by_name: dict[str, int] = {}
    n = len(body)
    for s in sorted(off.values()):
        if s + 8 > n:
            continue
        key = struct.unpack_from("<I", body, s)[0]
        by_key.setdefault(key, s)
        sklen = struct.unpack_from("<I", body, s + 4)[0]
        if s + 8 + sklen <= n:
            name = body[s + 8:s + 8 + sklen].decode("utf-8", "replace")
            if name:
                by_name.setdefault(name, s)
    return by_key, by_name


def _schema_supports_version(body: bytes, header: bytes | None) -> bool:
    """Fast probe: does the current ``_ITEM_FIELDS`` schema decode the
    first few records cleanly? A game patch that shifts the layout makes
    this False, and the caller uses the byte-patch fallback instead of
    the full parse (which would misalign every record and stall the
    watchdog). Fails fast — parses at most 5 records, each bounded by
    its .pabgh record boundary."""
    if header is None:
        return True  # no index to frame with; let the full path decide
    try:
        from cdumm.engine.iteminfo_native_parser import parse_record_at
        from cdumm.semantic.parser import parse_pabgh_index
        _, off = parse_pabgh_index(header, "iteminfo")
        starts = sorted(off.values())
    except Exception:
        return True
    if not starts or starts[0] != 0:
        return True
    for i, s in enumerate(starts[:5]):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        try:
            parse_record_at(body, s, end)
        except Exception:
            return False
    return True


def _bytepatch_leading_fields(
    vanilla_body: bytes,
    vanilla_header: bytes | None,
    intents: "list[Format3Intent]",
) -> Optional[dict]:
    """Whole-table writer fallback for game versions the parser schema
    can't fully decode. Copies the table verbatim and patches only
    fixed-offset leading scalar fields in place. Deep-field intents are
    skipped with a clear reason. Returns a v2 whole-file change dict, or
    None if nothing applied."""
    if vanilla_header is None:
        return None
    by_key, by_name = _iteminfo_record_starts(vanilla_body, vanilla_header)
    if by_key is None:
        return None
    buf = bytearray(vanilla_body)
    n = len(buf)
    applied = skipped_deep = skipped_key = skipped_op = 0
    for intent in intents:
        canon = _LEADING_BY_CANON.get(_canon_field(intent.field))
        if canon is None:
            skipped_deep += 1
            continue
        if intent.op != "set":
            skipped_op += 1
            continue
        start = by_key.get(intent.key)
        if start is None and intent.entry:
            start = by_name.get(intent.entry)
        if start is None:
            skipped_key += 1
            continue
        sklen = struct.unpack_from("<I", buf, start + 4)[0]
        fmt, base, width = _LEADING_SCALAR_FIELDS[canon]
        pos = start + base + sklen
        if pos + width > n:
            skipped_key += 1
            continue
        try:
            struct.pack_into(fmt, buf, pos, int(intent.new))
            applied += 1
        except (struct.error, ValueError, TypeError):
            skipped_deep += 1
    if skipped_deep:
        logger.warning(
            "iteminfo: %d intent(s) target fields that need full schema "
            "support for this game version (not yet reverse-engineered); "
            "only fixed-offset leading fields (%s) are byte-patchable "
            "here, so those intents were skipped.",
            skipped_deep, ", ".join(sorted(_LEADING_SCALAR_FIELDS)))
    if applied == 0:
        logger.warning(
            "iteminfo byte-patch fallback: 0 of %d intent(s) applied "
            "(%d deep-field, %d unknown key, %d non-'set' op).",
            len(intents), skipped_deep, skipped_key, skipped_op)
        return None
    patched = bytes(buf)
    if patched == vanilla_body:
        return None
    logger.info(
        "iteminfo byte-patch fallback: %d leading-field intent(s) applied "
        "on a game version the full schema can't decode.", applied)
    tail = (f", {skipped_deep} deep-field skipped)" if skipped_deep else ")")
    return {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": patched.hex(),
        "label": f"iteminfo Format 3 intents (byte-patched, {applied} applied"
                 + tail,
    }


def _build_change_relocated_layout(
    vanilla_body: bytes,
    vanilla_header: bytes | None,
    intents: "list[Format3Intent]",
) -> Optional[dict]:
    """Whole-table writer for a game version whose layout the parser can
    only decode via a relocated-field variant (e.g. CD 1.13 moved
    prefab_data_list/gimmick_visual_prefab_data_list to the record tail).

    Uses ``detect_iteminfo_layout`` to pick the variant, parses (records
    that still don't decode are carried opaque, byte-exact), applies
    intents to decoded records via normal dict edits and to opaque
    records via a fixed-offset leading-scalar byte-patch, then
    serializes. Falls back to the pure byte-patch writer if the variant
    can't round-trip. Returns a v2 whole-file change dict or None."""
    from cdumm.semantic.parser import parse_pabgh_index
    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout, parse_iteminfo_from_bytes,
        serialize_iteminfo,
    )
    if vanilla_header is None:
        return _bytepatch_leading_fields(vanilla_body, vanilla_header, intents)
    _, off = parse_pabgh_index(vanilla_header, "iteminfo")
    if not off:
        return _bytepatch_leading_fields(vanilla_body, vanilla_header, intents)
    record_offsets = sorted(off.values())
    fields = detect_iteminfo_layout(vanilla_body, record_offsets)
    if fields is None:
        # No known relocated layout matched — safest is the raw
        # leading-field byte-patch (never misdecodes).
        return _bytepatch_leading_fields(vanilla_body, vanilla_header, intents)

    items = parse_iteminfo_from_bytes(vanilla_body, record_offsets, fields=fields)
    ident_offsets: dict[int, int] = {}
    try:
        ident = serialize_iteminfo(items, offsets_out=ident_offsets, fields=fields)
    except Exception as e:  # noqa: BLE001
        logger.error("iteminfo(1.13) identity serialize failed: %s", e)
        return _bytepatch_leading_fields(vanilla_body, vanilla_header, intents)
    if ident != vanilla_body:
        logger.warning("iteminfo(1.13) relocated layout did not round-trip; "
                       "falling back to leading-field byte-patch.")
        return _bytepatch_leading_fields(vanilla_body, vanilla_header, intents)

    by_key = {it["key"]: it for it in items}
    by_name = {it["string_key"]: it for it in items
               if isinstance(it.get("string_key"), str) and it.get("string_key")}
    applied = decoded_edits = opaque_patches = 0
    skipped_key = skipped_field = skipped_op = skipped_opaque = 0

    for intent in intents:
        item = by_key.get(intent.key)
        if item is None and intent.entry:
            item = by_name.get(intent.entry)
        if item is None:
            skipped_key += 1
            continue
        if intent.op != "set":
            skipped_op += 1
            continue

        if item.get("_opaque_record"):
            # Record carried verbatim (structure not decoded on this
            # version). Only fixed-offset leading scalars are safely
            # patchable in its raw bytes.
            canon = _LEADING_BY_CANON.get(_canon_field(intent.field))
            if canon is None:
                skipped_opaque += 1
                continue
            b = bytearray(item["bytes"])
            sklen = struct.unpack_from("<I", b, 4)[0]
            fmt, base, width = _LEADING_SCALAR_FIELDS[canon]
            pos = base + sklen
            if pos + width > len(b):
                skipped_field += 1
                continue
            try:
                struct.pack_into(fmt, b, pos, int(intent.new))
                item["bytes"] = bytes(b)
                applied += 1
                opaque_patches += 1
            except (struct.error, ValueError, TypeError):
                skipped_field += 1
            continue

        # Decoded record: normal dict edit with field resolution + shape gate.
        if intent.field in UNWRITEABLE_KNOWN_FIELDS:
            skipped_field += 1
            continue

        # Nested path (dotted / indexed) -- e.g. the gear-stat paths
        # sharpness_data.stat_list[0].change_mb and
        # enchant_data_list[0].enchant_stat_data.stat_list_static[0].change_mb.
        # This branch used to be missing here (it existed only in the
        # default pre-1.13 writer), so on CD 1.13 -- the version the game
        # actually ships -- every nested intent was counted as an
        # "unwritable field" and dropped, even though the record decoded
        # fine. Equipment stats were readable and silently un-editable.
        if is_nested_path(intent.field):
            outcome = apply_nested_intent(item, intent.field, intent.new)
            if outcome == "ok":
                applied += 1
                decoded_edits += 1
            else:
                skipped_field += 1
                logger.warning(
                    "iteminfo(1.13): nested path %r on key=%d skipped (%s)",
                    intent.field, intent.key, outcome)
            continue

        target_field = _resolve_field_name(intent.field, item)
        if target_field is None:
            if intent.field in SUPPORTED_FIELDS:
                target_field = intent.field
            else:
                skipped_field += 1
                continue
        if not shape_matches(item.get(target_field), intent.new):
            skipped_field += 1
            continue
        item[target_field] = intent.new
        applied += 1
        decoded_edits += 1

    if applied == 0:
        logger.warning(
            "iteminfo(1.13): 0 of %d intent(s) applied "
            "(%d unknown key, %d unwritable/opaque field, %d non-set).",
            len(intents), skipped_key,
            skipped_field + skipped_opaque, skipped_op)
        return None

    new_offsets: dict[int, int] = {}
    try:
        new_bytes = serialize_iteminfo(items, offsets_out=new_offsets, fields=fields)
    except Exception as e:  # noqa: BLE001
        logger.error("iteminfo(1.13) serialize failed after edits: %s", e)
        return None
    if new_bytes == vanilla_body:
        return None

    change = {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_bytes.hex(),
        "label": (f"iteminfo Format 3 intents (1.13 relocated layout, "
                  f"{applied} applied: {decoded_edits} decoded, "
                  f"{opaque_patches} byte-patched)"),
    }
    if new_offsets != ident_offsets:
        from cdumm.engine.pabgh_rewrite import rewrite_pabgh_offsets
        new_header = rewrite_pabgh_offsets(vanilla_header, "iteminfo", new_offsets)
        if new_header is None:
            logger.error("iteminfo(1.13): record sizes changed but .pabgh "
                         "rewrite failed; refusing change.")
            return None
        if new_header != vanilla_header:
            change["_pabgh_companion"] = {
                "offset": 0,
                "original": vanilla_header.hex(),
                "patched": new_header.hex(),
                "label": "iteminfo .pabgh offsets (rebuilt for 1.13 edits)",
            }
    return change


def build_iteminfo_intent_change(
    vanilla_body: bytes,
    intents: "list[Format3Intent]",
    vanilla_header: bytes | None = None,
) -> Optional[dict]:
    """Apply all provided intents to a parsed copy of vanilla
    iteminfo.pabgb and return a single whole-file v2 change dict.

    Returns None if no intents touched a real record or all intents
    failed (so the caller can fall back to the regular per-intent
    path).

    Per-intent failures (unknown key, unsupported field) are logged
    and skipped; surviving intents still produce their effect.

    When ``vanilla_header`` (the companion .pabgh bytes) is given
    and the rebuilt table shifted any record offsets, the returned
    change carries a ``_pabgh_companion`` dict: a second whole-file
    change for the .pabgh, with the index offsets rewritten to the
    rebuilt table. Without it, size-changing edits (socket intents
    grow records) ship a table whose index points at stale offsets
    and the game reads garbage entry headers (audit finding A,
    2026-06-10). When the header is given, this function also
    refuses (returns None) if the table size changed but the index
    cannot be rebuilt.
    """
    # Version guard: if the current schema can't decode this game
    # version's records (e.g. CD 1.13 shifted prefab_data_list — GitHub
    # #247), a full whole-table parse misaligns every record and stalls
    # the 180s apply watchdog. Fall back to the leading-field byte-patch
    # writer, which handles the common stack-size mods safely without a
    # full decode. Probe is fast (≤5 records, fails fast).
    if not _schema_supports_version(vanilla_body, vanilla_header):
        logger.warning(
            "iteminfo: default parser schema does not match this game "
            "version (record decode fails) — using the relocated-layout "
            "writer (CD 1.13: prefab/gvp moved to record tail; GitHub "
            "#247). Stackable items decode + edit fully; other items are "
            "carried verbatim with leading-field byte-patch.")
        return _build_change_relocated_layout(
            vanilla_body, vanilla_header, intents)

    # With the .pabgh available, frame records from the authoritative
    # index instead of the sniff heuristic: the heuristic's key
    # ceiling silently swallows real records with large keys
    # (Delesyian_Flag, key 254M, audit finding M12), which makes
    # the index rewrite below refuse.
    record_offsets: "list[int] | None" = None
    if vanilla_header is not None:
        from cdumm.semantic.parser import parse_pabgh_index
        _, idx_offsets = parse_pabgh_index(vanilla_header, "iteminfo")
        if idx_offsets:
            record_offsets = list(idx_offsets.values())
    try:
        items = parse_iteminfo_from_bytes(
            vanilla_body, record_offsets=record_offsets)
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

    # Round-trip pre-flight: serialize the UNMODIFIED parse and
    # require byte equality with vanilla before mutating anything.
    # On a future game version the parser's salvage heuristics may
    # "succeed" lossily; without this gate the writer would then
    # emit a whole-table change that silently rewrites unrelated
    # bytes (audit finding I7, 2026-06-10). A refusal here is a
    # clean "this game version is not supported yet" instead of
    # corruption. The identity offsets double as the .pabgh
    # rewrite validation below.
    ident_offsets: dict[int, int] = {}
    try:
        ident_bytes = serialize_iteminfo(items, offsets_out=ident_offsets)
    except Exception as e:
        logger.error(
            "iteminfo identity serialize failed (%s); refusing to "
            "write this table", e, exc_info=True)
        return None
    if ident_bytes != vanilla_body:
        first_diff = next(
            (i for i in range(min(len(ident_bytes), len(vanilla_body)))
             if ident_bytes[i] != vanilla_body[i]),
            min(len(ident_bytes), len(vanilla_body)))
        logger.error(
            "iteminfo round-trip pre-flight FAILED: identity "
            "serialize differs from vanilla at byte %d "
            "(vanilla %d bytes, serialized %d bytes). The parser "
            "does not model this game version's layout; refusing "
            "to emit a whole-table change that would rewrite "
            "unrelated bytes.", first_diff,
            len(vanilla_body), len(ident_bytes))
        return None
    if vanilla_header is not None:
        from cdumm.engine.pabgh_rewrite import rewrite_pabgh_offsets
        ident_header = rewrite_pabgh_offsets(
            vanilla_header, "iteminfo", ident_offsets)
        if ident_header != vanilla_header:
            logger.error(
                "iteminfo .pabgh pre-flight FAILED: rewriting the "
                "vanilla index with identity offsets did not "
                "reproduce it (%s). Refusing to write this table.",
                "rewrite refused" if ident_header is None
                else "byte mismatch")
            return None

    by_key = {it["key"]: it for it in items}
    # Format 3 dialect contract: "lookup by entry name first, key as
    # fallback". Key-omitted intents arrive with the sentinel key=0,
    # so resolve through the record's string_key when the numeric key
    # misses (mirrors the multichangeinfo/characterinfo writers).
    by_name: dict = {}
    for it in items:
        sk = it.get("string_key")
        if isinstance(sk, str) and sk:
            by_name.setdefault(sk, it)
    applied = 0
    name_resolved = 0
    skipped_op = 0
    skipped_key = 0
    skipped_field = 0
    skipped_shape = 0
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
        item = by_key.get(intent.key)
        if item is None and intent.entry:
            item = by_name.get(intent.entry)
            if item is not None:
                name_resolved += 1
                logger.debug(
                    "iteminfo writer: intent key %r missed, resolved "
                    "by entry name %r (key=%d)",
                    intent.key, intent.entry, item.get("key"))
        if item is None:
            skipped_key += 1
            logger.debug(
                "iteminfo writer: key %d / entry %r not in table, "
                "skipping intent", intent.key, intent.entry)
            continue
        if intent.op != "set":
            # WARNING (not debug) because it's a real intent the mod
            # author wrote that gets silently dropped, bug reports
            # should capture this. Users targeting `max_stack_count
            # op="add" new=10` (relative bump) need to know it ran as
            # nothing.
            skipped_op += 1
            logger.warning(
                "iteminfo writer: op %r not supported (only 'set'); "
                "intent on key=%d field=%r dropped",
                intent.op, intent.key, intent.field)
            continue
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
        if is_nested_path(intent.field):
            outcome = apply_nested_intent(item, intent.field, intent.new)
            if outcome == "ok":
                applied += 1
            elif outcome == "shape":
                skipped_shape += 1
                logger.warning(
                    "iteminfo writer: nested path %r on key=%d has a "
                    "new value whose shape (%s) does not match the "
                    "existing value; skipping intent instead of "
                    "letting serialization fail later",
                    intent.field, intent.key, type(intent.new).__name__)
            else:
                skipped_field += 1
                logger.warning(
                    "iteminfo writer: nested path %r did not resolve "
                    "for key=%d, skipping (segment missing or index "
                    "out of range)", intent.field, intent.key)
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
        # Shape gate (audit 2026-06-11): a malformed `new` used to pass
        # straight into the dict and blow up serialize_iteminfo, which
        # returns None and silently kills EVERY mod batched on this
        # table. Validate against the record's existing value, or for
        # additive/empty lists against the known on-disk element kind.
        existing_val = item.get(target_field)
        shape_ok = shape_matches(existing_val, intent.new)
        if shape_ok and (existing_val is None
                         or (isinstance(existing_val, list)
                             and not existing_val)):
            kind = _LIST_ELEMENT_KINDS.get(target_field)
            if kind is not None:
                shape_ok = (isinstance(intent.new, list)
                            and _elements_match_kind(intent.new, kind))
        if not shape_ok:
            skipped_shape += 1
            logger.warning(
                "iteminfo writer: intent on key=%d field=%r carries a "
                "new value whose shape (%s) does not match the field; "
                "skipping intent instead of letting serialization "
                "fail later", intent.key, intent.field,
                type(intent.new).__name__)
            continue
        try:
            item[target_field] = intent.new
            applied += 1
        except Exception as e:
            logger.warning(
                "iteminfo writer: applying intent on key=%d field=%r "
                "failed: %s", intent.key, intent.field, e)

    if name_resolved:
        logger.info(
            "iteminfo writer: %d intent(s) resolved by entry name "
            "(key missing or not in table)", name_resolved)
    if applied == 0:
        skip_total = (skipped_op + skipped_key + skipped_field
                      + skipped_shape)
        if skip_total:
            logger.warning(
                "iteminfo writer: 0 of %d intent(s) applied "
                "(%d non-'set' op, %d unknown key, %d unknown field, "
                "%d bad value shape). No change emitted.",
                skip_total, skipped_op, skipped_key, skipped_field,
                skipped_shape)
        return None

    new_offsets: dict[int, int] = {}
    try:
        new_bytes = serialize_iteminfo(items, offsets_out=new_offsets)
    except Exception as e:
        logger.error("iteminfo serialize failed: %s", e, exc_info=True)
        return None

    if new_bytes == vanilla_body:
        # Intents resolved but produced no byte difference (e.g.,
        # `set` to the same value). No change to emit.
        return None

    # Companion .pabgh rebuild. Record offsets only move when a
    # record changed size, so compare the offset maps, not the body.
    pabgh_companion: Optional[dict] = None
    if vanilla_header is not None and new_offsets != ident_offsets:
        from cdumm.engine.pabgh_rewrite import rewrite_pabgh_offsets
        new_header = rewrite_pabgh_offsets(
            vanilla_header, "iteminfo", new_offsets)
        if new_header is None:
            logger.error(
                "iteminfo: record offsets shifted (%d bytes size "
                "delta) but the .pabgh index could not be rewritten. "
                "Refusing the whole change; shipping the table alone "
                "would leave the index pointing at stale offsets.",
                len(new_bytes) - len(vanilla_body))
            return None
        pabgh_companion = {
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": new_header.hex(),
            "label": "iteminfo .pabgh offsets (rebuilt for "
                     "size-changed records)",
        }

    skip_total = skipped_op + skipped_key + skipped_field + skipped_shape
    if skip_total:
        skip_summary_parts = []
        if skipped_op:
            skip_summary_parts.append(f"{skipped_op} non-'set' op")
        if skipped_key:
            skip_summary_parts.append(f"{skipped_key} unknown key")
        if skipped_field:
            skip_summary_parts.append(f"{skipped_field} unknown field")
        if skipped_shape:
            skip_summary_parts.append(f"{skipped_shape} bad value shape")
        skip_summary = ", ".join(skip_summary_parts)
        label = (
            f"iteminfo Format 3 intents ({applied} applied, "
            f"{skip_total} skipped: {skip_summary})"
        )
    else:
        label = f"iteminfo Format 3 intents ({applied} applied)"

    change = {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_bytes.hex(),
        "label": label,
    }
    if pabgh_companion is not None:
        change["_pabgh_companion"] = pabgh_companion
    return change
