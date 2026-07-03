"""Format 3 → v2 expansion for the apply pipeline (Phase 4 / Option B).

CDUMM's existing v2 mount-time apply path runs through
``aggregate_json_mods_into_synthetic_patches`` in apply_engine.py.
That function builds an ``aggregated[game_file] -> list[change]``
dict from every enabled v2 mod's stored JSON. Format 3 mods don't
have ``patches`` keys, so they contribute nothing through that
path, even though Phase 1-3's writer can resolve their intents
into the same shape of byte changes.

This module bridges the gap. ``expand_format3_into_aggregated()``
reads each enabled mod whose ``json_source`` points at a Format 3
file, extracts vanilla bytes for the target ``.pabgb``, resolves
each supported intent into a v2-style change dict
(``{entry, rel_offset, original, patched}``), and APPENDS the
results to the same ``aggregated`` dict.

Design invariants:

  * Existing v2 entries in ``aggregated`` are never modified , 
    only appended to.
  * Mods with no resolvable intents do NOT create empty
    ``aggregated[game_file] = []`` entries.
  * Vanilla extraction failures, malformed JSON, and unsupported
    PABGH key_sizes log at warning and skip that mod, never
    raising, the apply pipeline must always complete.
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
    renders them in the post-apply InfoBar, same surfacing the
    JMM-parity skipped-patches feature uses.
    """
    rows = db.connection.execute(
        "SELECT id, name, json_source, priority FROM mods "
        "WHERE enabled = 1 AND json_source IS NOT NULL "
        "AND json_source != '' "
        "ORDER BY priority DESC, id ASC"
    ).fetchall()

    # Counters for the apply-time summary log line. INFO-level so
    # bug reports include it automatically, addresses the DX
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
    # multichangeinfo.pabgb is whole-table because its writer reparses
    # the whole table per record and also rebuilds the companion
    # multichangeinfo.pabgh (GitHub #125 Refinement Cost Reforged).
    # storeinfo.pabgb is whole-table for the same reason as
    # multichangeinfo: its writer rebuilds the entry's record list
    # (which can grow) and the companion storeinfo.pabgh offsets in
    # one pass (GitHub #183 stock_data_list).
    # equipslotinfo.pabgb (GitHub #190): records grow when a mod
    # appends etl hashes, so the writer rebuilds entry + .pabgh
    # offsets in one pass, same contract as storeinfo.
    # stringinfo.pabgb is whole-table because its writer rewrites the
    # variable-length _buffer string per record (records change length)
    # and rebuilds the companion stringinfo.pabgh offsets in one pass
    # (GitHub #224 Female Armor Module).
    _WHOLE_TABLE_TARGETS = {
        "iteminfo.pabgb", "skill.pabgb", "multichangeinfo.pabgb",
        "storeinfo.pabgb", "equipslotinfo.pabgb", "stringinfo.pabgb"}
    whole_table_intents: dict[str, list] = {}
    whole_table_mod_names: dict[str, list[str]] = {}
    # Per-INTENT mod attribution, index-aligned with
    # whole_table_intents[target]. Lets the per-intent refusal
    # degradation below name which mod contributed a dropped intent.
    whole_table_intent_mods: dict[str, list[str]] = {}
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
            # the file is malformed. Either way, skip silently , 
            # the v2 aggregator already logged its own parse errors
            # for the same file.
            continue

        # Confirmed Format 3 mod from here on, count it once per mod,
        # not per target.
        n_mods_processed += 1

        for target, intents in target_pairs:
            try:
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
                    whole_table_intent_mods.setdefault(target, []).extend(
                        [mod_name] * len(validation.supported))
                    whole_table_mod_names.setdefault(target, []).append(mod_name)
                    whole_table_mod_ids.setdefault(target, []).append(mod_id)
                    n_mods_changed += 1  # provisional; recounted below if no bytes
                    files_touched.add(target)
                    # Instrumentation for GitHub #105: macOS bundle showed
                    # Buffed Axiom Bracelet (iteminfo Format 3) produce
                    # zero changes with no iteminfo whole-table-writer log
                    # anywhere. This INFO line proves whether the intent
                    # reached this branch on this platform, and exactly
                    # which intents got batched.
                    logger.info(
                        "Format 3 batched %d supported intent(s) from mod "
                        "'%s' (id=%d) into whole-table writer queue for %s "
                        "(queue depth now %d)",
                        len(validation.supported), mod_name, mod_id,
                        target, len(whole_table_intents[target]))
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
                            f"discriminator value isn't yet decoded, common "
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
            except Exception as e:
                # EXPAND NEVER RAISES contract (docstring): an
                # unguarded struct.unpack_from on corrupt table bytes
                # must not abort the whole apply. Log, surface a
                # warning naming the mod + target, continue with the
                # next target.
                logger.error(
                    "Format 3 mod '%s' (id=%d): processing target %s "
                    "crashed: %s", mod_name, mod_id, target, e,
                    exc_info=True)
                if warnings_out is not None:
                    warnings_out.append(
                        f"Format 3 mod '{mod_name}' could not apply: "
                        f"processing '{target}' failed ({e}). The rest "
                        f"of the apply continued.")
                continue

    # Whole-table writer dispatch: parse vanilla once, apply ALL
    # collected intents from every contributing mod, serialize once,
    # emit a SINGLE change. This is what makes multi-mod composition
    # work for iteminfo / skill.
    # Instrumentation for GitHub #105: log the queue depth for every
    # whole-table target before we dispatch. If a target appears here
    # with zero intents (or is missing entirely from the dict despite
    # being expected), the per-mod batched log above will have told us
    # which mods contributed; this loop tells us what made it through.
    for _wtt in sorted(_WHOLE_TABLE_TARGETS):
        logger.info(
            "Format 3 whole-table dispatch entering for %s: %d "
            "intent(s) batched",
            _wtt, len(whole_table_intents.get(_wtt, [])))
    for target, batched in whole_table_intents.items():
        try:
            if not batched:
                logger.info(
                    "Format 3 whole-table writer for %s: queue empty, "
                    "nothing to dispatch (no mod contributed any supported "
                    "intents this run)", target)
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

            # multichangeinfo.pabgb (GitHub #125): its writer produces one
            # per-record change for the .pabgb plus a rebuilt companion
            # .pabgh. The .pabgh change is injected under its own
            # aggregated key so the mount-time pipeline emits it as a
            # second overlay entry. Handled here, before the generic
            # _intents_to_v2_changes path, because that path emits changes
            # for a single target only.
            if target == "multichangeinfo.pabgb":
                from cdumm.engine.multichangeinfo_writer import (
                    build_multichangeinfo_changes,
                )
                mci_intents = [
                    (i.entry, i.key, i.field, i.new) for i in batched
                ]
                try:
                    pabgb_changes, pabgh_change = build_multichangeinfo_changes(
                        vanilla_body, vanilla_header, mci_intents)
                except Exception as e:
                    logger.error(
                        "Format 3 multichangeinfo writer crashed on %d "
                        "intent(s): %s", len(batched), e, exc_info=True)
                    pabgb_changes, pabgh_change = [], None
                if not pabgb_changes:
                    logger.warning(
                        "Format 3 multichangeinfo: %d intent(s) from %d "
                        "mod(s) produced 0 record changes",
                        len(batched), len(contributing_mods))
                    if warnings_out is not None:
                        warnings_out.append(
                            f"Format 3 mod(s) "
                            f"{', '.join(repr(n) for n in contributing_mods)} "
                            f"produced 0 byte changes for "
                            f"'multichangeinfo.pabgb'. The targeted "
                            f"refinement recipes may not exist in this game "
                            f"version, or every targeted material slot sits "
                            f"in a record whose material list CDUMM cannot "
                            f"locate yet."
                        )
                    continue
                contrib_ids = list(whole_table_mod_ids.get(target, []))
                for c in pabgb_changes:
                    c["_target_file"] = target
                    if contrib_ids:
                        c["_source_mod_ids"] = list(contrib_ids)
                aggregated.setdefault(target, []).extend(pabgb_changes)
                if pabgh_change is not None:
                    pabgh_change["_target_file"] = "multichangeinfo.pabgh"
                    if contrib_ids:
                        pabgh_change["_source_mod_ids"] = list(contrib_ids)
                    aggregated.setdefault(
                        "multichangeinfo.pabgh", []).append(pabgh_change)
                if participating_mod_ids is not None:
                    for mid in contrib_ids:
                        participating_mod_ids.add(mid)
                for c in pabgb_changes:
                    n_bytes_changed += len(c.get("patched", "")) // 2
                logger.info(
                    "Format 3 multichangeinfo writer: applied %d intent(s) "
                    "across %d mod(s), %d record change(s)%s",
                    len(batched), len(contributing_mods), len(pabgb_changes),
                    ", + pabgh offset rebuild"
                    if pabgh_change is not None else "")
                continue

            # stringinfo.pabgb (GitHub #224): same two-file contract as
            # multichangeinfo. The writer rewrites the variable-length
            # _buffer string per record (records change length) and
            # rebuilds the companion stringinfo.pabgh offsets in one pass.
            if target == "stringinfo.pabgb":
                from cdumm.engine.stringinfo_writer import (
                    build_stringinfo_changes,
                )
                si_intents = [
                    (i.entry, i.key, i.field, i.new) for i in batched
                ]
                try:
                    pabgb_changes, pabgh_change = build_stringinfo_changes(
                        vanilla_body, vanilla_header, si_intents)
                except Exception as e:
                    logger.error(
                        "Format 3 stringinfo writer crashed on %d "
                        "intent(s): %s", len(batched), e, exc_info=True)
                    pabgb_changes, pabgh_change = [], None
                if not pabgb_changes:
                    logger.warning(
                        "Format 3 stringinfo: %d intent(s) from %d mod(s) "
                        "produced 0 record changes",
                        len(batched), len(contributing_mods))
                    if warnings_out is not None:
                        warnings_out.append(
                            f"Format 3 mod(s) "
                            f"{', '.join(repr(n) for n in contributing_mods)} "
                            f"produced 0 byte changes for "
                            f"'stringinfo.pabgb'. The targeted string keys "
                            f"may not exist in this game version, or the new "
                            f"values matched the vanilla strings."
                        )
                    continue
                contrib_ids = list(whole_table_mod_ids.get(target, []))
                for c in pabgb_changes:
                    c["_target_file"] = target
                    if contrib_ids:
                        c["_source_mod_ids"] = list(contrib_ids)
                aggregated.setdefault(target, []).extend(pabgb_changes)
                if pabgh_change is not None:
                    pabgh_change["_target_file"] = "stringinfo.pabgh"
                    if contrib_ids:
                        pabgh_change["_source_mod_ids"] = list(contrib_ids)
                    aggregated.setdefault(
                        "stringinfo.pabgh", []).append(pabgh_change)
                if participating_mod_ids is not None:
                    for mid in contrib_ids:
                        participating_mod_ids.add(mid)
                for c in pabgb_changes:
                    n_bytes_changed += len(c.get("patched", "")) // 2
                logger.info(
                    "Format 3 stringinfo writer: applied %d intent(s) "
                    "across %d mod(s), %d record change(s)%s",
                    len(batched), len(contributing_mods), len(pabgb_changes),
                    ", + pabgh offset rebuild"
                    if pabgh_change is not None else "")
                continue

            # storeinfo.pabgb (GitHub #183) and equipslotinfo.pabgb
            # (GitHub #190): same two-file contract as multichangeinfo,
            # the writer rebuilds the targeted entry's record list (which
            # grows when a mod adds items/hashes) plus the companion
            # .pabgh offsets in one pass.
            #
            # Only the writer-supported list fields divert here; every
            # other intent on these targets falls through to the standard
            # path below (storeinfo has a PABGB schema for its scalar
            # fields, and equipslotinfo intents carrying `old` hex use the
            # raw-replacement branch). Without the partition, a mod mixing
            # stock_data_list with scalar storeinfo edits silently lost
            # the scalars (release-review finding, 2026-06-10). The list
            # replaces and the scalar changes cannot overlap: scalars live
            # in the entry head before the record list, and the apply
            # pipeline's cumulative shift covers offsets after a grown
            # entry, same as multichangeinfo's per-record growth.
            if target in ("storeinfo.pabgb", "equipslotinfo.pabgb"):
                if target == "equipslotinfo.pabgb":
                    from cdumm.engine.equipslotinfo_writer import (
                        EquipslotWriteRefused as StoreinfoWriteRefused,
                        build_equipslotinfo_changes as build_storeinfo_changes,
                    )
                    import re as _re
                    def _writer_supported(i):
                        return _re.match(
                            r"^entries\[\d+\]\.etl_hashes$",
                            (getattr(i, "field", "") or "")) is not None
                else:
                    from cdumm.engine.storeinfo_writer import (
                        StoreinfoWriteRefused, build_storeinfo_changes,
                    )
                    def _writer_supported(i):
                        return (getattr(i, "field", "") or "").strip() in (
                            "stock_data_list", "_exchangeItemInfoListForSell")
                _companion = target.replace(".pabgb", ".pabgh")
                # Per-intent mod attribution (index-aligned with the
                # original batched list, built BEFORE any filtering).
                _intent_mods = whole_table_intent_mods.get(target, [])
                _mod_by_intent: dict[int, str] = {}
                if len(_intent_mods) == len(batched):
                    for _i_obj, _m_name in zip(batched, _intent_mods):
                        _mod_by_intent[id(_i_obj)] = _m_name
                writer_batch = [i for i in batched if _writer_supported(i)]
                passthrough = [i for i in batched if not _writer_supported(i)]
                if passthrough:
                    extra = _intents_to_v2_changes(
                        target, vanilla_body, vanilla_header, passthrough)
                    if extra:
                        contrib_ids_pt = list(
                            whole_table_mod_ids.get(target, []))
                        for c in extra:
                            c["_target_file"] = target
                            if contrib_ids_pt:
                                c["_source_mod_ids"] = list(contrib_ids_pt)
                        aggregated.setdefault(target, []).extend(extra)
                        for c in extra:
                            n_bytes_changed += len(c.get("patched", "")) // 2
                        if participating_mod_ids is not None:
                            for mid in whole_table_mod_ids.get(target, []):
                                participating_mod_ids.add(mid)
                        logger.info(
                            "Format 3 %s: %d non-list intent(s) handled by "
                            "the standard path (%d change(s))",
                            target, len(passthrough), len(extra))
                if not writer_batch:
                    continue
                batched = writer_batch
                # Per-intent refusal degradation (audit 2026-06-11): a
                # single refused/malformed intent used to abort the whole
                # multi-mod batch for this table. Probe-and-rebuild keeps
                # every intent the writer accepts and drops only the
                # refused ones, with a warning naming intents + mods.
                try:
                    pabgb_changes, pabgh_change, _dropped = (
                        _build_with_per_intent_refusals(
                            build_storeinfo_changes, StoreinfoWriteRefused,
                            vanilla_body, vanilla_header, batched))
                except Exception as e:
                    logger.error(
                        "Format 3 %s writer crashed on %d "
                        "intent(s): %s", target, len(batched), e,
                        exc_info=True)
                    pabgb_changes, pabgh_change, _dropped = [], None, []
                if _dropped:
                    kept_n = len(batched) - len(_dropped)
                    drop_lines = []
                    for _it, _reason in _dropped[:5]:
                        _label = (getattr(_it, "entry", "")
                                  or f"key={getattr(_it, 'key', '?')}")
                        drop_lines.append(
                            f"{_label}.{getattr(_it, 'field', '?')} "
                            f"(mod '{_mod_by_intent.get(id(_it), '?')}'): "
                            f"{_reason}")
                    more_n = len(_dropped) - len(drop_lines)
                    more_sfx = (f"; and {more_n} more (see log)"
                                if more_n > 0 else "")
                    logger.warning(
                        "Format 3 %s writer refused %d of %d intent(s); "
                        "the remaining %d applied. Dropped: %s",
                        target, len(_dropped), len(batched), kept_n,
                        "; ".join(
                            f"{(getattr(i, 'entry', '') or getattr(i, 'key', '?'))}"
                            f".{getattr(i, 'field', '?')} "
                            f"('{_mod_by_intent.get(id(i), '?')}': {r})"
                            for i, r in _dropped))
                    if warnings_out is not None:
                        warnings_out.append(
                            f"Format 3: {len(_dropped)} intent(s) on "
                            f"'{target}' could not be applied and were "
                            f"skipped: " + "; ".join(drop_lines) + more_sfx
                            + (f". The other {kept_n} intent(s) on this "
                               f"table still applied." if kept_n else ""))
                if not pabgb_changes:
                    logger.warning(
                        "Format 3 %s: %d intent(s) from %d mod(s) "
                        "produced 0 changes", target, len(batched),
                        len(contributing_mods))
                    if warnings_out is not None:
                        warnings_out.append(
                            f"Format 3 mod(s) "
                            f"{', '.join(repr(n) for n in contributing_mods)} "
                            f"produced 0 byte changes for '{target}'. "
                            f"The targeted entry may not exist in this game "
                            f"version.")
                    continue
                contrib_ids = list(whole_table_mod_ids.get(target, []))
                for c in pabgb_changes:
                    c["_target_file"] = target
                    if contrib_ids:
                        c["_source_mod_ids"] = list(contrib_ids)
                aggregated.setdefault(target, []).extend(pabgb_changes)
                if pabgh_change is not None:
                    pabgh_change["_target_file"] = _companion
                    if contrib_ids:
                        pabgh_change["_source_mod_ids"] = list(contrib_ids)
                    aggregated.setdefault(
                        _companion, []).append(pabgh_change)
                if participating_mod_ids is not None:
                    for mid in contrib_ids:
                        participating_mod_ids.add(mid)
                for c in pabgb_changes:
                    n_bytes_changed += len(c.get("patched", "")) // 2
                logger.info(
                    "Format 3 %s writer: applied %d intent(s) "
                    "across %d mod(s), %d change(s)%s",
                    target, len(batched) - len(_dropped),
                    len(contributing_mods),
                    len(pabgb_changes),
                    ", + pabgh offset rebuild"
                    if pabgh_change is not None else "")
                continue

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
            #
            # Respect a pre-stamped _target_file: the iteminfo/skill
            # whole-table flush emits .pabgh companion changes that must
            # route to the index file, not the table (audit finding A).
            contrib_ids = list(whole_table_mod_ids.get(target, []))
            for c in changes:
                c.setdefault("_target_file", target)
                if contrib_ids:
                    c["_source_mod_ids"] = list(contrib_ids)
                aggregated.setdefault(c["_target_file"], []).append(c)
            # Whole-table dispatch produced bytes for this target , credit
            # every contributing mod as a participant so persist_skip_summary
            # resets their skip rows on a clean apply (H2 fix).
            if participating_mod_ids is not None:
                for mid in whole_table_mod_ids.get(target, []):
                    participating_mod_ids.add(mid)
            for c in changes:
                n_bytes_changed += len(c.get("patched", "")) // 2
            # #105 pitonpp macOS diagnostic: the writer reports applying
            # N intents and produces a single byte-level change covering
            # the whole table. If 'original' (vanilla bytes hex) and
            # 'patched' (modified bytes hex) compare equal, the writer
            # effectively produced vanilla, which mount-time will see as
            # zero byte diff. On Windows this never happens for the same
            # mods; on macOS pitonpp hits this every time. Comparing the
            # two strings here surfaces the exact failure point.
            for c in changes:
                orig_hex = c.get("original", "")
                patched_hex = c.get("patched", "")
                if len(orig_hex) == len(patched_hex) and orig_hex == patched_hex:
                    logger.warning(
                        "Format 3 whole-table writer for %s: produced a "
                        "change at offset %d where original==patched "
                        "(%d bytes). The writer effectively output vanilla "
                        "bytes despite %d batched intent(s). This points to "
                        "the writer failing to mutate the in-memory record "
                        "list before serialise. macOS-specific symptom in "
                        "GitHub #105 pitonpp.",
                        target, c.get("offset", 0), len(orig_hex) // 2,
                        len(batched))
                else:
                    # Find the first byte position where original differs
                    # from patched, so the bundle shows the writer DID
                    # mutate something.
                    first_diff = -1
                    for i in range(min(len(orig_hex), len(patched_hex))):
                        if orig_hex[i] != patched_hex[i]:
                            first_diff = i // 2
                            break
                    logger.info(
                        "Format 3 whole-table writer for %s: first byte "
                        "differs at offset %d, original len=%d patched len=%d",
                        target, first_diff,
                        len(orig_hex) // 2, len(patched_hex) // 2)
            logger.info(
                "Format 3 whole-table writer for %s: applied %d intents "
                "across %d mod(s) in one pass",
                target, len(batched), len(contributing_mods))
        except Exception as e:
            # EXPAND NEVER RAISES contract (docstring): corrupt or
            # unmodeled table bytes can blow an unguarded
            # struct.unpack_from out of the expansion helpers; one
            # bad table must not abort the whole apply. Log, warn
            # with the target + contributing mods, move on.
            logger.error(
                "Format 3 whole-table dispatch for %s crashed: %s",
                target, e, exc_info=True)
            if warnings_out is not None:
                warnings_out.append(
                    f"Format 3 mod(s) "
                    f"{', '.join(repr(n) for n in whole_table_mod_names.get(target, []))} "
                    f"could not apply: processing '{target}' failed "
                    f"({e}). The rest of the apply continued.")
            continue

    # Summary line, INFO level so bug reports auto-include it.
    # The single line summarizes "did the feature do anything?" so
    # users + maintainers can answer that question from the log
    # without digging into per-mod debug entries.
    logger.info(
        "Format 3 apply: %d mod(s) processed, %d byte(s) changed "
        "across %d file(s), %d mod(s) skipped (see warnings).",
        n_mods_processed, n_bytes_changed,
        len(files_touched), n_mods_skipped,
    )


def _build_with_per_intent_refusals(
    build_fn, refused_exc, vanilla_body: bytes, vanilla_header: bytes,
    batched: list,
) -> "tuple[list[dict], dict | None, list[tuple]]":
    """Run a ``(pabgb_changes, pabgh_change)`` writer, degrading
    refusals to per-intent drops instead of aborting the whole
    multi-mod batch (audit finding, 2026-06-11).

    Strategy: try the full batch first (the common case costs one
    build). On a refusal, probe each intent individually to find the
    refused ones, then rebuild once with the survivors. Bounded at
    ``len(batched) + 2`` builds total. Returns
    ``(pabgb_changes, pabgh_change, dropped)`` where ``dropped`` is a
    list of ``(intent, reason)`` pairs.

    Non-refusal exceptions from the first full-batch build propagate
    to the caller (existing crash handling); during per-intent probing
    they just mark that one intent dropped.
    """
    dropped: list[tuple] = []
    try:
        changes, companion = build_fn(
            vanilla_body, vanilla_header, list(batched))
        return changes, companion, dropped
    except refused_exc as first_err:
        logger.warning(
            "Format 3 writer refused the %d-intent batch (%s); "
            "probing per-intent to keep the rest",
            len(batched), first_err)
    survivors = []
    for it in batched:
        try:
            build_fn(vanilla_body, vanilla_header, [it])
            survivors.append(it)
        except refused_exc as e:
            dropped.append((it, str(e)))
        except Exception as e:
            dropped.append((it, f"writer error: {e}"))
    if not survivors:
        return [], None, dropped
    try:
        changes, companion = build_fn(
            vanilla_body, vanilla_header, survivors)
        return changes, companion, dropped
    except refused_exc as e:
        # Refusal only in combination (should not happen for these
        # per-entry writers); conservative fallback drops everything
        # rather than guessing.
        for it in survivors:
            dropped.append((it, f"refused in combination: {e}"))
        return [], None, dropped


def _intents_to_v2_changes(
    target: str, vanilla_body: bytes, vanilla_header: bytes,
    intents: list[Format3Intent],
) -> list[dict]:
    """Produce v2-format change dicts from a list of supported intents.

    Each output dict has: ``entry``, ``rel_offset``, ``original``,
    ``patched``, exactly the shape ``aggregate_json_mods_into_
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

    # characterinfo.pabgb has a CDUMM PABGB schema, but it is a
    # positional name-less decompiled structure, so the generic walker
    # can't resolve a field by name. Route through the clean-room
    # characterinfo writer, which walks each record to the action-chart
    # block and writes the five fields Format 3 character-swap mods use
    # (GitHub #150).
    if table_name == "characterinfo":
        return _characterinfo_intents_to_changes(
            vanilla_body, vanilla_header, intents)

    has_cdumm_schema = has_schema(table_name)
    # Tables without a CDUMM PABGB schema are still processable when
    # intents route through either:
    #   1. a registered list writer (e.g. skill.pabgb via the vendored
    #      skillinfo_parser), or
    #   2. a community-curated field_schema/<table>.json entry that
    #      gives a tid/offset/type for primitive writes.
    if not has_cdumm_schema:
        fs_entries_no_schema = load_field_schema(table_name)
        list_routable = [
            i for i in intents
            if (table_name, i.field) in LIST_WRITERS
        ]
        fs_routable = [
            i for i in intents
            if (table_name, i.field) not in LIST_WRITERS
            and i.field in fs_entries_no_schema
            and i.old is None
        ]
        raw_routable = [
            i for i in intents
            if (table_name, i.field) not in LIST_WRITERS
            and i.old is not None
        ]
        if not list_routable and not fs_routable and not raw_routable:
            return []
        out: list[dict] = []
        # Whole-table writer dispatch for the list-routable batch.
        if list_routable:
            # Carry the raw intents on the change so the apply loop
            # can REBUILD the whole-table bytes against the actual
            # buffer when the prebuilt original mismatches. A
            # whole-table change's `original` is the full table, so a
            # single stale byte anywhere (contaminated vanilla backup,
            # composition with another mod) used to skip the entire
            # batch (falobos76's v3.3.19 retest on #191).
            if table_name == "iteminfo":
                from cdumm.engine.iteminfo_writer import (
                    build_iteminfo_intent_change,
                )
                change = build_iteminfo_intent_change(
                    vanilla_body, list(list_routable),
                    vanilla_header=vanilla_header)
                if change is not None:
                    change["_f3_rebuild"] = {
                        "table": "iteminfo",
                        "intents": _portable_intents(list_routable),
                        # The live rebuild needs the index for exact
                        # record framing (the sniff walk swallows
                        # large-key records, audit M12) and so its
                        # pre-flight refuses size-diverged buffers
                        # whose prebuilt .pabgh companion would no
                        # longer match (release-review finding 3).
                        "header": vanilla_header.hex(),
                    }
                    _route_pabgh_companion(change, target, out)
                    out.append(change)
            elif table_name == "skill":
                from cdumm.engine.skill_writer import (
                    build_skill_intent_change,
                )
                change = build_skill_intent_change(
                    vanilla_body, vanilla_header, list(list_routable))
                if change is not None:
                    change["_f3_rebuild"] = {
                        "table": "skill",
                        "intents": _portable_intents(list_routable),
                        "header": vanilla_header.hex(),
                    }
                    _route_pabgh_companion(change, target, out)
                    out.append(change)
        # Per-record field_schema dispatch for primitive intents on
        # no-PABGB-schema tables. Mirrors the standard primitive path
        # below but skips the PABGB schema walk entirely (we have a
        # tid/offset directly from the community schema).
        if fs_routable:
            key_size_ns, offsets_ns = parse_pabgh_index(
                vanilla_header, table_name)
            if offsets_ns and key_size_ns in (2, 4):
                sorted_ns = sorted(
                    offsets_ns.items(), key=lambda kv: kv[1])
                bounds_ns: dict[int, tuple[int, int, str]] = {}
                for idx, (k, off) in enumerate(sorted_ns):
                    end = (
                        sorted_ns[idx + 1][1]
                        if idx + 1 < len(sorted_ns)
                        else len(vanilla_body)
                    )
                    name = _entry_name(vanilla_body, off, key_size_ns)
                    bounds_ns[k] = (off, end, name)
                for intent in fs_routable:
                    if intent.key not in bounds_ns:
                        continue
                    entry_off_ns, entry_end_ns, entry_name_ns = (
                        bounds_ns[intent.key])
                    payload_off_ns = _payload_offset(
                        vanilla_body, entry_off_ns, key_size_ns)
                    if payload_off_ns is None:
                        continue
                    fs_entry = fs_entries_no_schema[intent.field]
                    fmt_size = DTYPE_TABLE.get(
                        fs_entry.data_type.lower())
                    if fmt_size is None:
                        continue
                    fmt_ns, size_ns = fmt_size
                    abs_off_ns = locate_field(
                        vanilla_body, payload_off_ns,
                        entry_end_ns, fs_entry)
                    if abs_off_ns is None:
                        continue
                    if abs_off_ns + size_ns > entry_end_ns:
                        continue
                    try:
                        new_bytes_ns = struct.pack(
                            f"<{fmt_ns}", intent.new)
                    except struct.error:
                        continue
                    if len(new_bytes_ns) != size_ns:
                        continue
                    original_ns = bytes(
                        vanilla_body[abs_off_ns:abs_off_ns + size_ns])
                    eid_size_ns = 2 if key_size_ns == 2 else 4
                    name_len_ns = struct.unpack_from(
                        "<I", vanilla_body,
                        entry_off_ns + eid_size_ns)[0]
                    name_end_ns = (
                        entry_off_ns + eid_size_ns + 4 + name_len_ns)
                    rel_offset_ns = abs_off_ns - name_end_ns
                    out.append({
                        "entry": entry_name_ns or intent.entry,
                        "rel_offset": rel_offset_ns,
                        "original": original_ns.hex(),
                        "patched": new_bytes_ns.hex(),
                        "label": f"{intent.entry}.{intent.field}",
                    })
        # Raw-record replacement: intent ships full old + new hex
        # blobs and we search the entry's payload for an exact-once
        # match of old. Used by _buff_data_raw style intents on
        # skill.pabgb where the mod author exports the vanilla
        # bytes alongside their modded bytes (voiddoiv contribution
        # 2026-05-08).
        if raw_routable:
            key_size_rr, offsets_rr = parse_pabgh_index(
                vanilla_header, table_name)
            if offsets_rr and key_size_rr in (2, 4):
                sorted_rr = sorted(
                    offsets_rr.items(), key=lambda kv: kv[1])
                bounds_rr: dict[int, tuple[int, int, str]] = {}
                for idx, (k, off) in enumerate(sorted_rr):
                    end = (
                        sorted_rr[idx + 1][1]
                        if idx + 1 < len(sorted_rr)
                        else len(vanilla_body)
                    )
                    name = _entry_name(vanilla_body, off, key_size_rr)
                    bounds_rr[k] = (off, end, name)
                for intent in raw_routable:
                    if intent.key not in bounds_rr:
                        continue
                    entry_off_rr, entry_end_rr, entry_name_rr = (
                        bounds_rr[intent.key])
                    try:
                        old_bytes = bytes.fromhex(intent.old)
                        new_bytes_rr = bytes.fromhex(str(intent.new))
                    except (ValueError, TypeError):
                        logger.warning(
                            "Format 3 raw intent for %s.%s on entry "
                            "key=%d skipped: old/new are not valid "
                            "hex strings",
                            table_name, intent.field, intent.key)
                        continue
                    if not old_bytes or not new_bytes_rr:
                        continue
                    if len(old_bytes) != len(new_bytes_rr):
                        logger.warning(
                            "Format 3 raw intent for %s.%s on entry "
                            "key=%d skipped: old (%d bytes) and new "
                            "(%d bytes) must be equal length",
                            table_name, intent.field, intent.key,
                            len(old_bytes), len(new_bytes_rr))
                        continue
                    region = bytes(
                        vanilla_body[entry_off_rr:entry_end_rr])
                    first = region.find(old_bytes)
                    if first == -1:
                        logger.warning(
                            "Format 3 raw intent for %s.%s on entry "
                            "key=%d (%s) skipped: 'old' bytes not "
                            "found in entry payload (mod expects "
                            "different vanilla bytes than this game "
                            "version ships)",
                            table_name, intent.field, intent.key,
                            entry_name_rr or intent.entry)
                        continue
                    second = region.find(old_bytes, first + 1)
                    if second != -1:
                        logger.warning(
                            "Format 3 raw intent for %s.%s on entry "
                            "key=%d (%s) skipped: 'old' bytes match "
                            "at multiple positions (%d and %d) "
                            "inside entry, refusing to guess which "
                            "one to replace",
                            table_name, intent.field, intent.key,
                            entry_name_rr or intent.entry,
                            first, second)
                        continue
                    abs_off_rr = entry_off_rr + first
                    eid_size_rr = 2 if key_size_rr == 2 else 4
                    name_len_rr = struct.unpack_from(
                        "<I", vanilla_body,
                        entry_off_rr + eid_size_rr)[0]
                    name_end_rr = (
                        entry_off_rr + eid_size_rr + 4 + name_len_rr)
                    rel_offset_rr = abs_off_rr - name_end_rr
                    out.append({
                        "entry": entry_name_rr or intent.entry,
                        "rel_offset": rel_offset_rr,
                        "original": old_bytes.hex(),
                        "patched": new_bytes_rr.hex(),
                        "label": f"{intent.entry}.{intent.field}",
                    })
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
    # Iteminfo nested paths the native writer's path-resolver can
    # walk (prefab_data_list[N].xxx, drop_default_data.xxx,
    # gimmick_visual_prefab_data_list[N].xxx). These don't appear in
    # LIST_WRITERS because their field names are dynamic per-record,
    # but the writer handles them in its parsed-dict pass.
    def _is_iteminfo_nested_path(field: str) -> bool:
        return bool(field) and (
            field.startswith("prefab_data_list[")
            or field.startswith("drop_default_data.")
            or field.startswith("gimmick_visual_prefab_data_list[")
        )

    # ALL iteminfo intents route through the native writer, not just
    # list-of-dict batches. The PABGB schema walk below still carries
    # the pre-1.09 layout (schemas/pabgb_type_overrides.json lists
    # fields removed in 1.09/1.10 and lacks the added ones), so any
    # primitive intent on a field at/after _itemIconList walks with
    # wrong byte counts on a 1.10 binary: usually a silent drop, worst
    # case an in-bounds wrong-offset write (audit finding C,
    # 2026-06-10). The native parser tracks the live layout and the
    # writer handles primitives via dict assignment, so it is the
    # version-correct path for every iteminfo field.
    iteminfo_force_batch = (table_name == "iteminfo")
    skill_force_batch = (
        table_name == "skill" and any(
            (table_name, i.field) in LIST_WRITERS for i in intents
        )
    )

    for intent in intents:
        # Batched whole-table writer dispatch (forced for iteminfo /
        # skill when any list-writer intent exists in the same mod).
        # These MUST run before the entry_bounds gate below: key-
        # omitted intents arrive with the sentinel key=0, which is
        # never in the pabgh index, and the writers resolve them by
        # entry NAME. Gating first silently dropped every name-only
        # iteminfo/skill intent on the production (has-schema) path
        # (release-review finding 1, 2026-06-11).
        if iteminfo_force_batch and table_name == "iteminfo":
            iteminfo_batch.append(intent)
            continue
        if skill_force_batch and table_name == "skill":
            skill_batch.append(intent)
            continue
        # Per-list-writer-only path (no primitives mixed in).
        if (table_name == "iteminfo"
                and ((table_name, intent.field) in LIST_WRITERS
                     or _is_iteminfo_nested_path(intent.field))):
            iteminfo_batch.append(intent)
            continue
        if (table_name == "skill"
                and (table_name, intent.field) in LIST_WRITERS):
            skill_batch.append(intent)
            continue

        if intent.key not in entry_bounds:
            continue
        entry_off, entry_end, entry_name = entry_bounds[intent.key]

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
        # v3.2.3 when Format 3 primitive support shipped, the
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
    # iteminfo.pabgb body, plus a .pabgh companion when record
    # offsets shifted (audit finding A).
    #
    # `_f3_rebuild` (the raw intents riding on the change) MUST be
    # attached HERE, not only in the no-schema branch above:
    # has_schema("iteminfo") is True, so production iteminfo flows
    # through THIS flush, and the v3.3.20 live-buffer rebuild never
    # fired for iteminfo until this was added (audit finding B,
    # 2026-06-10).
    if iteminfo_batch:
        from cdumm.engine.iteminfo_writer import (
            build_iteminfo_intent_change,
        )
        iteminfo_change = build_iteminfo_intent_change(
            vanilla_body, iteminfo_batch, vanilla_header=vanilla_header)
        if iteminfo_change is not None:
            iteminfo_change["_f3_rebuild"] = {
                "table": "iteminfo",
                "intents": _portable_intents(iteminfo_batch),
                # See the no-schema attach site: the live rebuild
                # needs the index for exact record framing and for
                # the size-divergence refusal (release-review
                # finding 3, 2026-06-11).
                "header": vanilla_header.hex(),
            }
            _route_pabgh_companion(iteminfo_change, target, out)
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
            skill_change["_f3_rebuild"] = {
                "table": "skill",
                "intents": _portable_intents(skill_batch),
                "header": vanilla_header.hex(),
            }
            _route_pabgh_companion(skill_change, target, out)
            out.append(skill_change)

    return out


def _portable_intents(items) -> list[dict]:
    """JSON-serializable copies of intents, carried on whole-table
    changes so the apply loop can re-run the writer against the live
    buffer on a byte mismatch (#191)."""
    return [
        {"entry": i.entry, "key": i.key, "field": i.field,
         "op": i.op, "new": i.new, "old": i.old}
        for i in items
    ]


def _route_pabgh_companion(change: dict, target: str, out: list) -> None:
    """Pop a writer-attached ``_pabgh_companion`` off ``change`` and
    append it to ``out`` pre-stamped with the companion target file,
    so the caller's routing sends it to <table>.pabgh instead of the
    .pabgb (audit finding A)."""
    companion = change.pop("_pabgh_companion", None)
    if companion is None:
        return
    companion["_target_file"] = target.replace(".pabgb", ".pabgh")
    out.append(companion)


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


def _characterinfo_intents_to_changes(
    vanilla_body: bytes, vanilla_header: bytes,
    intents: list[Format3Intent],
) -> list[dict]:
    """Resolve characterinfo intents through the clean-room
    characterinfo writer (GitHub #150).

    The writer walks each record to the action-chart / skeleton block
    and resolves the five supported fields by name. Intents on
    unsupported fields, missing records, or out-of-range values are
    dropped inside the writer with a logged warning; the expand_format3
    caller surfaces a "0 byte changes" warning for whole-mod dropouts.
    """
    from cdumm.engine.characterinfo_writer import (
        build_characterinfo_changes,
    )
    tuples = [
        (i.entry, i.key, i.field, i.new) for i in intents
    ]
    return build_characterinfo_changes(
        vanilla_body, vanilla_header, tuples)


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
    # Honor the verified-only gate. A table may mark which fields have been
    # validated against real record data (schemas/pabgb_type_overrides.json
    # `_verified_fields`). Fields it doesn't vouch for render `(unverified)`
    # in the grid because their real offset isn't proven — so a Format 3 mod
    # must not write to them either, or the write could land on the wrong
    # byte. Only affects tables that opt in; every other table is unchanged.
    vf = getattr(schema, "verified_fields", None)
    if vf is not None and target_name not in vf:
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
      1. ``spec.type_descriptor`` (Path B override), delegate to
         ``pabgb_types.consume_bytes`` for full PABGB primitive +
         CArray + COptional + tagged-variant + sub-struct support.
      2. Legacy ``CString`` literal in ``spec.field_type``.
      3. Legacy ``stream_size`` for fixed-size fields.
      4. None (caller must bail).
    """
    # Defensive negative-offset guard mirroring pabgb_types.consume_bytes.
    # struct.unpack_from with a negative offset reads from the buffer's
    # end and raises struct.error when there aren't enough bytes, the
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
        # AND the buffer. Iteration 6 systematic-debugging finding , 
        # the previous unconditional `return spec.stream_size` would
        # report a successful consume even past EOF, breaking
        # downstream offset accounting.
        if off + spec.stream_size > min(entry_end, len(body)):
            return None
        return spec.stream_size
    # stream_size=0 means the schema didn't classify this, can't
    # walk safely.
    return None


def _payload_offset(body: bytes, entry_off: int,
                    key_size: int,
                    no_null_skip: bool = False,
                    no_entry_header: bool = False) -> "int | None":
    """Return the byte offset where the entry's first payload field starts.

    Three modes (in priority order):

    * ``no_entry_header=True``, payload IS the entry; return ``entry_off``
      verbatim. Required for tables like RegionInfo where ``_key`` and
      ``_stringKey`` are regular schema fields (no separate header).

    * ``no_null_skip=True``, skip the standard entry header (entry_id +
      name_len + name) but do NOT skip a trailing zero byte. Required for
      ItemInfo, VehicleInfo, FieldInfo, StageInfo where the byte after
      the name is a real ``_isBlocked`` u8 field, not padding.

    * Default, legacy heuristic from ``format3_handler``: skip a single
      0 byte after the name when present. Works for tables where the
      post-name byte is genuinely padding.
    """
    if no_entry_header:
        # Strict `<` so EOF itself is rejected, there's no field to
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
    failure, the caller falls back to ``intent.entry``."""
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
