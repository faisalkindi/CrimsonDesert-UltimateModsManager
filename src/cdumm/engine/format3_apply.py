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
) -> None:
    """For each enabled mod whose json_source is a Format 3 file,
    resolve its intents into v2-style change dicts and append to
    ``aggregated[target]``.

    Mutates ``aggregated`` and ``signatures`` in place. Never raises.
    Logs at warning on extraction failures, malformed JSON, and
    unsupported tables; logs at debug on successful expansion.
    """
    rows = db.connection.execute(
        "SELECT id, name, json_source, priority FROM mods "
        "WHERE enabled = 1 AND json_source IS NOT NULL "
        "AND json_source != '' "
        "ORDER BY priority DESC, id ASC"
    ).fetchall()

    for mod_id, mod_name, json_source, _priority in rows:
        try:
            jp = Path(json_source)
            if not jp.exists():
                continue
            target, intents = parse_format3_mod(jp)
        except (ValueError, OSError):
            # Either not Format 3 (v2 path already handled it), or
            # the file is malformed. Either way, skip silently —
            # the v2 aggregator already logged its own parse errors
            # for the same file.
            continue

        # Validate intents against the schema + community field_schema
        validation = validate_intents(target, intents)
        if not validation.supported:
            logger.warning(
                "Format 3 mod '%s' (id=%d): no supported intents "
                "for %s — %d skipped. Mod produced 0 byte changes.",
                mod_name, mod_id, target,
                len(validation.skipped))
            continue

        # Extract vanilla bytes for the target file
        vanilla = vanilla_extractor(target)
        if vanilla is None:
            logger.warning(
                "Format 3 mod '%s' (id=%d): vanilla extraction "
                "failed for %s; skipping.",
                mod_name, mod_id, target)
            continue
        vanilla_body, vanilla_header = vanilla

        # Convert each supported intent into a v2-style change dict
        changes = _intents_to_v2_changes(
            target, vanilla_body, vanilla_header, validation.supported)
        if not changes:
            # Don't pollute aggregated with empty lists.
            logger.debug(
                "Format 3 mod '%s' (id=%d): all %d supported intents "
                "resolved to zero changes (probably TID-not-found "
                "or value out of range).",
                mod_name, mod_id, len(validation.supported))
            continue

        aggregated.setdefault(target, []).extend(changes)
        logger.debug(
            "Format 3 mod '%s' (id=%d): expanded %d intents into "
            "%d changes on %s",
            mod_name, mod_id, len(validation.supported),
            len(changes), target)


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
    if not has_schema(table_name):
        return []

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

    out: list[dict] = []
    for intent in intents:
        if intent.key not in entry_bounds:
            continue
        entry_off, entry_end, entry_name = entry_bounds[intent.key]
        payload_off = _payload_offset(vanilla_body, entry_off, key_size)
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
        rel_offset = abs_off - entry_off

        out.append({
            "entry": entry_name or intent.entry,
            "rel_offset": rel_offset,
            "original": original_bytes.hex(),
            "patched": new_bytes.hex(),
            "label": f"{intent.entry}.{intent.field}",
        })
    return out


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

    spec = field_specs.get(intent.field)
    if spec is None or not spec.struct_fmt or not spec.stream_size:
        return None
    # Walk schema fields up to target, summing stream sizes
    rel = 0
    for f in schema.fields:
        if f.name == intent.field:
            return payload_off + rel, spec.stream_size, spec.struct_fmt
        if not f.stream_size:
            return None  # variable-length field blocks the walk
        rel += f.stream_size
    return None


def _payload_offset(body: bytes, entry_off: int,
                    key_size: int) -> "int | None":
    """Same logic as format3_handler._entry_payload_offset."""
    eid_size = 2 if key_size == 2 else 4
    head_size = eid_size + 4
    if entry_off + head_size > len(body):
        return None
    name_len = struct.unpack_from("<I", body, entry_off + eid_size)[0]
    if name_len > 500 or entry_off + head_size + name_len > len(body):
        return None
    name_end = entry_off + head_size + name_len
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
