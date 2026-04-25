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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cdumm.semantic.parser import get_schema, has_schema

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
        for required in ("entry", "field", "op"):
            if required not in raw:
                raise ValueError(
                    f"intent #{i} is missing required key '{required}'"
                )
        if "new" not in raw:
            raise ValueError(
                f"intent #{i} is missing 'new' (the value to set)"
            )
        intents.append(Format3Intent(
            entry=str(raw["entry"]),
            key=int(raw.get("key", 0)),
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

    for intent in intents:
        reason = _classify_intent(intent, field_specs, table_name)
        if reason is None:
            result.supported.append(intent)
        else:
            result.skipped.append((intent, reason))

    return result


def _classify_intent(
    intent: Format3Intent, field_specs: dict, table_name: str
) -> str | None:
    """Return ``None`` if the intent is supported, otherwise a
    human-readable reason for skipping it."""
    if intent.op not in _SUPPORTED_OPS:
        return (
            f"op '{intent.op}' not supported in Phase 1 "
            f"(only 'set' is implemented)"
        )

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
            f"field '{intent.field}' not found in schema — "
            f"may need a friendly-name → schema-name mapping "
            f"(NattKh's tools translate names like 'drops' → "
            f"'_list' internally; that mapping isn't published yet)"
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


import struct  # noqa: E402  -- binary writer below uses this

from cdumm.semantic.parser import parse_pabgh_index  # noqa: E402


def _entry_payload_offset(body: bytes, entry_offset: int) -> int | None:
    """Return the absolute byte offset where the payload starts
    inside the entry at ``entry_offset``.

    Entry header: u32 entry_id + u32 name_len + name UTF-8 + 0x00.
    """
    if entry_offset + 8 > len(body):
        return None
    name_len = struct.unpack_from("<I", body, entry_offset + 4)[0]
    if name_len > 500 or entry_offset + 8 + name_len > len(body):
        return None
    name_end = entry_offset + 8 + name_len
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
    """
    offset = 0
    for spec in schema.fields:
        if spec.name == target_field:
            return offset, spec
        if not spec.stream_size:
            # Variable-length / unknown field before our target
            # invalidates the byte offset — we cannot find the
            # target deterministically without parsing the
            # variable-length field's contents first.
            return None
        offset += spec.stream_size
    return None


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
    _, offsets = parse_pabgh_index(vanilla_header, table_name)
    if not offsets:
        return vanilla_body

    body = bytearray(vanilla_body)
    field_specs = {f.name: f for f in schema.fields}

    for intent in intents:
        if intent.op not in _SUPPORTED_OPS:
            continue
        if intent.key not in offsets:
            continue
        if intent.field not in field_specs:
            continue

        entry_off = offsets[intent.key]
        payload_off = _entry_payload_offset(body, entry_off)
        if payload_off is None:
            continue

        located = _field_offset_in_payload(schema, intent.field)
        if located is None:
            continue
        rel_off, spec = located
        abs_off = payload_off + rel_off

        packed = _pack_value(intent.new, spec)
        if packed is None or len(packed) != spec.stream_size:
            continue
        if abs_off + spec.stream_size > len(body):
            continue

        body[abs_off:abs_off + spec.stream_size] = packed

    return bytes(body)
