"""Format 3 → v2 expansion for the apply pipeline (Phase 4 / Option B).

CDUMM's existing v2 mount-time apply path runs through
``aggregate_json_mods_into_synthetic_patches`` in apply_engine.py.
That function builds an ``aggregated[game_file] -> list[change]``
dict from every enabled v2 mod's stored JSON. Format 3 mods don't
have ``patches`` keys, so they contribute nothing through that
path — even though Phase 1-3's writer can resolve their intents
into the same shape of byte changes.

This module bridges the gap. ``expand_format3_into_aggregated()``
reads each enabled mod whose ``json_source`` points at a Format 3
file, extracts vanilla bytes for the target ``.pabgb``, resolves
each supported intent into a v2-style change dict
(``{entry, rel_offset, original, patched}``), and APPENDS the
results to the same ``aggregated`` dict.

Design invariants:

  * Existing v2 entries in ``aggregated`` are never modified —
    only appended to.
  * Mods with no resolvable intents do NOT create empty
    ``aggregated[game_file] = []`` entries.
  * Vanilla extraction failures, malformed JSON, and unsupported
    PABGH key_sizes log at warning and skip that mod, never
    raising — the apply pipeline must always complete.
  * key_size guard mirrors apply_intents_to_pabgb_bytes (only 2
    or 4 are supported; anything else means a malformed header
    or a table layout we don't know).

Single-line wire-up in apply_engine.py is the next commit.
"""
from __future__ import annotations

import json
import logging
import struct
from pathlib import Path
from typing import Callable

from cdumm.engine.field_schema import (
    DTYPE_TABLE,
    FieldSchemaEntry,
    load_field_schema,
    locate_field,
)
from cdumm.engine.format3_handler import (
    Format3Intent,
    parse_format3_mod,
    parse_format3_mod_targets,
    validate_intents,
)
from cdumm.semantic.parser import (
    get_schema,
    has_schema,
    identify_table_from_path,
    parse_pabgh_index,
)

logger = logging.getLogger(__name__)


VanillaExtractor = Callable[[str], "tuple[bytes, bytes] | None"]
"""Callable that takes a game_file path and returns (body, header)
bytes for the vanilla version of that file, or None if the file
can't be extracted. apply_engine wires this to its existing
``_get_vanilla_entry_content`` + ``_extract_sibling_entry`` helpers."""


def expand_format3_into_aggregated(
    aggregated: dict[str, list[dict]],
    signatures: dict[str, str],
    db,
    vanilla_extractor: VanillaExtractor,
    warnings_out: list[str] | None = None,
    participating_mod_ids: set | None = None,
) -> None:
    """For each enabled mod whose json_source is a Format 3 file,
    resolve its intents into v2-style change dicts and append to
    ``aggregated[target]``.

    Mutates ``aggregated`` and ``signatures`` in place. Never raises.
    Logs at warning on extraction failures, malformed JSON, and
    unsupported tables; logs at debug on successful expansion.

    When ``warnings_out`` is provided, appends per-mod user-facing
    messages for cases the user needs to see (zero supported intents,
    vanilla extraction failure). The apply_engine wire-up routes
    these through the existing ``warning`` signal so on_apply_done
    renders them in the post-apply InfoBar — same surfacing the
    JMM-parity skipped-patches feature uses.
    """
    rows = db.connection.execute(
        "SELECT id, name, json_source, priority FROM mods "
        "WHERE enabled = 1 AND json_source IS NOT NULL "
        "AND json_source != '' "
        "ORDER BY priority DESC, id ASC"
    ).fetchall()

    # Counters for the apply-time summary log line. INFO-level so
    # bug reports include it automatically — addresses the DX
    # review's "no measurement" finding.
    n_mods_processed = 0
    n_mods_changed = 0
    n_mods_skipped = 0
    n_bytes_changed = 0
    files_touched: set[str] = set()

    # Cross-mod accumulator for whole-table writer targets (iteminfo,
    # skill). Each whole-table writer emits ONE byte change covering
    # the entire .pabgb body, so per-mod dispatch can't compose: when
    # mod_A and mod_B both target iteminfo.pabgb, applying A then B
    # would reset the buffer to vanilla via the apply path's
    # "vanilla-remnant" recovery branch and discard A's edits. Fix:
    # collect intents from ALL mods first, dispatch once with the
    # union, emit a single change. Bug from systematic-debugging
    # round on test_iteminfo_multi_mod_compose.
    _WHOLE_TABLE_TARGETS = {"iteminfo.pabgb", "skill.pabgb"}
    whole_table_intents: dict[str, list] = {}
    whole_table_mod_names: dict[str, list[str]] = {}
    # Track contributing mod ids per whole-table target so the
    # participating_mod_ids set picks them up on a successful
    # batch dispatch , H2 fix for Format 3 mods that go through
    # the whole-table path.
    whole_table_mod_ids: dict[str, list[int]] = {}

    for mod_id, mod_name, json_source, _priority in rows:
        try:
            jp = Path(json_source)
            if not jp.exists():
                continue
            # parse_format3_mod_targets returns one (target, intents)
            # pair per .pabgb file the mod modifies. Singular-shape
            # files yield a 1-pair list; multi-target files (newer
            # dialect, e.g. Double Resource Buff) yield one pair per
            # ``targets[i]`` entry.
            target_pairs = parse_format3_mod_targets(jp)
        except (ValueError, OSError):
            # Either not Format 3 (v2 path already handled it), or
            # the file is malformed. Either way, skip silently —
            # the v2 aggregator already logged its own parse errors
            # for the same file.
            continue

        # Confirmed Format 3 mod from here on — count it once per mod,
        # not per target.
        n_mods_processed += 1

        for target, intents in target_pairs:
            # Validate intents against the schema + community field_schema
            validation = validate_intents(target, intents)
            if not validation.supported:
                n_mods_skipped += 1
                logger.warning(
                    "Format 3 mod '%s' (id=%d): no supported intents "
                    "for %s, %d skipped. Mod produced 0 byte changes.",
                    mod_name, mod_id, target,
                    len(validation.skipped))
                if warnings_out is not None:
                    warnings_out.append(
                        f"Format 3 mod '{mod_name}' produced 0 byte "
                        f"changes targeting '{target}': all "
                        f"{len(validation.skipped)} intent(s) skipped. "
                        f"Most likely the field_schema for this table "
                        f"doesn't yet have entries for the intent fields. "
                        f"Add a field_schema/<table>.json file with "
                        f"matching tid or rel_offset entries, or use the "
                        f"mod's offset-based JSON variant if available."
                    )
                continue

            # Extract vanilla bytes for the target file
            vanilla = vanilla_extractor(target)
            if vanilla is None:
                n_mods_skipped += 1
                logger.warning(
                    "Format 3 mod '%s' (id=%d): vanilla extraction "
                    "failed for %s; skipping.",
                    mod_name, mod_id, target)
                if warnings_out is not None:
                    warnings_out.append(
                        f"Format 3 mod '{mod_name}' produced 0 byte "
                        f"changes: vanilla bytes for '{target}' are "
                        f"unavailable. Most common cause: the live "
                        f"PAZ has been modded by another tool and "
                        f"the vanilla backup is missing. Revert to "
                        f"vanilla first (Settings -> Fix Everything), "
                        f"then re-apply. If the file is genuinely "
                        f"missing from your install, check the "
                        f"target name spelling or run Steam Verify."
                    )
                continue
            vanilla_body, vanilla_header = vanilla

            # Whole-table writer targets: defer dispatch to the
            # post-loop phase so all mods' intents land in a single
            # parse+serialize.
            if target in _WHOLE_TABLE_TARGETS:
                whole_table_intents.setdefault(target, []).extend(
                    validation.supported)
                whole_table_mod_names.setdefault(target, []).append(mod_name)
                whole_table_mod_ids.setdefault(target, []).append(mod_id)
                n_mods_changed += 1  # provisional; recounted below if no bytes
                files_touched.add(target)
                continue

            # Convert each supported intent into a v2-style change dict
            changes = _intents_to_v2_changes(
                target, vanilla_body, vanilla_header, validation.supported)
            if not changes:
                n_mods_skipped += 1
                # Don't pollute aggregated with empty lists.
                logger.debug(
                    "Format 3 mod '%s' (id=%d): all %d supported intents "
                    "resolved to zero changes (probably TID-not-found "
                    "or value out of range).",
                    mod_name, mod_id, len(validation.supported))
                if warnings_out is not None:
                    warnings_out.append(
                        f"Format 3 mod '{mod_name}' produced 0 byte "
                        f"changes targeting '{target}': all "
                        f"{len(validation.supported)} intents resolved "
                        f"to write-failures. Possible causes: the byte "
                        f"walker bailed on a variable-length field "
                        f"(e.g. a tagged-variant entry whose "
                        f"discriminator value isn't yet decoded — common "
                        f"for stageinfo's _sequencerDesc), TID not found "
                        f"in target entries, or value out of range for "
                        f"the field width. Check the CDUMM log for "
                        f"per-intent debug lines."
                    )
                continue

            # Tag each change with the contributing mod's id so the apply
            # pipeline's _record_skip can attribute byte-mismatch failures
            # back to this mod and persist_skip_summary writes a row that
            # lights up the yellow SKIPPED badge.
            for c in changes:
                c["_source_mod_id"] = mod_id
                c["_target_file"] = target
            aggregated.setdefault(target, []).extend(changes)
            # Report this mod as a participant so persist_skip_summary
            # resets its last_apply_skipped_count on a clean re-apply.
            if participating_mod_ids is not None:
                participating_mod_ids.add(mod_id)
            # Update summary counters for the apply-time log line.
            n_mods_changed += 1
            files_touched.add(target)
            for c in changes:
                patched_hex = c.get("patched", "")
                n_bytes_changed += len(patched_hex) // 2
            logger.debug(
                "Format 3 mod '%s' (id=%d): expanded %d intents into "
                "%d changes on %s",
                mod_name, mod_id, len(validation.supported),
                len(changes), target)

    # Whole-table writer dispatch: parse vanilla once, apply ALL
    # collected intents from every contributing mod, serialize once,
    # emit a SINGLE change. This is what makes multi-mod composition
    # work for iteminfo / skill.
    for target, batched in whole_table_intents.items():
        if not batched:
            continue
        contributing_mods = whole_table_mod_names.get(target, [])
        vanilla = vanilla_extractor(target)
        if vanilla is None:
            logger.warning(
                "Format 3 whole-table writer: vanilla extraction "
                "failed for %s, skipping %d intent(s) from %d mod(s)",
                target, len(batched), len(contributing_mods))
            if warnings_out is not None:
                warnings_out.append(
                    f"Format 3 mod(s) {', '.join(repr(n) for n in contributing_mods)} "
                    f"could not apply: vanilla bytes for '{target}' "
                    f"are unavailable. Most common cause: the live "
                    f"PAZ has been modded by another tool and the "
                    f"vanilla backup is missing. Revert to vanilla "
                    f"first (Settings -> Fix Everything), then "
                    f"re-apply. If the file is genuinely missing "
                    f"from your install, run Steam Verify."
                )
            continue
        vanilla_body, vanilla_header = vanilla
        changes = _intents_to_v2_changes(
            target, vanilla_body, vanilla_header, batched)
        if not changes:
            logger.debug(
                "Format 3 whole-table writer for %s: %d intent(s) "
                "produced 0 changes", target, len(batched))
            if warnings_out is not None:
                warnings_out.append(
                    f"Format 3 mod(s) {', '.join(repr(n) for n in contributing_mods)} "
                    f"produced 0 byte changes for '{target}'. "
                    f"Possible causes: all target item/skill keys in "
                    f"the mod are missing from this game version's "
                    f"table, or every intent set the field to its "
                    f"current value (no-op)."
                )
            continue
        # Stamp _target_file plus the full list of contributing mod
        # ids on the merged change. A single int can't represent N
        # mods' shared intent, so we use _source_mod_ids (plural list)
        # , persist_skip_summary fans out one row per id when the
        # change byte-mismatches. H3 fix.
        contrib_ids = list(whole_table_mod_ids.get(target, []))
        for c in changes:
            c["_target_file"] = target
            if contrib_ids:
                c["_source_mod_ids"] = list(contrib_ids)
        aggregated.setdefault(target, []).extend(changes)
        # Whole-table dispatch produced bytes for this target , credit
        # every contributing mod as a participant so persist_skip_summary
        # resets their skip rows on a clean apply (H2 fix).
        if participating_mod_ids is not None:
            for mid in whole_table_mod_ids.get(target, []):
                participating_mod_ids.add(mid)
        for c in changes:
            n_bytes_changed += len(c.get("patched", "")) // 2
        logger.info(
            "Format 3 whole-table writer for %s: applied %d intents "
            "across %d mod(s) in one pass",
            target, len(batched), len(contributing_mods))

    # Summary line — INFO level so bug reports auto-include it.
    # The single line summarizes "did the feature do anything?" so
    # users + maintainers can answer that question from the log
    # without digging into per-mod debug entries.
    logger.info(
        "Format 3 apply: %d mod(s) processed, %d byte(s) changed "
        "across %d file(s), %d mod(s) skipped (see warnings).",
        n_mods_processed, n_bytes_changed,
        len(files_touched), n_mods_skipped,
    )


def _intents_to_v2_changes(
    target: str, vanilla_body: bytes, vanilla_header: bytes,
    intents: list[Format3Intent],
) -> list[dict]:
    """Produce v2-format change dicts from a list of supported intents.

    Each output dict has: ``entry``, ``rel_offset``, ``original``,
    ``patched`` — exactly the shape ``aggregate_json_mods_into_
    synthetic_patches`` aggregates from real v2 mods.
    """
    table_name = identify_table_from_path(target) or _strip_pabgb(target)
    from cdumm.engine.format3_handler import LIST_WRITERS

    # buffinfo.pabgb has a CDUMM PABGB schema entry but its declared
    # field stream sizes are wrong (e.g. _isBlocked declared as
    # direct_15B when it's actually a u8, _buffDataList declared as
    # direct_u32 when it's actually a length-prefixed variant array).
    # The generic schema walker silently lands at the wrong offset.
    # Route buffinfo through the clean-room buffinfo parser instead,
    # which knows the actual on-disk layout.
    if table_name == "buffinfo":
        return _buffinfo_intents_to_changes(
            vanilla_body, vanilla_header, intents)

    has_cdumm_schema = has_schema(table_name)
    # Tables without a CDUMM PABGB schema are still processable when
    # ALL their intents target a registered list writer (e.g. skill.pabgb
    # via the vendored skillinfo_parser). The writer is the
    # source of truth for the binary layout.
    if not has_cdumm_schema:
        all_writer_routable = bool(intents) and all(
            (table_name, i.field) in LIST_WRITERS for i in intents
        )
        if not all_writer_routable:
            return []
        # Whole-table writer dispatch only — skip per-record path
        # entirely. No need for PABGH parse, name index, etc.
        out: list[dict] = []
        if table_name == "iteminfo":
            from cdumm.engine.iteminfo_writer import (
                build_iteminfo_intent_change,
            )
            change = build_iteminfo_intent_change(vanilla_body, list(intents))
            if change is not None:
                out.append(change)
        elif table_name == "skill":
            from cdumm.engine.skill_writer import (
                build_skill_intent_change,
            )
            change = build_skill_intent_change(
                vanilla_body, vanilla_header, list(intents))
            if change is not None:
                out.append(change)
        return out

    key_size, offsets = parse_pabgh_index(vanilla_header, table_name)
    if not offsets:
        return []
    # H2 guard: we don't know how to walk entry headers for widths
    # other than u16/u32. Refusing is safer than misaligning.
    if key_size not in (2, 4):
        logger.warning(
            "Format 3 expansion on '%s' refused: unsupported "
            "PABGH key_size=%d", target, key_size)
        return []

    schema = get_schema(table_name)
    field_specs = {f.name: f for f in schema.fields}
    fs_entries = load_field_schema(table_name)

    # Compute (start, end) per record for TID search bounds + name lookup
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    entry_bounds: dict[int, tuple[int, int, str]] = {}
    for i, (k, off) in enumerate(sorted_offs):
        end = (sorted_offs[i + 1][1]
               if i + 1 < len(sorted_offs) else len(vanilla_body))
        name = _entry_name(vanilla_body, off, key_size)
        entry_bounds[k] = (off, end, name)

    no_null_skip = getattr(schema, "no_null_skip", False)
    no_entry_header = getattr(schema, "no_entry_header", False)

    out: list[dict] = []
    # Whole-table writers (iteminfo, skill) batch all their intents
    # into one offset=0 change for the entire .pabgb body.
    iteminfo_batch: list = []
    skill_batch: list = []

    # When a mod mixes primitive intents and list-of-dict intents on
    # the same iteminfo target, the primitive's per-record change
    # would be silently overwritten by the whole-file change (the
    # whole-file applies first by sort order, leaving the primitive's
    # `original` bytes mismatched against the new buffer). Route ALL
    # iteminfo intents through the writer when any list intent is
    # present so they compose in one parsed-dict pass. Bug from
    # systematic-debugging round on test_iteminfo_mixed_intents.
    iteminfo_force_batch = (
        table_name == "iteminfo" and any(
            (table_name, i.field) in LIST_WRITERS for i in intents
        )
    )
    skill_force_batch = (
        table_name == "skill" and any(
            (table_name, i.field) in LIST_WRITERS for i in intents
        )
    )

    for intent in intents:
        if intent.key not in entry_bounds:
            continue
        entry_off, entry_end, entry_name = entry_bounds[intent.key]

        # Batched whole-table writer dispatch (forced for iteminfo /
        # skill when any list-writer intent exists in the same mod).
        if iteminfo_force_batch and table_name == "iteminfo":
            iteminfo_batch.append(intent)
            continue
        if skill_force_batch and table_name == "skill":
            skill_batch.append(intent)
            continue
        # Per-list-writer-only path (no primitives mixed in).
        if (table_name == "iteminfo"
                and (table_name, intent.field) in LIST_WRITERS):
            iteminfo_batch.append(intent)
            continue
        if (table_name == "skill"
                and (table_name, intent.field) in LIST_WRITERS):
            skill_batch.append(intent)
            continue

        # Per-record list writer dispatch (e.g. dropsetinfo.drops):
        # one change emitted per intent, anchored at the entry name.
        if (table_name, intent.field) in LIST_WRITERS:
            change = _build_list_writer_change(
                table_name, intent, vanilla_body, entry_off, entry_end,
                entry_name)
            if change is not None:
                out.append(change)
            continue

        payload_off = _payload_offset(
            vanilla_body, entry_off, key_size,
            no_null_skip=no_null_skip,
            no_entry_header=no_entry_header)
        if payload_off is None:
            continue

        # Resolve write position via field_schema first, then PABGB schema
        write_pos = _resolve_write_pos(
            intent, fs_entries, field_specs, schema,
            vanilla_body, payload_off, entry_end)
        if write_pos is None:
            continue
        abs_off, size, fmt = write_pos

        if abs_off + size > entry_end:
            continue
        try:
            new_bytes = struct.pack(f"<{fmt}", intent.new)
        except struct.error:
            continue
        if len(new_bytes) != size:
            continue

        original_bytes = bytes(vanilla_body[abs_off:abs_off + size])
        # rel_offset must be in the same coordinate system as the apply
        # pipeline's `_build_name_offsets_generic`, which anchors entry
        # names at `name_end` (= entry_off + eid_size + 4 + name_len).
        # Earlier this emitted `abs_off - entry_off` (record-start
        # relative), causing the apply to land `8 + name_len` bytes
        # past the target field on every primitive Format 3 intent.
        # Bug from Faisal's Can It Stack JSON V3 test 2026-05-01: 1812
        # of 1827 max_stack_count patches mismatched because they were
        # reading bytes from adjacent string fields. Latent since
        # v3.2.3 when Format 3 primitive support shipped — the
        # ZirconX1 / Lichtnocht "applies cleanly but doesn't work
        # in-game" reports trace here.
        if no_entry_header:
            # No name field, so the apply pipeline can't use name_end.
            # Keep entry_off as the anchor (matches the no_entry_header
            # case in _payload_offset).
            rel_offset = abs_off - entry_off
        else:
            eid_size = 2 if key_size == 2 else 4
            _name_len = struct.unpack_from(
                "<I", vanilla_body, entry_off + eid_size)[0]
            name_end = entry_off + eid_size + 4 + _name_len
            rel_offset = abs_off - name_end

        out.append({
            "entry": entry_name or intent.entry,
            "rel_offset": rel_offset,
            "original": original_bytes.hex(),
            "patched": new_bytes.hex(),
            "label": f"{intent.entry}.{intent.field}",
        })

    # Flush the iteminfo batch (whole-table writer): all collected
    # intents become a single offset=0 change covering the full
    # iteminfo.pabgb body.
    if iteminfo_batch:
        from cdumm.engine.iteminfo_writer import (
            build_iteminfo_intent_change,
        )
        iteminfo_change = build_iteminfo_intent_change(
            vanilla_body, iteminfo_batch)
        if iteminfo_change is not None:
            out.append(iteminfo_change)

    # Same for skill: the skillinfo_parser needs the .pabgh
    # header to walk records, so we forward `vanilla_header` here.
    if skill_batch:
        from cdumm.engine.skill_writer import (
            build_skill_intent_change,
        )
        skill_change = build_skill_intent_change(
            vanilla_body, vanilla_header, skill_batch)
        if skill_change is not None:
            out.append(skill_change)

    return out


def _buffinfo_field_candidates(field: str) -> list[str]:
    """Yield naming-convention aliases for a buffinfo intent field.

    Mirrors the 4-shape lookup chain in
    ``format3_apply._resolve_write_pos`` (and the validator) so the
    apply-time path accepts every name the validator accepts. Without
    this the validator says "supported" but the apply silently emits
    nothing for camelCase names like ``_minLevel``.

    Item paths (``buff_data_list[N].xxx``) pass through unchanged ,
    only the leaf identifier inside wrapper-only paths is normalized.
    """
    if "[" in field or "." in field:
        # Item-path leaves are already snake_case in the parser; we
        # don't fan out aliases for them yet. (The field-names
        # dialect is the only known producer of these paths.)
        return [field]
    candidates = [field, f"_{field}"]
    from cdumm.engine.format3_handler import _snake_to_camel
    if "_" in field:
        camel = _snake_to_camel(field)
        if camel != field:
            candidates.extend([camel, f"_{camel}"])
    # camelCase → snake_case for inputs that came in camelCase.
    if any(c.isupper() for c in field):
        snake = []
        for i, c in enumerate(field):
            if c.isupper() and i > 0:
                snake.append("_")
            snake.append(c.lower())
        candidates.append("".join(snake))
    # Always try the leading-underscore-stripped form too , covers
    # ``_min_level`` -> ``min_level`` and ``_minLevel`` -> ``minLevel``.
    if field.startswith("_"):
        stripped = field[1:]
        candidates.append(stripped)
        if any(c.isupper() for c in stripped):
            snake2 = []
            for i, c in enumerate(stripped):
                if c.isupper() and i > 0:
                    snake2.append("_")
                snake2.append(c.lower())
            candidates.append("".join(snake2))
    # Deduplicate while preserving order.
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


_BUFFINFO_DTYPE_PACK = {
    "u8": ("B", 1),
    "u16": ("H", 2),
    "u32": ("I", 4),
    "u64": ("Q", 8),
}


def _buffinfo_intents_to_changes(
    vanilla_body: bytes, vanilla_header: bytes,
    intents: list[Format3Intent],
) -> list[dict]:
    """Resolve buffinfo intents through the clean-room buffinfo parser.

    The generic schema walker can't walk past variable-length items, so
    the CDUMM PABGB schema for buffinfo is structurally wrong. This
    helper reads the PABGH key→offset table, slices each entry's bytes,
    delegates field resolution to ``buffinfo_parser.locate_buff_field``,
    and packs the new value with the dtype the parser reports.

    Intents whose key isn't in PABGH, whose field path isn't yet
    resolvable (e.g. items past an unknown variant), or whose new
    value doesn't fit the declared width, are silently dropped , the
    same shape the v2 aggregator uses. The expand_format3 caller
    surfaces "0 byte changes" warnings for whole-mod dropouts.
    """
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    key_size, offsets = parse_pabgh_index(vanilla_header, "buffinfo")
    if not offsets:
        return []
    # buffinfo PABGH is u32-keyed in every shipped game version we've
    # observed. Refuse other widths rather than misalign.
    if key_size != 4:
        logger.warning(
            "Format 3 buffinfo expansion refused: PABGH key_size=%d "
            "(expected 4). Skipping all %d intent(s).",
            key_size, len(intents))
        return []

    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    entry_bounds: dict[int, tuple[int, int]] = {}
    for i, (k, off) in enumerate(sorted_offs):
        end = (sorted_offs[i + 1][1]
               if i + 1 < len(sorted_offs) else len(vanilla_body))
        entry_bounds[k] = (off, end)

    out: list[dict] = []
    for intent in intents:
        bounds = entry_bounds.get(intent.key)
        if bounds is None:
            continue
        entry_off, entry_end = bounds
        entry_bytes = bytes(vanilla_body[entry_off:entry_end])
        # Mirror the validator's 4-shape field-name lookup so a path
        # like ``_minLevel`` (CDUMM schema convention) resolves the
        # same as ``min_level`` (field-names dialect convention). Without
        # this, intents would validate-then-fail-to-apply and present
        # to users as "imported, enabled, no effect".
        located = None
        for candidate in _buffinfo_field_candidates(intent.field):
            try:
                hit = locate_buff_field(entry_bytes, candidate)
            except (ValueError, struct.error):
                continue
            if hit is not None:
                located = hit
                break
        if located is None:
            continue
        rel_in_entry, width, dtype = located
        if dtype == "cstring":
            # Length-preserving asset_path write: the located offset
            # points at the u32 length prefix. The string body sits 4
            # bytes later. We only support same-byte-length writes ,
            # changing the length would shift every subsequent byte
            # in the entry and require whole-entry re-encoding (which
            # then ripples through PAMT/PAPGT integrity hashes).
            if not isinstance(intent.new, str):
                continue
            length_pos = entry_off + rel_in_entry
            if length_pos + 4 > entry_end:
                continue
            current_len = struct.unpack_from(
                "<I", vanilla_body, length_pos)[0]
            new_b = intent.new.encode("utf-8")
            if len(new_b) != current_len:
                continue  # length change not supported here
            body_pos = length_pos + 4
            if body_pos + current_len > entry_end:
                continue
            original_bytes = bytes(
                vanilla_body[body_pos:body_pos + current_len])
            # Emit a change covering the body bytes only , length
            # prefix is unchanged.
            eid_size = 4
            name_len = struct.unpack_from(
                "<I", vanilla_body, entry_off + eid_size)[0]
            name_end = entry_off + eid_size + 4 + name_len
            try:
                entry_name = vanilla_body[
                    entry_off + eid_size + 4:name_end].decode("utf-8")
            except UnicodeDecodeError:
                entry_name = intent.entry
            out.append({
                "entry": entry_name or intent.entry,
                "rel_offset": body_pos - name_end,
                "original": original_bytes.hex(),
                "patched": new_b.hex(),
                "label": f"{intent.entry}.{intent.field}",
            })
            continue
        spec = _BUFFINFO_DTYPE_PACK.get(dtype)
        if spec is None:
            continue
        fmt, expected_width = spec
        if width != expected_width:
            continue
        # Writes to the tag byte (reachable via ``.data.variant.type``
        # OR ``.data.base.tag`` , same byte either way) need careful
        # handling because the tag determines the variant tail layout.
        # A real type change would leave the entry with the new tag's
        # discriminator but the OLD tag's tail bytes after the common
        # payload , silent corruption.
        # Accepted shapes:
        #   * "VariantName" string -> translate via _VARIANT_NAME_TO_TAG
        #   * int -> use as-is
        # In both cases the new value must match the current tag byte
        # (no-op confirmation). Mismatches are skipped silently;
        # type-changing writes need whole-tail re-encoding (deferred).
        new_value = intent.new
        is_tag_write = (
            intent.field.endswith(".data.variant.type")
            or intent.field.endswith(".data.base.tag")
        )
        if is_tag_write:
            current_tag = vanilla_body[entry_off + rel_in_entry]
            if isinstance(new_value, str):
                from cdumm._vendor.buffinfo_parser import (
                    _VARIANT_NAME_TO_TAG,
                )
                new_tag = _VARIANT_NAME_TO_TAG.get(new_value)
                if new_tag is None:
                    continue  # unknown variant name
                if new_tag != current_tag:
                    continue  # type change not supported
                new_value = current_tag
            elif isinstance(new_value, int):
                if new_value != current_tag:
                    continue  # type change not supported
            else:
                continue  # unsupported type
        try:
            new_bytes = struct.pack(f"<{fmt}", new_value)
        except (struct.error, TypeError):
            continue
        abs_off = entry_off + rel_in_entry
        if abs_off + width > entry_end:
            continue
        original_bytes = bytes(vanilla_body[abs_off:abs_off + width])

        # Compute rel_offset against the same anchor the apply pipeline
        # uses for buffinfo (name_end), matching the convention in the
        # generic schema-based path.
        eid_size = 4
        name_len = struct.unpack_from(
            "<I", vanilla_body, entry_off + eid_size)[0]
        name_end = entry_off + eid_size + 4 + name_len
        try:
            entry_name = vanilla_body[
                entry_off + eid_size + 4:name_end].decode("utf-8")
        except UnicodeDecodeError:
            entry_name = intent.entry

        out.append({
            "entry": entry_name or intent.entry,
            "rel_offset": abs_off - name_end,
            "original": original_bytes.hex(),
            "patched": new_bytes.hex(),
            "label": f"{intent.entry}.{intent.field}",
        })
    return out


def _build_list_writer_change(
    table_name: str,
    intent,
    vanilla_body: bytes,
    entry_off: int,
    entry_end: int,
    entry_name: str,
) -> "dict | None":
    """Dispatch a list-of-dict intent to its registered writer module.

    Currently registered:
      ('dropsetinfo', 'drops') -> dropset_writer.build_drops_replacement_change

    Returns a v2-style change dict suitable for the aggregator, or
    None on parse failure / wrong intent shape.
    """
    record_bytes = vanilla_body[entry_off:entry_end]
    if table_name == "dropsetinfo" and intent.field == "drops":
        from cdumm.engine.dropset_writer import (
            build_drops_replacement_change,
        )
        if not isinstance(intent.new, list):
            return None
        return build_drops_replacement_change(
            record_bytes,
            intent_key=intent.key,
            intent_entry=entry_name or intent.entry,
            new_drops_json=intent.new,
        )
    return None


def _resolve_write_pos(
    intent: Format3Intent, fs_entries: dict, field_specs: dict,
    schema, body: bytes, payload_off: int, entry_end: int,
) -> "tuple[int, int, str] | None":
    """Return (abs_offset, size, struct_fmt) for the write, or None.

    Mirrors the precedence the Phase 1-3 writer + validator use:
    field_schema entry → PABGB schema field → None.
    """
    fs_entry = fs_entries.get(intent.field)
    if fs_entry is not None:
        fmt_size = DTYPE_TABLE.get(fs_entry.data_type.lower())
        if fmt_size is None:
            return None
        fmt, size = fmt_size
        abs_off = locate_field(
            body, payload_off, entry_end, fs_entry)
        if abs_off is None:
            return None
        return abs_off, size, fmt

    # Field-name lookup: field-names mods use snake_case without
    # the leading underscore; the schema/overrides use camelCase
    # with prefix (`_gimmickInfo`). Mirror the validator's four-shape
    # lookup at format3_handler.py: exact / +underscore /
    # snake→camel / snake→camel + underscore. Round-5 systematic-
    # debugging finding (Matrixz mod's gimmick_info / item_charge_type).
    from cdumm.engine.format3_handler import _snake_to_camel
    candidate_names = [intent.field, f"_{intent.field}"]
    if "_" in intent.field:
        camel = _snake_to_camel(intent.field)
        if camel != intent.field:
            candidate_names.extend([camel, f"_{camel}"])
    spec = None
    target_name = intent.field
    for n in candidate_names:
        if n in field_specs:
            spec = field_specs[n]
            target_name = n
            break
    if spec is None or not spec.struct_fmt or not spec.stream_size:
        return None
    # Walk schema fields up to target. For each field, use its actual
    # binary footprint so variable-length fields (CString, etc.) are
    # consumed correctly. Pure stream_size summation breaks when any
    # preceding field has a variable encoding. Round-N review.
    abs_off = payload_off
    for f in schema.fields:
        if f.name == target_name:
            return abs_off, spec.stream_size, spec.struct_fmt
        consumed = _consume_field_bytes(body, abs_off, f, entry_end)
        if consumed is None:
            return None  # unknown / unsupported variable type
        abs_off += consumed
        if abs_off >= entry_end:
            return None
    return None


def _consume_field_bytes(body: bytes, off: int, spec, entry_end: int
                          ) -> int | None:
    """Return how many bytes ``spec`` consumes starting at ``off``,
    or None if the type isn't safely walkable.

    Resolution order:
      1. ``spec.type_descriptor`` (Path B override) — delegate to
         ``pabgb_types.consume_bytes`` for full PABGB primitive +
         CArray + COptional + tagged-variant + sub-struct support.
      2. Legacy ``CString`` literal in ``spec.field_type``.
      3. Legacy ``stream_size`` for fixed-size fields.
      4. None (caller must bail).
    """
    # Defensive negative-offset guard mirroring pabgb_types.consume_bytes.
    # struct.unpack_from with a negative offset reads from the buffer's
    # end and raises struct.error when there aren't enough bytes — the
    # exception would propagate up. In production callers always pass
    # non-negative offsets, but this keeps the legacy path symmetric
    # with the walker's defenses. Iteration 5 systematic-debugging
    # finding 2026-04-27.
    if off < 0:
        return None
    descriptor = getattr(spec, "type_descriptor", None)
    if descriptor:
        from cdumm.semantic.pabgb_types import consume_bytes
        return consume_bytes(descriptor, body, off, entry_end)
    if spec.field_type == "CString":
        if off + 4 > min(len(body), entry_end):
            return None
        slen = struct.unpack_from("<I", body, off)[0]
        # Defensive 10MB cap mirroring pabgb_types.consume_bytes; a
        # garbage slen=0xFFFFFFFF passes the bounds check trivially
        # because off+4+4294967295 overflows the comparison only after
        # int promotion, but the cap makes the failure intent explicit.
        if slen > 10_000_000:
            return None
        if off + 4 + slen > min(len(body), entry_end):
            return None
        return 4 + slen
    if spec.stream_size:
        # Fixed-size field (struct_fmt set OR raw direct_NB): consume
        # the declared stream_size IF it fits within the entry payload
        # AND the buffer. Iteration 6 systematic-debugging finding —
        # the previous unconditional `return spec.stream_size` would
        # report a successful consume even past EOF, breaking
        # downstream offset accounting.
        if off + spec.stream_size > min(entry_end, len(body)):
            return None
        return spec.stream_size
    # stream_size=0 means the schema didn't classify this — can't
    # walk safely.
    return None


def _payload_offset(body: bytes, entry_off: int,
                    key_size: int,
                    no_null_skip: bool = False,
                    no_entry_header: bool = False) -> "int | None":
    """Return the byte offset where the entry's first payload field starts.

    Three modes (in priority order):

    * ``no_entry_header=True`` — payload IS the entry; return ``entry_off``
      verbatim. Required for tables like RegionInfo where ``_key`` and
      ``_stringKey`` are regular schema fields (no separate header).

    * ``no_null_skip=True`` — skip the standard entry header (entry_id +
      name_len + name) but do NOT skip a trailing zero byte. Required for
      ItemInfo, VehicleInfo, FieldInfo, StageInfo where the byte after
      the name is a real ``_isBlocked`` u8 field, not padding.

    * Default — legacy heuristic from ``format3_handler``: skip a single
      0 byte after the name when present. Works for tables where the
      post-name byte is genuinely padding.
    """
    if no_entry_header:
        # Strict `<` so EOF itself is rejected — there's no field to
        # read at exactly `len(body)`. Adversarial review CONSENSUS-2
        # 2026-04-27. The walker's per-primitive bounds checks would
        # also catch a subsequent read, but rejecting up front
        # surfaces malformed PAMT offsets at the right layer.
        return entry_off if 0 <= entry_off < len(body) else None
    eid_size = 2 if key_size == 2 else 4
    head_size = eid_size + 4
    if entry_off + head_size > len(body):
        return None
    name_len = struct.unpack_from("<I", body, entry_off + eid_size)[0]
    if name_len > 500 or entry_off + head_size + name_len > len(body):
        return None
    name_end = entry_off + head_size + name_len
    if no_null_skip:
        return name_end
    if name_end < len(body) and body[name_end] == 0:
        return name_end + 1
    return name_end


def _entry_name(body: bytes, entry_off: int,
                key_size: int) -> str:
    """Read the name string from an entry header. Empty string on
    failure — the caller falls back to ``intent.entry``."""
    eid_size = 2 if key_size == 2 else 4
    head_size = eid_size + 4
    if entry_off + head_size > len(body):
        return ""
    name_len = struct.unpack_from("<I", body, entry_off + eid_size)[0]
    if name_len > 500 or entry_off + head_size + name_len > len(body):
        return ""
    try:
        return body[
            entry_off + head_size:entry_off + head_size + name_len
        ].decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _strip_pabgb(target: str) -> str:
    name = target
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.lower().endswith(".pabgb"):
        name = name[: -len(".pabgb")]
    return name.lower()
