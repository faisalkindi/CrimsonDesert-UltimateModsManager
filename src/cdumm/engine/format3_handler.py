"""NattKh Format 3 (field-names) JSON mod handler.

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
name → schema-name translation layer NattKh uses internally
(``drops`` → ``_list``), and ops other than ``"set"`` (``add_entry``,
``remove``, ``append``, etc.) are deferred to later phases.
"""
from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
                    with open(path, "r", encoding="utf-8") as f:
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
    """A single semantic intent from a Format 3 mod."""
    entry: str
    key: int
    field: str
    op: str
    new: Any


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


def parse_format3_mod(path: Path) -> tuple[str, list[Format3Intent]]:
    """Read and validate the structural shape of a Format 3 file.

    Returns ``(target, intents)``. Raises ``ValueError`` with a
    user-facing message when the file is malformed — the importer
    surfaces those messages directly.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"Cannot read Format 3 file: {e}") from e

    if not isinstance(data, dict) or data.get("format") != 3:
        raise ValueError(
            "Not a Format 3 file — missing or wrong "
            "\"format\": 3 marker"
        )

    target = data.get("target")
    if not isinstance(target, str) or not target:
        raise ValueError(
            "Format 3 file is missing a \"target\" string "
            "(should name the .pabgb file the mod modifies)"
        )

    raw_intents = data.get("intents")
    if not isinstance(raw_intents, list):
        raise ValueError(
            "Format 3 file is missing an \"intents\" list"
        )

    intents: list[Format3Intent] = []
    for i, raw in enumerate(raw_intents):
        if not isinstance(raw, dict):
            raise ValueError(
                f"intent #{i} is not a JSON object"
            )
        # Required keys per the v3 spec example file
        for required in ("entry", "key", "field", "op"):
            if required not in raw:
                raise ValueError(
                    f"intent #{i} is missing required key '{required}'"
                )
        if "new" not in raw:
            raise ValueError(
                f"intent #{i} is missing 'new' (the value to set)"
            )
        # ``key`` is the record id — silently coercing strings or
        # truncating floats would silently target a wrong record.
        # Refuse anything that isn't an int (booleans pass isinstance
        # check for int in Python, so reject explicitly).
        raw_key = raw["key"]
        if isinstance(raw_key, bool) or not isinstance(raw_key, int):
            raise ValueError(
                f"intent #{i} has non-integer key {raw_key!r} — "
                f"key must be an integer record id"
            )
        intents.append(Format3Intent(
            entry=str(raw["entry"]),
            key=raw_key,
            field=str(raw["field"]),
            op=str(raw["op"]),
            new=raw["new"],
        ))

    return target, intents


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


def validate_intents(
    target: str, intents: list[Format3Intent]
) -> Format3Validation:
    """Partition intents into supported (Phase 1 can apply) and
    skipped (Phase 1 cannot, with a per-intent reason).
    """
    result = Format3Validation()
    table_name = _table_name_from_target(target)

    if not has_schema(table_name):
        reason = (
            f"target '{target}' has no schema in CDUMM "
            f"(table '{table_name}' not in pabgb_complete_schema.json)"
        )
        for intent in intents:
            result.skipped.append((intent, reason))
        return result

    schema = get_schema(table_name)
    # Map field name → spec for O(1) lookups.
    field_specs = {f.name: f for f in schema.fields}
    # Community-curated field schema (JMM-compatible). Empty if no
    # field_schema/<table>.json exists yet — that's normal.
    fs_entries = load_field_schema(table_name)

    for intent in intents:
        reason = _classify_intent(
            intent, schema, field_specs, fs_entries, table_name)
        if reason is None:
            result.supported.append(intent)
        else:
            result.skipped.append((intent, reason))

    return result


def _classify_intent(
    intent: Format3Intent, schema, field_specs: dict,
    fs_entries: dict, table_name: str
) -> str | None:
    """Return ``None`` if the intent is supported, otherwise a
    human-readable reason for skipping it.

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

    # Community-curated field_schema wins
    if intent.field in fs_entries:
        return None

    spec = field_specs.get(intent.field)
    if spec is None:
        # parser.py drops variable-length fields (stream=None) from
        # the loaded schema, so the field-not-found path can't tell
        # "really doesn't exist" from "exists but is an array we
        # can't write yet". Hit the raw schema for a better message.
        raw = _raw_field_metadata(table_name, intent.field)
        if raw is not None and (
            raw.get("stream") is None
            or raw.get("type") is None
        ):
            return (
                f"field '{intent.field}' is a variable-length / "
                f"array field (stream=None in schema); writing "
                f"variable-length data lands in a later phase"
            )
        return (
            f"field '{intent.field}' has no field_schema entry "
            f"and isn't in the PABGB record schema — "
            f"author needs to add a field_schema/{table_name}.json "
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
    if not _field_walker_reachable(schema, intent.field):
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
    """
    from cdumm.semantic.pabgb_types import is_known_type
    for spec in schema.fields:
        if spec.name == target_field:
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
