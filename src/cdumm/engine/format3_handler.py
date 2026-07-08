"""Format 3 (field-names) JSON mod handler.

Format 3 is a high-level semantic mod format that uses entry
names + field names + intent operations instead of raw byte
offsets. Files declare a ``target`` (the .pabgb game data
file they modify) and a list of ``intents``::

    {
      "format": 3,
      "target": "dropsetinfo.pabgb",
      "intents": [
        {"entry": "DropSet_Faction_Graymane",
         "key": 175001,
         "field": "drops",
         "op": "set",
         "new": [...]}
      ]
    }

This module covers parsing + validation. Applying intents to
binary data is handled elsewhere (Phase 2+).

**Phase 1 limitations.** We only classify an intent as supported
when:

  * the ``target`` matches a known PABGB table schema (one of
    434 from ``schemas/pabgb_complete_schema.json``),
  * the ``field`` exactly matches a schema field name,
  * the field has a known fixed-width ``direct_*`` type with a
    non-zero stream size, and
  * the ``op`` is ``"set"``.

Variable-length array fields (e.g., ``_list``), the friendly-
name → schema-name translation layer the upstream tool uses
internally (``drops`` → ``_list``), and ops other than ``"set"`` (``add_entry``,
``remove``, ``append``, etc.) are deferred to later phases.
"""
from __future__ import annotations

import json
import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cdumm.engine.characterinfo_writer import (
    SUPPORTED_FIELDS as _CHARACTERINFO_FIELDS,
)
from cdumm.engine.field_schema import (
    DTYPE_TABLE,
    FieldSchemaEntry,
    load_field_schema,
    locate_field,
)
from cdumm.semantic.parser import get_schema, has_schema, parse_pabgh_index

logger = logging.getLogger(__name__)


_SUPPORTED_OPS = frozenset({"set"})


_raw_schema_cache: dict[str, dict] | None = None


def _raw_field_metadata(table_name: str, field_name: str) -> dict | None:
    """Look up a field in the RAW schema JSON, before parser.py
    drops variable-length fields.

    Needed so we can distinguish "field doesn't exist" from "field
    exists but has stream=None / type=None" — the user-facing
    message is different for each.
    """
    global _raw_schema_cache
    if _raw_schema_cache is None:
        import sys
        candidates = [
            Path(__file__).parent.parent.parent / "schemas"
            / "pabgb_complete_schema.json",
            Path(__file__).parent.parent.parent.parent / "schemas"
            / "pabgb_complete_schema.json",
        ]
        if getattr(sys, "frozen", False):
            candidates.insert(
                0,
                Path(sys._MEIPASS) / "schemas"
                / "pabgb_complete_schema.json",
            )
        for path in candidates:
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8-sig") as f:
                        raw = json.load(f)
                    # Index by lowercase table name for matching
                    # parser.py's lowercase convention.
                    _raw_schema_cache = {
                        k.lower(): v for k, v in raw.items()
                    }
                    break
                except (OSError, ValueError):
                    pass
        if _raw_schema_cache is None:
            _raw_schema_cache = {}

    fields_raw = _raw_schema_cache.get(table_name.lower())
    if not fields_raw:
        return None
    for fr in fields_raw:
        if fr.get("f") == field_name:
            return fr
    return None


@dataclass(frozen=True)
class Format3Intent:
    """A single semantic intent from a Format 3 mod.

    ``old`` is optional and only set for raw-record replacements
    (``_buff_data_raw`` style intents): when both ``old`` and ``new``
    are hex strings, the apply path searches the entry's payload
    for ``old`` bytes and replaces them with ``new``. For regular
    primitive / list intents ``old`` stays None and ``new`` is the
    typed value to set.
    """
    entry: str
    key: int
    field: str
    op: str
    new: Any
    old: str | None = None


@dataclass
class Format3Validation:
    """Result of validating a list of intents against a target's schema.

    ``supported`` intents can be applied by the current Phase.
    ``skipped`` carries the intent + a human-readable reason for
    each one we cannot apply, so the UI can surface every skip
    rather than silently dropping it.
    """
    supported: list[Format3Intent] = field(default_factory=list)
    skipped: list[tuple[Format3Intent, str]] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary listing skip count + distinct reasons.

        Used by the importer to surface a single InfoBar message
        with the actionable details.
        """
        lines: list[str] = []
        if self.supported:
            lines.append(
                f"{len(self.supported)} intent(s) ready to apply"
            )
        if self.skipped:
            # Group identical reasons so the message stays short
            # on a 695-intent mod.
            from collections import Counter
            reasons = Counter(reason for _, reason in self.skipped)
            lines.append(f"{len(self.skipped)} intent(s) skipped:")
            for reason, count in reasons.most_common():
                lines.append(f"  - {count}x {reason}")
        return "\n".join(lines) if lines else "No intents to process"


# ── Parsing ─────────────────────────────────────────────────────────


def _parse_intents_block(
    raw_intents, label: str = "intents",
) -> list[Format3Intent]:
    """Validate a raw intents list and produce Format3Intent objects.

    Shared between the legacy single-target ``intents`` block and
    each per-target block under the newer ``targets: [...]`` shape.
    The error messages name the offending block via ``label`` so the
    user can locate the problem in a multi-target file.
    """
    if not isinstance(raw_intents, list):
        raise ValueError(
            f"Format 3 {label} is missing an intents list"
        )

    intents: list[Format3Intent] = []
    for i, raw in enumerate(raw_intents):
        if not isinstance(raw, dict):
            raise ValueError(
                f"{label} intent #{i} is not a JSON object"
            )
        # The newer skill .field.json variant drops 'op' since 'set'
        # is implicit. We default to 'set' when absent. GitHub #66.
        # GitHub #125 AgentRatchet: DMM v3.1 mods (e.g. Refinement Cost
        # Reforged targeting multichangeinfo.pabgb) ship intents that
        # only carry 'entry' and omit 'key'. The v3.1 spec lists key as
        # required but also explicitly says "Try string_key (entry name)
        # first, then numeric key", which means in practice the entry
        # field is the primary record locator and key is a fallback the
        # mod author may not have populated. Accept missing key when an
        # entry name is present, default it to 0 (apply path looks up by
        # entry name and only falls back to key if the name miss).
        for required in ("entry", "field"):
            if required not in raw:
                raise ValueError(
                    f"{label} intent #{i} is missing required key "
                    f"'{required}'"
                )
        if "new" not in raw:
            raise ValueError(
                f"{label} intent #{i} is missing 'new' "
                f"(the value to set)"
            )
        # ``key`` is the numeric record id. Spec calls it required but
        # in real-world v3.1 exports the entry name takes precedence
        # so a missing key still lets the apply path resolve the
        # record. Default to 0 (sentinel: "no numeric fallback").
        # Booleans pass isinstance(int), so reject explicitly when
        # the field IS present.
        if "key" in raw:
            raw_key = raw["key"]
            if isinstance(raw_key, bool) or not isinstance(raw_key, int):
                raise ValueError(
                    f"{label} intent #{i} has non-integer key "
                    f"{raw_key!r}, key must be an integer record id"
                )
        else:
            raw_key = 0
        # ``old`` is optional and only present on raw-record
        # replacement intents (e.g. _buff_data_raw on skill.pabgb).
        # When present alongside ``new``, both must be hex strings
        # of equal length; the apply path treats them as a literal
        # byte search-and-replace within the entry's payload.
        raw_old = raw.get("old")
        if raw_old is not None and not isinstance(raw_old, str):
            raise ValueError(
                f"{label} intent #{i} has non-string 'old' "
                f"({type(raw_old).__name__}); 'old' must be a hex "
                f"string when present"
            )
        # Parser stays lenient on op (#66 deadriver35 contract): accept
        # any string, default missing op to 'set'. The actual rejection
        # of unsupported ops happens in validate_intents downstream, so
        # one unsupported intent shows up in the per-mod skipped summary
        # instead of taking the whole import down.
        intents.append(Format3Intent(
            entry=str(raw["entry"]),
            key=raw_key,
            field=str(raw["field"]),
            op=str(raw.get("op", "set")),
            new=raw["new"],
            old=raw_old,
        ))
    return intents


# GitHub #135 (Better Unique Gears, Luxxbell): NattKh's exporter
# models a few iteminfo fields as a three-element a/b/c group, e.g.
# it writes cooltime.a / cooltime.b / cooltime.c. CDUMM's iteminfo
# native parser flattens those same three on-disk i64 slots into
# three separate top-level fields. Verified 2026-05-20 against a
# vanilla 1.07.00 iteminfo.pabgb dump: cooltime, unk_post_cooltime_a
# and unk_post_cooltime_b always hold an identical value per record
# (e.g. WeatherWeaver_Necklace = 1800000 in all three), confirming
# they are one logical a/b/c triplet, not the "8-byte zero padding"
# the parser comment originally guessed. Same holds for
# max_charged_useable_count and its two unk_post_max_charged slots.
# Mapping the dotted a/b/c names onto CDUMM's flat field names lets
# the existing flat-field writer handle them with no special-casing.
_ITEMINFO_FIELD_ALIASES: dict[str, str] = {
    "cooltime.a": "cooltime",
    "cooltime.b": "unk_post_cooltime_a",
    "cooltime.c": "unk_post_cooltime_b",
    "max_charged_useable_count.a": "max_charged_useable_count",
    "max_charged_useable_count.b": "unk_post_max_charged_a",
    "max_charged_useable_count.c": "unk_post_max_charged_b",
    # GitHub #171 (pinapana): DMM's exports name socket-equipment
    # fields with the binary-side underscored camelCase
    # (_addSocketMaterialItemList / _socketValidCount / _useSocket).
    # CDUMM's iteminfo parser already knows them under their
    # snake_case path inside the drop_default_data struct
    # (drop_default_data.add_socket_material_item_list etc.) and the
    # iteminfo writer's nested-path resolver handles the dotted form
    # at apply time. Aliasing at parse time makes both the validator
    # and the writer see the canonical dotted form with no per-call
    # special-casing.
    "_addSocketMaterialItemList":
        "drop_default_data.add_socket_material_item_list",
    "_socketValidCount": "drop_default_data.socket_valid_count",
    "_useSocket": "drop_default_data.use_socket",
}


def _apply_field_aliases(
    target: str, intents: list[Format3Intent]
) -> None:
    """Rewrite known dotted alias field names to CDUMM's flat field
    names, mutating the ``intents`` list in place. Currently only the
    iteminfo cooltime / max_charged_useable_count a/b/c triplets
    (GitHub #135). Applied at parse time so the validator and the
    writer both see the canonical flat name and need no per-call
    special-casing.

    Format3Intent is a frozen dataclass, so each aliased entry is
    replaced with a fresh dataclasses.replace() copy rather than
    mutated in place.
    """
    tname = target.lower()
    if not (tname == "iteminfo.pabgb"
            or tname.endswith("/iteminfo.pabgb")):
        return
    import dataclasses
    for i, intent in enumerate(intents):
        canonical = _ITEMINFO_FIELD_ALIASES.get(intent.field)
        if canonical is not None:
            intents[i] = dataclasses.replace(intent, field=canonical)


def parse_format3_mod_targets(
    path: Path,
) -> list[tuple[str, list[Format3Intent]]]:
    """Read a Format 3 file and return one (target, intents) pair per
    target the mod ships.

    Accepts BOTH dialects of the Field-JSON v3 spec:

    * **Singular** (original spec, FIELD_JSON_V3_SPEC.md
      2026-04-24)::

          {"format": 3, "target": "iteminfo.pabgb",
           "intents": [...]}

    * **Plural** (newer multi-target export, e.g. Double Resource
      Buff)::

          {"format": 3,
           "targets": [
             {"file": "buffinfo.pabgb", "intents": [...]},
             {"file": "iteminfo.pabgb", "intents": [...]}
           ]}

    The plural shape is normalized to a list of pairs so apply-time
    code can iterate uniformly. The singular shape returns a 1-pair
    list.

    Raises ``ValueError`` with a user-facing message on any
    structural problem , the importer surfaces those messages
    directly.
    """
    try:
        # utf-8-sig transparently strips a UTF-8 BOM. Mod files
        # authored on Windows in Notepad save with BOM by default.
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"Cannot read Format 3 file: {e}") from e

    if not isinstance(data, dict) or data.get("format") != 3:
        raise ValueError(
            "Not a Format 3 file: missing or wrong "
            "\"format\": 3 marker"
        )

    # format_minor: 1 marks the v3.1 DMM-compatible dialect (targets[]
    # array, multi-table writes). v3.0 documents either omit this key or
    # set it to 0. Log when we see a v3.1 marker so bundles make it
    # obvious which dialect a mod is using. CDUMM accepts both dialects
    # uniformly so the log is purely informational. See
    # CrimsonGameMods/FIELD_JSON_V3_1_SPEC.md.
    format_minor = data.get("format_minor", 0)
    if isinstance(format_minor, int) and format_minor >= 1:
        logger.info(
            "Format 3.%d document accepted from %s",
            format_minor, path.name)

    has_singular = "target" in data
    has_plural = "targets" in data
    if has_singular and has_plural:
        raise ValueError(
            "Format 3 file has BOTH 'target' (singular) and "
            "'targets' (plural) keys, only one shape is allowed"
        )
    if not has_singular and not has_plural:
        raise ValueError(
            "Format 3 file is missing a \"target\" string or a "
            "\"targets\" list "
            "(should name the .pabgb file(s) the mod modifies)"
        )

    if has_singular:
        target = data.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError(
                "Format 3 \"target\" must be a non-empty string "
                "naming the .pabgb file the mod modifies"
            )
        intents = _parse_intents_block(data.get("intents"), label="intents")
        _apply_field_aliases(target, intents)
        return [(target, intents)]

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError(
            "Format 3 \"targets\" must be a list of "
            "{file, intents} entries"
        )
    if not raw_targets:
        raise ValueError(
            "Format 3 \"targets\" list is empty , a mod must "
            "declare at least one target"
        )

    pairs: list[tuple[str, list[Format3Intent]]] = []
    for ti, raw_t in enumerate(raw_targets):
        if not isinstance(raw_t, dict):
            raise ValueError(
                f"targets[{ti}] is not a JSON object"
            )
        file_value = raw_t.get("file")
        if not isinstance(file_value, str) or not file_value:
            raise ValueError(
                f"targets[{ti}] is missing a \"file\" string"
            )
        intents = _parse_intents_block(
            raw_t.get("intents"),
            label=f"targets[{ti}] ({file_value}) intents",
        )
        _apply_field_aliases(file_value, intents)
        pairs.append((file_value, intents))
    return pairs


def parse_format3_mod(path: Path) -> tuple[str, list[Format3Intent]]:
    """Legacy single-target entry point. Returns ``(target, intents)``.

    Multi-target files (``targets: [{file, intents}, ...]``) raise
    ``ValueError`` so callers that haven't migrated don't silently
    drop intents past the first target. Migrate the call site to
    :func:`parse_format3_mod_targets` and iterate.
    """
    pairs = parse_format3_mod_targets(path)
    if len(pairs) > 1:
        raise ValueError(
            f"Multi-target Format 3 file with {len(pairs)} targets "
            f"hit a single-target caller. Use "
            f"parse_format3_mod_targets(path) and iterate the "
            f"returned (target, intents) pairs."
        )
    return pairs[0]


# ── Validation ──────────────────────────────────────────────────────


def _table_name_from_target(target: str) -> str:
    """Strip ``.pabgb`` and normalize to the schema's lowercase key.

    ``parser.has_schema`` looks up by the lowercase filename stem —
    same convention CDUMM uses everywhere else.
    """
    name = target
    if name.lower().endswith(".pabgb"):
        name = name[: -len(".pabgb")]
    return name.lower()


_SUPPORTED_OPS = frozenset({"set"})
_V32_RESERVED_OPS = frozenset({
    "list_set", "list_append", "list_remove", "list_merge",
    # Older mods sometimes used the unprefixed verbs; map them to the
    # same v3.2-reserved bucket so the skip reason is consistent.
    "append", "remove", "merge",
})


def _partition_unsupported_op(
    intent: Format3Intent,
) -> str | None:
    """If the intent uses an op CDUMM cannot apply, return the
    skipped-reason string. Otherwise return None.

    Currently only "set" applies. v3.2-reserved list mutation ops get
    a specific message naming the spec; everything else gets a generic
    "unknown op" reason. RichmondS1337 GitHub #125, in service of
    DMM Field JSON v3.1 compatibility.
    """
    if intent.op in _SUPPORTED_OPS:
        return None
    if intent.op in _V32_RESERVED_OPS:
        return (
            f"intent uses op {intent.op!r} which is reserved for "
            f"Field JSON v3.2 (list mutation). CDUMM only supports "
            f"'set' today. Ask the mod author for a 'set'-shaped "
            f"variant, or wait for the v3.2 ops to land."
        )
    return (
        f"intent uses unknown op {intent.op!r}; CDUMM only "
        f"supports 'set'. If this is a real op from a newer spec, "
        f"file an issue with the mod's JSON attached."
    )


def validate_intents(
    target: str, intents: list[Format3Intent]
) -> Format3Validation:
    """Partition intents into supported (Phase 1 can apply) and
    skipped (Phase 1 cannot, with a per-intent reason).
    """
    result = Format3Validation()
    table_name = _table_name_from_target(target)

    if not has_schema(table_name):
        # Three routes accept intents on a no-PABGB-schema table:
        #   1. (table, field) in LIST_WRITERS — vendored writer handles
        #      the binary layout (e.g. skill.pabgb _useResourceStatList).
        #   2. field is in field_schema/<table>.json — community-curated
        #      tid/offset/type entry, resolves the write position via
        #      locate_field. Added 2026-05-08 to land voiddoiv's
        #      skill.pabgb primitive contribution (Nexus comment).
        #   3. intent has an ``old`` hex string alongside ``new`` —
        #      raw-record byte replacement, anchored by searching for
        #      ``old`` inside the entry's payload bounds. Used by
        #      _buff_data_raw style intents on skill.pabgb where the
        #      mod author ships the full vanilla + modded bytes.
        fs_entries = load_field_schema(table_name)

        def _routable(i: Format3Intent) -> bool:
            # Indexed list paths ('entries[0].etl_hashes') normalize to
            # a wildcard key ('entries[].etl_hashes') so one LIST_WRITERS
            # registration covers every index (#190 equipslotinfo).
            normalized = re.sub(r"\[\d+\]", "[]", i.field or "")
            return (
                (table_name, i.field) in LIST_WRITERS
                or (table_name, normalized) in LIST_WRITERS
                or i.field in fs_entries
                or i.old is not None
            )

        if bool(intents) and all(_routable(i) for i in intents):
            for intent in intents:
                op_reason = _partition_unsupported_op(intent)
                if op_reason is not None:
                    result.skipped.append((intent, op_reason))
                else:
                    result.supported.append(intent)
            return result
        reason = (
            f"target '{target}' has no schema in CDUMM "
            f"(table '{table_name}' not in pabgb_complete_schema.json, "
            f"and field not in field_schema/{table_name}.json)"
        )
        for intent in intents:
            op_reason = _partition_unsupported_op(intent)
            if op_reason is not None:
                result.skipped.append((intent, op_reason))
            elif _routable(intent):
                result.supported.append(intent)
            else:
                result.skipped.append((intent, reason))
        return result

    schema = get_schema(table_name)
    # Map field name → spec for O(1) lookups.
    field_specs = {f.name: f for f in schema.fields}
    # Community-curated field schema (JMM-compatible). Empty if no
    # field_schema/<table>.json exists yet — that's normal.
    fs_entries = load_field_schema(table_name)

    for intent in intents:
        # Unsupported op is checked first; the schema/field walker
        # below assumes op == 'set' and would silently do a set-style
        # write for e.g. op='append' otherwise.
        op_reason = _partition_unsupported_op(intent)
        if op_reason is not None:
            result.skipped.append((intent, op_reason))
            continue
        reason = _classify_intent(
            intent, schema, field_specs, fs_entries, table_name)
        if reason is None:
            result.supported.append(intent)
        else:
            result.skipped.append((intent, reason))

    return result


def _snake_to_camel(name: str) -> str:
    """Convert ``foo_bar_baz`` to ``fooBarBaz``.

    Pure-string transform that mirrors the engine-internal naming
    convention. Underscores between letters become camelCase
    boundaries. Names without underscores pass through unchanged.
    Leading/trailing underscores are preserved (so callers can
    layer the +underscore-prefix step independently).
    """
    if "_" not in name:
        return name
    # Track leading underscores so we can preserve them.
    head = ""
    body = name
    while body.startswith("_"):
        head += "_"
        body = body[1:]
    # Split body on _, lower-case-then-capitalize each subsequent piece.
    parts = body.split("_")
    if not parts:
        return name
    camel = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:] if p)
    return head + camel


# Registry of (table_name, field) tuples that have a custom
# list-writer dispatching to a dedicated module. Add entries here
# as new list-of-dict writers are implemented. Used by both the
# validator (so the field passes classification) and the apply-time
# expander (so the change actually gets emitted).
LIST_WRITERS: dict[tuple[str, str], str] = {
    # Per-record dropset writer (CDUMM native parser).
    ("dropsetinfo", "drops"):
        "dropset_writer.build_drops_replacement_change",
    # Storeinfo whole-table writer (GitHub #183): rebuilds a store's
    # stock list (which can grow) plus the companion .pabgh in one
    # pass. Dispatched via the whole-table branch in format3_apply;
    # registered here so validation accepts the field instead of
    # rejecting it as schema-less.
    ("storeinfo", "stock_data_list"):
        "storeinfo_writer.build_storeinfo_changes",
    ("storeinfo", "_exchangeItemInfoListForSell"):
        "storeinfo_writer.build_storeinfo_changes",
    # Equipslotinfo whole-table writer (GitHub #190): rewrites a
    # record's etl_hashes list + the companion .pabgh. The wildcard
    # key matches 'entries[N].etl_hashes' for any N via the indexed-
    # path normalization in validate_intents.
    ("equipslotinfo", "entries[].etl_hashes"):
        "equipslotinfo_writer.build_equipslotinfo_changes",
    # Iteminfo whole-table writer (CDUMM native parser).
    # The full list of iteminfo list-of-dict fields the writer
    # accepts is in `iteminfo_writer.SUPPORTED_FIELDS`. We mirror the
    # commonly-used ones here so validation accepts them; the writer
    # itself is the source of truth for what's actually applicable.
    ("iteminfo", "enchant_data_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "equip_passive_skill_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "occupied_equip_slot_data_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "item_tag_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "consumable_type_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "item_use_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "item_icon_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "sealable_item_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "sealable_character_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "sealable_gimmick_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "sealable_gimmick_tag_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "sealable_tribe_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "sealable_money_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "transmutation_material_gimmick_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "transmutation_material_item_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "transmutation_material_item_group_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "multi_change_info_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    ("iteminfo", "gimmick_tag_list"):
        "iteminfo_writer.build_iteminfo_intent_change",
    # Skill whole-table writer (vendored skillinfo_parser).
    ("skill", "_useResourceStatList"):
        "skill_writer.build_skill_intent_change",
    ("skill", "_buffLevelList"):
        "skill_writer.build_skill_intent_change",
}


def _diagnose_unsupported_intent(
    field: str, new_value, table_name: str = "",
) -> str | None:
    """Return a clear "not yet supported" message for Format 3
    intent shapes that can't be written, OR None if the intent is
    supported (either as a primitive or via a registered list writer).

    Two unsupported shapes:
      1. Dotted-path fields (``parent.child``): nested struct walking
         not implemented yet.
      2. List-of-dicts values where (table, field) is NOT in
         LIST_WRITERS: needs a dedicated serializer per table.
    """
    if "." in (field or ""):
        # buffinfo has a clean-room item-path resolver
        # (locate_buff_field) that handles ``buff_data_list[N].xxx``
        # and ``buff_data_list[N].data.base.X`` paths. Don't reject
        # those at validation , the apply-time helper does the real
        # resolution and emits zero-bytes-cleanly when an item is
        # behind an unknown variant tag.
        tn = (table_name or "").lower().replace(".pabgb", "")
        if tn == "buffinfo" and (field or "").startswith(
                "buff_data_list["):
            return None
        # iteminfo: the native writer's path resolver handles known
        # nested-path shapes. Don't reject these at validation, the
        # writer walks the parsed item dict and emits the change.
        # Bug confirmed 2026-05-08 against
        # gmVIP233's Marni_Devotee_PlateArmor_Helm
        # (prefab_data_list[N].tribe_gender_list), niyaruza's
        # kliff_Wears_Damiane_Armor (same path), and floozo's cloak
        # (drop_default_data.add_socket_material_item_list,
        # drop_default_data.use_socket).
        if tn == "iteminfo":
            f = field or ""
            if (f.startswith("prefab_data_list[")
                    or f.startswith("drop_default_data.")
                    or f.startswith("gimmick_visual_prefab_data_list[")
                    # GitHub #135: docking_child_data.<subfield> resolves
                    # via the iteminfo writer's nested-path walker.
                    or f.startswith("docking_child_data.")):
                return None
        # GitHub #125 (Refinement Cost Reforged): multichangeinfo
        # fixed_material_data_list[N].item_info / .count are resolved
        # by the multichangeinfo writer's element patcher at apply
        # time, not by the generic nested-struct walker.
        if tn == "multichangeinfo" and (field or "").startswith(
                "fixed_material_data_list["):
            return None
        # GitHub #150 (Female Animations): characterinfo
        # upper_chart.group_lookup / lower_chart.group_lookup are
        # resolved by the clean-room characterinfo writer.
        if tn == "characterinfo" and field in (
                "upper_chart.group_lookup", "lower_chart.group_lookup"):
            return None
        return (
            f"field '{field}' targets a nested struct sub-field "
            f"(dotted path). Format 3 nested-field writes are not "
            f"implemented for this field yet. Ask the mod author "
            f"to flatten this intent or use the byte-offset JSON "
            f"variant if available."
        )
    if isinstance(new_value, list) and new_value and isinstance(
            new_value[0], dict):
        # Table-specific list-writer registered? Allow.
        tn = (table_name or "").lower().replace(".pabgb", "")
        if (tn, field) in LIST_WRITERS:
            return None
        return (
            f"field '{field}' is a variable-length list-of-dicts "
            f"(e.g. enchant_data_list, equip_passive_skill_list). "
            f"This table doesn't have a list writer yet. Other "
            f"intents in the same mod still apply."
        )
    return None


def _classify_intent(
    intent: Format3Intent, schema, field_specs: dict,
    fs_entries: dict, table_name: str
) -> str | None:
    """Return ``None`` if the intent is supported, otherwise a
    human-readable reason for skipping it. See _diagnose_unsupported_intent
    for the deferred-feature catalogue.

    Resolution order: field_schema (community-curated) takes
    precedence over the PABGB engine schema, mirroring JMM's
    design where field_schema is the override layer mod authors
    target. Fall back to PABGB schema for direct matches against
    underscored engine names (``_dropRollCount`` etc.).

    For PABGB-schema fallbacks: the validator must verify the
    field's byte offset is *actually computable* via the same
    walk the writer uses — otherwise a flat field that sits
    after a variable-length field passes validation and silently
    no-ops at apply time.
    """
    if intent.op not in _SUPPORTED_OPS:
        return (
            f"op '{intent.op}' not supported in Phase 1 "
            f"(only 'set' is implemented)"
        )

    # A malformed mod JSON can produce field=None / field=42 /
    # field="" — every downstream lookup expects a non-empty
    # string. Surface a clean per-intent skip so other intents in
    # the same mod still apply. Round-14 systematic-debugging.
    if not isinstance(intent.field, str) or not intent.field:
        return (
            f"intent has no `field` name set "
            f"(got {type(intent.field).__name__}={intent.field!r}); "
            f"every Format 3 intent must specify which field to write"
        )

    # Format 3 nested writes (dotted paths and list-of-dicts) are
    # deferred to v3.3. Catch them here so users get a clear
    # message instead of a misleading "add a field_schema entry"
    # (which they can't — these need writer-side support, not
    # schema-side metadata).
    nested_msg = _diagnose_unsupported_intent(
        intent.field,
        getattr(intent, "new", None),
        table_name=table_name,
    )
    if nested_msg:
        return nested_msg

    # buffinfo nested-item paths (``buff_data_list[N].xxx``) are
    # resolved by the clean-room buffinfo parser at apply time, not
    # via field_schema or PABGB schema. Accept them up front so the
    # rest of the lookup chain doesn't reject them as "no field_schema
    # entry". The apply helper drops intents that don't actually
    # resolve, so a typo in the nested path produces a clean
    # "0 byte changes" warning rather than a misleading
    # "add a field_schema entry" instruction the author can't act on.
    tn_norm = (table_name or "").lower().replace(".pabgb", "")
    if tn_norm == "buffinfo" and intent.field.startswith(
            "buff_data_list["):
        return None

    # iteminfo nested-item paths (``prefab_data_list[N].xxx``,
    # ``drop_default_data.xxx``, ``gimmick_visual_prefab_data_list
    # [N].xxx``) are resolved by the iteminfo native writer's
    # path-walker at apply time, not via field_schema or PABGB
    # schema. The v3.2.11 fix added these to
    # ``_diagnose_unsupported_intent``'s whitelist so they're not
    # rejected as "nested writes not implemented", but the
    # validator still continued to field_specs lookup which fails
    # for nested paths and emitted a misleading "no field_schema
    # entry, author needs to add one" message. Bug reported by
    # helmysaini, niyaruza, cajae 2026-05-09 against
    # kliff_Wears_Damiane_Armor_Update_1.05.01.json on v3.2.13.
    # Mirror the buffinfo early-accept above so these intents reach
    # the apply-time path-walker.
    if tn_norm == "iteminfo" and (
        intent.field.startswith("prefab_data_list[")
        or intent.field.startswith("drop_default_data.")
        or intent.field.startswith("gimmick_visual_prefab_data_list[")
        # GitHub #135 (Better Unique Gears): docking_child_data is an
        # `optional` struct in the iteminfo schema, so the schema
        # walker reports stream_size=0 and rejects it as
        # variable-length. The iteminfo writer's nested-path resolver
        # (iteminfo_writer.py _resolve_path_target) handles both the
        # bare `docking_child_data` whole-struct set and the dotted
        # `docking_child_data.<subfield>` form, so early-accept here
        # lets those intents reach the writer. Extra struct keys the
        # mod ships (inherit_summoner, summon_tag_name_hash) that the
        # 1.07.00 binary does not carry are simply ignored by
        # _write_DockingChildData. Verified the iteminfo round-trip is
        # byte-perfect so the struct layout is correct.
        or intent.field == "docking_child_data"
        or intent.field.startswith("docking_child_data.")
        # Faisal 2026-05-12 GitHub #99 (paloroycevincent-sketch /
        # Combat God's Plate Gloves): the iteminfo native writer has
        # explicit byte-perfect round-trip support for these three
        # primitive fields (iteminfo_writer.py:228 comment, verified
        # against all 6235 vanilla records), but the validator's
        # schema-walker reachability check rejected them because a
        # preceding variable-length field has no walker descriptor.
        # The writer handles them via _resolve_field_name into the
        # parsed item dict, so early-accept here bypasses the walker
        # check and lets the apply path do its job.
        or intent.field in {
            "cooltime",
            "unk_post_cooltime_a",
            "unk_post_cooltime_b",
        }
        # GitHub #191 (AbyssGearUnlock, pinapana): equipable_hash is a
        # primitive u32 the iteminfo writer round-trips byte-exact and
        # resolves across separator/case variants (equipable_hash,
        # _equipAbleHash) via _resolve_field_name. The schema walker
        # cannot reach it (a preceding variable-length field has no
        # descriptor), so every intent was skipped at import and the
        # mod produced 0 byte changes even after the writer learned the
        # field name. Early-accept the normalized name so the apply path
        # reaches the writer, which round-trip-guards before committing.
        or intent.field.replace("_", "").lower() == "equipablehash"
        # Gear stats (armor defense / weapon damage / AbyssGear values):
        # gear_stat[<statkey>] targets an EnchantStatChange value inside the
        # opaque equipment tail. The iteminfo writer locates it structurally
        # and overwrites the i64 same-width (byte-exact); records that don't
        # carry that stat drop cleanly at apply time.
        or intent.field.startswith("gear_stat[")
    ):
        return None

    # GitHub #125 (Refinement Cost Reforged): multichangeinfo
    # fixed_material_data_list[N].item_info / .count intents are
    # resolved by the multichangeinfo writer at apply time
    # (multichangeinfo_writer.build_multichangeinfo_changes). The PABGB
    # schema walker can't reach the array - several fields before it
    # are variable-length - so early-accept here lets these intents
    # reach the writer. Intents on records whose array the writer
    # cannot locate are dropped cleanly at apply time with a logged
    # warning, mirroring the iteminfo nested-path early-accept above.
    if tn_norm == "multichangeinfo" and intent.field.startswith(
            "fixed_material_data_list["):
        return None

    # GitHub #224 (Female Armor Module): stringinfo's _buffer is a
    # length-prefixed variable-length string the PABGB schema drops
    # (stream=None), so the generic walker can't write it. The clean-room
    # stringinfo writer (stringinfo_writer.build_stringinfo_changes)
    # locates the record by key, rewrites the buffer, and rebuilds the
    # companion .pabgh offsets. Accept the DMM name 'buffer' and the
    # engine name '_buffer' so these whole-table intents reach the
    # writer; intents whose key isn't in the table are dropped cleanly at
    # apply time with a logged warning. The value must be a string, which
    # the writer re-checks before committing.
    if tn_norm == "stringinfo" and intent.field.lstrip("_").lower() == (
            "buffer") and isinstance(getattr(intent, "new", None), str):
        return None

    # GitHub #150 (Female Animations) + #192 (mesh swap): characterinfo's
    # PABGB schema is a positional name-less decompiled structure, so the
    # schema walker can't resolve these field names. The clean-room
    # characterinfo writer (characterinfo_writer.build_characterinfo_changes)
    # locates them by walking each record to the action-chart block. The
    # accept-set is the writer's own SUPPORTED_FIELDS so the two can never
    # drift apart (the recurring two-spot edit that #150 flagged and #192
    # tripped over).
    if tn_norm == "characterinfo" and intent.field in _CHARACTERINFO_FIELDS:
        return None

    # List writer dispatch: this (table, field) pair has a registered
    # serializer (e.g. dropsetinfo.drops). The validator must accept
    # the intent so the apply-time expander can land the bytes.
    if (tn_norm, intent.field) in LIST_WRITERS:
        return None

    # Community-curated field_schema wins
    if intent.field in fs_entries:
        return None

    # Prefix + camelCase fallback lookup. Field-names mods use
    # snake_case field names without the underscore prefix. The
    # schema/overrides use camelCase WITH the prefix (engine-internal
    # form, e.g. `_gimmickInfo`). Try four shapes in order:
    #   1. exact (user's `cooltime`)
    #   2. +underscore  (user's `cooltime` → schema `_cooltime`)
    #   3. snake→camel  (user's `gimmick_info` → `gimmickInfo`)
    #   4. snake→camel +underscore (`gimmick_info` → `_gimmickInfo`)
    # Originally just (1)+(2): NoCooldownForALLItems was the trigger
    # (commit 7c9fb05). Round-5 systematic-debugging found Matrixz's
    # mod hits (4) for `gimmick_info` and `item_charge_type`.
    candidate_names = [intent.field, f"_{intent.field}"]
    if "_" in intent.field:
        camel = _snake_to_camel(intent.field)
        if camel != intent.field:
            candidate_names.extend([camel, f"_{camel}"])
    spec = None
    target_field_name = intent.field
    for name in candidate_names:
        if name in field_specs:
            spec = field_specs[name]
            target_field_name = name
            break
    if spec is None:
        # parser.py drops variable-length fields (stream=None) from
        # the loaded schema, so the field-not-found path can't tell
        # "really doesn't exist" from "exists but is an array we
        # can't write yet". Hit the raw schema for a better message.
        # Try the same name candidates the field_specs lookup used,
        # so an engine-internal underscore/camelCase name (user
        # 'buffer' -> schema '_buffer') resolves to its raw metadata
        # and yields the accurate variable-length message instead of a
        # misleading "add a field_schema entry" the author cannot act
        # on for a variable-length field (#224).
        raw = None
        raw_name = intent.field
        for _cand in candidate_names:
            _meta = _raw_field_metadata(table_name, _cand)
            if _meta is not None:
                raw, raw_name = _meta, _cand
                break
        if raw is not None and (
            raw.get("stream") is None
            or raw.get("type") is None
        ):
            return (
                f"field '{raw_name}' is a variable-length / "
                f"array field (stream=None in schema); writing "
                f"variable-length data lands in a later phase"
            )
        return (
            f"field '{intent.field}' has no field_schema entry "
            f"and isn't in the PABGB record schema. "
            f"Author needs to add a field_schema/{table_name}.json "
            f"entry mapping '{intent.field}' to a tid or rel_offset"
        )

    # Variable-length / unknown-layout fields: stream None or 0.
    if not spec.stream_size:
        return (
            f"field '{intent.field}' is variable-length or has "
            f"unknown binary layout (stream_size={spec.stream_size}); "
            f"writing arrays / variable-length data lands in a "
            f"later phase"
        )

    # Tagged-primitive fields (direct_13B, direct_15B): stream is
    # set but the actual numeric value lives at an unknown offset
    # inside the tagged bytes. Phase 1 does not write these.
    if spec.struct_fmt is None:
        return (
            f"field '{intent.field}' uses Pearl Abyss tagged-"
            f"primitive format ({spec.field_type}, "
            f"{spec.stream_size}B); writing into tagged primitives "
            f"needs a TID-based field schema and lands in Phase 2"
        )

    # Final check: the apply pipeline reaches a field by walking
    # the schema, consuming each preceding field's bytes. With
    # Path B's pabgb_types walker, fields with a known descriptor
    # (CArray, CString, COptional, sub-structs, tagged variants)
    # are also reachable. Fields preceded only by truly-unknown
    # layouts (no fixed size AND no descriptor) still fail here.
    if not _field_walker_reachable(schema, target_field_name):
        return (
            f"field '{intent.field}' has a preceding variable-"
            f"length field with unknown binary layout (no fixed "
            f"size and no walker-known type descriptor). Author "
            f"can add a field_schema entry with rel_offset or tid "
            f"to bypass the schema walk, or extend "
            f"schemas/pabgb_type_overrides.json with a descriptor "
            f"for the blocking field."
        )

    return None


# ── Apply (records-dict level) ──────────────────────────────────────


def apply_intents_to_records(
    vanilla_records: dict[int, dict[str, Any]],
    intents: list[Format3Intent],
) -> dict[int, dict[str, Any]]:
    """Synthesize the "mod records" dict for the existing semantic
    engine pipeline.

    Returns ONLY records that an intent successfully modified — mirroring
    what the differ expects from a real mod (a mod's PABGB only contains
    records the mod authored values for; vanilla-equal records are
    absent from the diff input).

    Out-of-band intents are dropped silently here. The upstream
    ``validate_intents`` call has already classified them and surfaced
    reasons through ``Format3Validation.skipped``.
    """
    out: dict[int, dict[str, Any]] = {}
    for intent in intents:
        # Phase 1 only writes 'set'. Other ops would change the record
        # shape (add_entry, remove) or compose values (append) — neither
        # belongs at this layer.
        if intent.op not in _SUPPORTED_OPS:
            continue
        if intent.key not in vanilla_records:
            continue
        vanilla_rec = vanilla_records[intent.key]
        if intent.field not in vanilla_rec:
            # Don't invent fields — the differ would treat a phantom
            # field as a real diff, and the rebuilder would have no
            # schema slot for it.
            continue
        if intent.key not in out:
            out[intent.key] = dict(vanilla_rec)  # shallow copy
        out[intent.key][intent.field] = intent.new
    return out


# ── Apply (binary level) ────────────────────────────────────────────


def _entry_payload_offset(body: bytes, entry_offset: int,
                          key_size: int) -> int | None:
    """Return the absolute byte offset where the payload starts
    inside the entry at ``entry_offset``.

    Entry header is ``[entry_id of key_size bytes] + [u32 name_len]
    + name UTF-8 + 0x00``. The entry_id width must match the
    PABGH index ``key_size`` (u16 for storeinfo / inventory style
    tables, u32 for dropsetinfo / iteminfo style tables) — read
    the wrong width and name_len comes out garbage, the bounds
    check trips, and we end up writing at the wrong byte offset.
    """
    eid_size = 2 if key_size == 2 else 4
    head_size = eid_size + 4
    if entry_offset + head_size > len(body):
        return None
    name_len = struct.unpack_from("<I", body, entry_offset + eid_size)[0]
    if name_len > 500 or entry_offset + head_size + name_len > len(body):
        return None
    name_end = entry_offset + head_size + name_len
    # Single byte null terminator after the name
    if name_end < len(body) and body[name_end] == 0:
        return name_end + 1
    return name_end


def _field_offset_in_payload(
    schema, target_field: str
) -> tuple[int, "FieldSpec"] | None:
    """Walk the schema's flat fields up to ``target_field``, summing
    their stream sizes. Returns ``(offset_in_payload, spec)`` or
    None if the field doesn't exist or any preceding field has
    unknown layout (which would invalidate the offset).

    Static-only walker — returns None as soon as any preceding field
    is variable-length (stream_size=0). The validator uses
    :func:`_field_walker_reachable` for the relaxed Path B check.
    """
    offset = 0
    for spec in schema.fields:
        if spec.name == target_field:
            return offset, spec
        if not spec.stream_size:
            # Variable-length / unknown field before our target
            # invalidates the static byte offset.
            return None
        offset += spec.stream_size
    return None


def _field_walker_reachable(schema, target_field: str) -> bool:
    """Return True when the runtime byte walker can reach ``target_field``
    at apply time. Path B added: a preceding field with a known
    ``type_descriptor`` counts as walkable even when ``stream_size=0``.

    The validator uses this to surface "supported" for fields like
    ``_cooltime`` whose static offset can't be summed but whose
    runtime offset is computable from the typed schema + body bytes
    (handled by ``format3_apply._consume_field_bytes``).

    Accepts both the raw mod-naming form (``cooltime``) and the
    underscore-prefixed CDUMM-internal form (``_cooltime``) — falls
    back to the prefixed variant if the unprefixed one isn't found.
    """
    from cdumm.semantic.pabgb_types import is_known_type
    # Build a candidate list: try the requested name first, then the
    # underscore-prefixed variant (mod-naming → schema-naming).
    candidates = [target_field]
    if not target_field.startswith("_"):
        candidates.append(f"_{target_field}")
    for spec in schema.fields:
        if spec.name in candidates:
            return True
        if spec.stream_size:
            continue
        if spec.type_descriptor and is_known_type(spec.type_descriptor):
            continue
        # Truly unknown — neither a fixed size nor a walker-known type.
        return False
    return False


def _pack_value(value, spec) -> bytes | None:
    """Pack a Python value into the field's binary format.

    Returns the bytes to write, or None if the value can't fit
    or the field doesn't have a known struct format.
    """
    if not spec.struct_fmt:
        return None
    try:
        return struct.pack(f"<{spec.struct_fmt}", value)
    except struct.error:
        # Out-of-range, wrong type, etc. Caller's job to refuse
        # rather than corrupting bytes.
        return None


def apply_intents_to_pabgb_bytes(
    table_name: str,
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list[Format3Intent],
) -> bytes:
    """Apply each supported intent directly to ``vanilla_body``,
    returning the modified bytes.

    Phase 1 supports flat fixed-width fields only. An intent is
    silently ignored when:

      * the table has no schema, or
      * the intent's key isn't in the PABGH index, or
      * the intent's op is not 'set', or
      * the intent's field isn't a known fixed-width schema field
        with no preceding variable-length field, or
      * the value won't fit in the field's binary format.

    Use ``validate_intents`` upstream to surface skip reasons to
    the user — this writer is the apply step for already-validated
    intents and won't itself produce diagnostics.
    """
    if not has_schema(table_name):
        return vanilla_body

    schema = get_schema(table_name)
    key_size, offsets = parse_pabgh_index(vanilla_header, table_name)
    if not offsets:
        return vanilla_body
    # parse_pabgh_index derives key_size from arithmetic on the
    # header layout — it can yield 1, 3, 5, 6, 7, 8 from a
    # truncated or malformed header. The entry-header parser only
    # handles u16 (2) and u32 (4) widths. Refuse the apply for
    # anything else; silently misaligning every payload is worse
    # than not applying.
    if key_size not in (2, 4):
        logger.warning(
            "Format 3 apply on '%s' refused: unsupported PABGH "
            "key_size=%d (only 2 or 4 are handled)",
            table_name, key_size)
        return vanilla_body

    body = bytearray(vanilla_body)
    field_specs = {f.name: f for f in schema.fields}
    fs_entries = load_field_schema(table_name)

    # Compute (start, end) bounds per record so the TID search has
    # an upper limit and won't match the next entry's bytes.
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    entry_bounds: dict[int, tuple[int, int]] = {}
    for idx, (k, off) in enumerate(sorted_offs):
        end = (sorted_offs[idx + 1][1]
               if idx + 1 < len(sorted_offs) else len(body))
        entry_bounds[k] = (off, end)

    for intent in intents:
        if intent.op not in _SUPPORTED_OPS:
            continue
        if intent.key not in entry_bounds:
            continue

        entry_off, entry_end = entry_bounds[intent.key]
        payload_off = _entry_payload_offset(body, entry_off, key_size)
        if payload_off is None:
            continue

        # Try community field_schema first (TID or rel_offset).
        fs_entry = fs_entries.get(intent.field)
        if fs_entry is not None:
            written = _apply_via_field_schema(
                body, fs_entry, payload_off, entry_end, intent.new)
            if written:
                continue
            # field_schema entry was present but write failed (TID
            # not found, value out of range, etc.). Don't fall back
            # to PABGB schema — the author meant the field_schema
            # path; falling back would silently target a different
            # field.
            continue

        # Fall back to PABGB schema for direct field-name match.
        if intent.field not in field_specs:
            continue
        located = _field_offset_in_payload(schema, intent.field)
        if located is None:
            continue
        rel_off, spec = located
        abs_off = payload_off + rel_off

        packed = _pack_value(intent.new, spec)
        if packed is None or len(packed) != spec.stream_size:
            continue
        # Bound to THIS entry's end, not the whole body. Real game
        # entries are sometimes truncated when trailing fields hold
        # default values, so a schema-computed offset can validly
        # land past the entry's own end. Without this check, the
        # write spills into the next entry's bytes.
        if abs_off + spec.stream_size > entry_end:
            continue

        body[abs_off:abs_off + spec.stream_size] = packed

    return bytes(body)


# ── field_schema apply helpers ──────────────────────────────────────


def _apply_via_field_schema(
    body: bytearray, entry: FieldSchemaEntry,
    payload_off: int, entry_end: int, new_value
) -> bool:
    """Try writing ``new_value`` using a field_schema entry.

    Returns True on a successful write, False if the entry can't
    be applied (TID missing, value out of range, write would
    overflow entry bounds). The data_type was validated at load
    time so the dtype lookup here is guaranteed to hit, but check
    defensively in case someone constructed a FieldSchemaEntry
    by hand without going through the loader.
    """
    fmt_size = DTYPE_TABLE.get(entry.data_type.lower())
    if fmt_size is None:
        return False
    fmt, size = fmt_size

    # locate_field uses [blob_start, blob_end) — payload is the
    # entry's blob from JMM's perspective.
    abs_off = locate_field(
        bytes(body), payload_off, entry_end, entry)
    if abs_off is None:
        return False
    if abs_off + size > entry_end:
        return False

    try:
        packed = struct.pack(f"<{fmt}", new_value)
    except struct.error:
        return False

    body[abs_off:abs_off + size] = packed
    return True
