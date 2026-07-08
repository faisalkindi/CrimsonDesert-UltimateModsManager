"""Semantic parser for PABGB game data tables.

Parses binary PABGB files into structured records using the PABGH index
for entry boundaries. Each record becomes a dict of {field_name: value}
that can be diffed and merged at the field level.

PABGB format:
  .pabgh (index): count + N × (key + offset)
  .pabgb (body): sequential binary records at offsets from index
  Entry: u32 entry_id + u32 name_len + UTF-8 name + null + payload

Schema source: NattKh/CrimsonDesertModdingTools pabgb_complete_schema.json
  434 tables, 3708 fields from IDA Pro decompilation.
"""
from __future__ import annotations

import json
import logging
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tables that use u32 count instead of u16 in pabgh header.
# From Potter's pycrimson: these specific tables have 4-byte count prefix.
UINT_COUNT_TABLES = frozenset({
    "characterappearanceindexinfo", "globalstagesequencerinfo",
    "sequencerspawninfo", "sheetmusicinfo", "spawningpoolautospawninfo",
    "itemuseinfo", "terrainregionautospawninfo", "textguideinfo",
    "validscheduleaction", "stageinfo", "questinfo", "gimmickeventtableinfo",
    "reviepointinfo", "aidialogstringinfo", "dialogsetinfo",
    "vibratepatterninfo", "platformachievementinfo",
    "levelgimmicksceneobjectinfo", "fieldlevelnametableinfo", "levelinfo",
    "board", "gameplaytrigger", "characterchange", "materialrelationinfo",
})

# Type → struct format + size mapping for the community schema types
_TYPE_MAP: dict[str, tuple[str, int]] = {
    "direct_u8": ("B", 1),
    "direct_u16": ("H", 2),
    "direct_u32": ("I", 4),
    "direct_u64": ("Q", 8),
    "direct_i8": ("b", 1),
    "direct_i16": ("h", 2),
    "direct_i32": ("i", 4),
    "direct_i64": ("q", 8),
    "direct_f32": ("f", 4),
    "direct_4B": ("I", 4),
    "direct_8B": ("Q", 8),
    "direct_12B": (None, 12),   # raw bytes, no struct format
    "direct_16B": (None, 16),
}


@dataclass(frozen=True)
class FieldSpec:
    """Schema definition for a single field in a PABGB record."""
    name: str
    stream_size: int         # bytes consumed from binary stream (0 if variable/walker-driven)
    field_type: str          # from schema: "direct_u32", "CString", etc.
    struct_fmt: str | None   # struct format char, None for complex types
    type_descriptor: str | None = None
    """Override type descriptor for the pabgb_types walker (e.g. ``"u32"``,
    ``"CArray<OccupiedEquipSlotData>"``, ``"COptional<DockingChildData>"``).
    Set when ``schemas/pabgb_type_overrides.json`` provides an entry for this
    field. Lets the format3 walker consume variable-length fields that the
    base schema marks as ``stream=?``. None = no override; use
    legacy field_type/stream_size logic."""


@dataclass
class TableSchema:
    """Schema for a PABGB table — field definitions in read order."""
    table_name: str
    fields: list[FieldSpec]
    no_null_skip: bool = False
    """Path B: when True, the apply pipeline must NOT use CDUMM's
    `_payload_offset` null-terminator skip for this table. The byte
    after the entry name is the first real field (e.g. ItemInfo's
    ``_isBlocked``), not a null. Set via ``_no_null_skip: true`` in
    the type-override file. Empirically required for ItemInfo."""
    no_entry_header: bool = False
    """Path B: when True, ``_payload_offset`` returns the raw pabgh
    entry offset — there is no CDUMM-style entry header (entry_id +
    name_len + name) to skip. The schema's first field is at byte 0
    of the entry. Set via ``_no_entry_header: true`` in the override
    file. Empirically required for RegionInfo (the upstream parser
    notes: 'There is NO per-entry name header in RegionInfo PABGB.
    The _key and _stringKey are regular fields read by the table
    reader')."""
    verified_fields: frozenset[str] | None = None
    """Verified-only display gate. ``None`` = table not yet hand-curated;
    show every schema field as decoded (legacy behavior). A set = only
    these fields have been validated against real record data; the GUI
    renders any other field as ``(unverified)`` instead of a possibly-
    wrong value. Set via ``_verified_fields: [...]`` in the override file.
    ``_key`` / ``_name`` are always trustworthy and never gated."""

    @property
    def fixed_record_size(self) -> int | None:
        """Total size if all fields are fixed-width, None if variable."""
        total = 0
        for f in self.fields:
            if f.field_type == "CString" or f.field_type.startswith("array"):
                return None  # variable-length
            total += f.stream_size
        return total


# ── Schema loading ──────────────────────────────────────────────────────────

_loaded_schemas: dict[str, TableSchema] | None = None


def _load_schemas() -> dict[str, TableSchema]:
    """Load table schemas from the bundled JSON schema file."""
    global _loaded_schemas
    if _loaded_schemas is not None:
        return _loaded_schemas

    # Look for schema file in multiple locations
    candidates = [
        Path(__file__).parent.parent.parent / "schemas" / "pabgb_complete_schema.json",
        Path(__file__).parent.parent.parent.parent / "schemas" / "pabgb_complete_schema.json",
    ]

    # Also check PyInstaller _MEIPASS
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys._MEIPASS) / "schemas" / "pabgb_complete_schema.json")

    schema_path = None
    for p in candidates:
        if p.exists():
            schema_path = p
            break

    if schema_path is None:
        logger.warning("PABGB schema file not found, semantic parsing unavailable")
        _loaded_schemas = {}
        return _loaded_schemas

    try:
        # utf-8-sig matches the override loader so a BOM-prefixed
        # base schema (e.g. edited via Notepad) doesn't silently
        # disable semantic parsing.
        with open(schema_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("Failed to load PABGB schema: %s", e)
        _loaded_schemas = {}
        return _loaded_schemas

    # Load type-descriptor overrides (Path B — fills the base schema's
    # stream=? gaps using crimson-rs-derived types). Same lookup order
    # as the base schema.
    overrides = _load_type_overrides(schema_path.parent)

    schemas: dict[str, TableSchema] = {}
    for table_name, fields_raw in raw.items():
        table_overrides = overrides.get(table_name, {})
        no_null_skip = bool(table_overrides.get("_no_null_skip", False))
        no_entry_header = bool(table_overrides.get("_no_entry_header", False))
        # `_verified_fields`: opt-in list of hand-validated fields. When
        # present, the GUI shows only these as decoded values and marks
        # every other field `(unverified)` — so a table can be worked
        # incrementally without ever presenting an unproven column.
        vf_raw = table_overrides.get("_verified_fields")
        verified_fields = frozenset(vf_raw) if vf_raw is not None else None
        # `_ordered_fields` REPLACES the upstream schema's array order.
        # The upstream schema sorts by memory address; the actual
        # on-disk deserialization order often differs (verified
        # empirically against vanilla ItemInfo 2026-04-27). When
        # provided, build the schema from this list, picking each
        # field's metadata from the upstream array (or from the
        # override entry).
        ordered = table_overrides.get("_ordered_fields")
        if ordered is not None:
            base_by_name = {fr.get("f", ""): fr for fr in fields_raw}
            # Refuse to load the table if any _ordered_fields entry has
            # neither a base schema entry NOR a type override providing
            # its width. The legacy fallback would silently drop such
            # fields, shifting every later field's offset (CONSENSUS-1
            # adversarial review finding 2026-04-27). Loud failure
            # forces author to fix the typo or add the override.
            unmatched = [
                fname for fname in ordered
                if fname not in base_by_name
                and not (table_overrides.get(fname) or {}).get("type")
            ]
            if unmatched:
                logger.error(
                    "Schema override for table '%s' lists fields in "
                    "_ordered_fields that have no base schema entry "
                    "AND no type override: %s. Refusing to load this "
                    "table — the silent drop would shift all later "
                    "field offsets. Either fix the typo or add a "
                    "type override for each listed field.",
                    table_name, unmatched)
                continue
            fields_raw = []
            for fname in ordered:
                base = base_by_name.get(fname, {"f": fname})
                fields_raw.append(base)
        fields = []
        for fr in fields_raw:
            fname = fr.get("f", "")
            ftype = fr.get("type", "")
            stream = fr.get("stream", 0)

            if not fname:
                continue

            # Apply type override if present. Overrides set type_descriptor;
            # the format3 walker (pabgb_types.consume_bytes) drives byte
            # consumption from the descriptor, so stream_size becomes a
            # hint rather than the source of truth. We still record the
            # primitive width when the descriptor is a known fixed type
            # so legacy callers that read stream_size keep working.
            override = table_overrides.get(fname)
            type_descriptor = None
            if override is not None:
                type_descriptor = override.get("type")
                # If the override is a primitive, set stream_size +
                # struct_fmt so the existing parse_record_payload /
                # _intents_to_v2_changes paths keep working without
                # extra wiring.
                from cdumm.semantic.pabgb_types import (
                    primitive_width, is_known_type,
                )
                prim_size = primitive_width(type_descriptor or "")
                if prim_size is not None:
                    stream = prim_size
                    ftype = f"direct_{type_descriptor}"
                else:
                    # Variable-length descriptor. Stream_size=0 means
                    # "ask the walker"; field_type stays whatever the
                    # upstream schema had (often `?` or
                    # `array_or_complex`) for trace.
                    stream = 0
                if not is_known_type(type_descriptor or ""):
                    logger.warning(
                        "Type override for %s.%s uses unknown descriptor %r; "
                        "ignoring", table_name, fname, type_descriptor)
                    type_descriptor = None

            if type_descriptor is None:
                # No override — use legacy upstream behavior: skip
                # fields the schema marks as stream=None or stream=0
                # (parser can't walk past them). With Path B, an
                # override fills most of these gaps for ItemInfo and
                # friends.
                if stream is None:
                    continue
                if stream == 0:
                    logger.debug("Schema field %s.%s has stream=0, skipping",
                                 table_name, fname)
                    continue

            # Determine struct format
            type_info = _TYPE_MAP.get(ftype)
            struct_fmt = type_info[0] if type_info else None

            fields.append(FieldSpec(
                name=fname,
                stream_size=stream if isinstance(stream, int) else 0,
                field_type=ftype,
                struct_fmt=struct_fmt,
                type_descriptor=type_descriptor,
            ))

        if fields:
            schemas[table_name.lower()] = TableSchema(
                table_name=table_name, fields=fields,
                no_null_skip=no_null_skip,
                no_entry_header=no_entry_header,
                verified_fields=verified_fields)

    _loaded_schemas = schemas
    logger.info("Loaded %d PABGB table schemas (%d with type overrides)",
                len(schemas), len(overrides))
    return schemas


def _load_type_overrides(schemas_dir: Path) -> dict[str, dict[str, dict]]:
    """Load ``pabgb_type_overrides.json`` — type descriptors that fill
    the base schema's ``stream=?`` gaps using crimson-rs-derived
    types. Returns
    ``{table_name: {field_name: {"type": descriptor}}}``. Missing file
    or parse errors degrade silently to no overrides.
    """
    candidates = [
        schemas_dir / "pabgb_type_overrides.json",
        Path(__file__).parent.parent.parent / "schemas" / "pabgb_type_overrides.json",
        Path(__file__).parent.parent.parent.parent / "schemas" / "pabgb_type_overrides.json",
    ]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys._MEIPASS) / "schemas" / "pabgb_type_overrides.json")

    for p in candidates:
        if not p.exists():
            continue
        try:
            # utf-8-sig transparently strips a leading BOM if present.
            # Notepad on Windows saves with BOM by default, so a user
            # editing the override file there would silently disable
            # all overrides without this. Iteration 9 systematic-
            # debugging finding 2026-04-27.
            with open(p, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except Exception as e:
            logger.warning("Failed to load type overrides at %s: %s", p, e)
            return {}
        # Strip the optional _meta block — table names never start with _.
        return {k: v for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, dict)}
    return {}


def init_schemas() -> int:
    """Eagerly load schemas. Returns count of loaded tables."""
    schemas = _load_schemas()
    return len(schemas)


def supported_tables() -> list[str]:
    """List all tables with known schemas."""
    return sorted(_load_schemas().keys())


def has_schema(table_name: str) -> bool:
    """Check if a table has a known schema."""
    return table_name.lower() in _load_schemas()


def get_schema(table_name: str) -> TableSchema | None:
    """Get the schema for a table."""
    return _load_schemas().get(table_name.lower())


# ── PABGH index parsing ────────────────────────────────────────────────────

def parse_pabgh_index(header_bytes: bytes, table_name: str = ""
                      ) -> tuple[int, dict[int, int]]:
    """Parse PABGH index file.

    Returns (key_size, {key: offset_in_body}).
    """
    name_lower = table_name.lower()
    count_size = 4 if name_lower in UINT_COUNT_TABLES else 2

    if len(header_bytes) < count_size:
        logger.warning("PABGH header too short: %d bytes", len(header_bytes))
        return 0, {}

    if count_size == 4:
        count = struct.unpack_from("<I", header_bytes, 0)[0]
    else:
        count = struct.unpack_from("<H", header_bytes, 0)[0]

    if count == 0:
        return 0, {}

    total_key_bytes = len(header_bytes) - count_size - count * 4
    if total_key_bytes <= 0 or total_key_bytes % count != 0:
        logger.warning("PABGH key size calculation failed for %s: "
                       "header=%d, count=%d", table_name, len(header_bytes), count)
        return 0, {}

    key_size = total_key_bytes // count

    offsets: dict[int, int] = {}
    pos = count_size
    for _ in range(count):
        if pos + key_size + 4 > len(header_bytes):
            break
        key = int.from_bytes(header_bytes[pos:pos + key_size], "little")
        offset = struct.unpack_from("<I", header_bytes, pos + key_size)[0]
        if key in offsets:
            logger.warning("Duplicate key %d in PABGH %s, overwriting", key, table_name)
        offsets[key] = offset
        pos += key_size + 4

    return key_size, offsets


# ── Record parsing ──────────────────────────────────────────────────────────

def _parse_entry_header(data: bytes, offset: int,
                        key_size: int = 4
                        ) -> tuple[int, str, int]:
    """Parse entry header at ``offset``.

    The entry_id width matches the PABGH index ``key_size``: u16
    for storeinfo / inventory style tables, u32 for dropsetinfo /
    iteminfo style tables. Reading u32 unconditionally misreads
    the name_len, which then trips the safety check and bails to
    payload_start = offset + 8, putting every subsequent field
    read at the wrong offset and yielding garbage values.

    Returns ``(entry_id, entry_name, payload_start_offset)``.
    """
    eid_fmt = "<H" if key_size == 2 else "<I"
    eid_size = 2 if key_size == 2 else 4
    head_size = eid_size + 4  # entry_id + u32 name_len

    if offset + head_size > len(data):
        return 0, "", offset

    eid = struct.unpack_from(eid_fmt, data, offset)[0]
    nlen = struct.unpack_from("<I", data, offset + eid_size)[0]

    if nlen > 500 or offset + head_size + nlen > len(data):
        return eid, "", offset + head_size

    name_start = offset + head_size
    name_end = name_start + nlen

    if nlen > 0:
        try:
            name = data[name_start:name_end].decode("utf-8")
        except UnicodeDecodeError:
            name = ""
    else:
        name = ""

    # Skip null terminator after name
    payload_start = name_end + 1 if name_end < len(data) and data[name_end] == 0 else name_end
    return eid, name, payload_start


def _read_field(data: bytes, offset: int, spec: FieldSpec) -> tuple[Any, int]:
    """Read a single field from binary data.

    Returns (value, bytes_consumed).
    """
    if spec.field_type == "CString":
        # u32 length prefix + UTF-8 bytes
        if offset + 4 > len(data):
            return None, 0
        slen = struct.unpack_from("<I", data, offset)[0]
        if slen == 0:
            return "", 4
        if offset + 4 + slen > len(data):
            return None, 4
        try:
            value = data[offset + 4:offset + 4 + slen].decode("utf-8")
        except UnicodeDecodeError:
            value = data[offset + 4:offset + 4 + slen].hex()
        return value, 4 + slen

    if spec.struct_fmt:
        size = spec.stream_size
        if offset + size > len(data):
            return None, 0
        value = struct.unpack_from(f"<{spec.struct_fmt}", data, offset)[0]
        return value, size

    # Raw bytes (12B, 16B, etc.)
    size = spec.stream_size
    if offset + size > len(data):
        return None, 0
    return data[offset:offset + size].hex(), size


def parse_record_payload(payload: bytes, schema: TableSchema
                         ) -> dict[str, Any]:
    """Parse a record payload into a field dict using the schema.

    Returns {field_name: value} for all fields that could be parsed.
    """
    result: dict[str, Any] = {}
    offset = 0

    for spec in schema.fields:
        if offset >= len(payload):
            break
        value, consumed = _read_field(payload, offset, spec)
        if consumed == 0:
            break
        result[spec.name] = value
        offset += consumed

    return result


def parse_records(table_name: str, body_bytes: bytes, header_bytes: bytes
                  ) -> dict[int, dict[str, Any]]:
    """Parse all records from a PABGB table into structured dicts.

    Args:
        table_name: table name (e.g., "inventory", "skill", "iteminfo")
        body_bytes: raw .pabgb file contents
        header_bytes: raw .pabgh file contents

    Returns:
        {record_key: {field_name: value}} for each record.
        Returns empty dict if schema not available.
    """
    schema = get_schema(table_name)
    if schema is None:
        return {}

    key_size, offsets = parse_pabgh_index(header_bytes, table_name)
    if not offsets:
        return {}

    # Sort offsets to determine entry boundaries
    sorted_entries = sorted(offsets.items(), key=lambda x: x[1])

    records: dict[int, dict[str, Any]] = {}

    for idx, (key, entry_offset) in enumerate(sorted_entries):
        # Determine entry end
        if idx + 1 < len(sorted_entries):
            entry_end = sorted_entries[idx + 1][1]
        else:
            entry_end = len(body_bytes)

        if entry_offset >= len(body_bytes):
            continue

        entry_data = body_bytes[entry_offset:entry_end]

        # Parse entry header — entry_id width matches PABGH key_size.
        eid, name, payload_start = _parse_entry_header(
            entry_data, 0, key_size)
        payload = entry_data[payload_start:]

        # Parse payload using schema
        fields = parse_record_payload(payload, schema)

        # Include metadata
        fields["_key"] = key
        fields["_name"] = name
        fields["_entry_id"] = eid

        records[key] = fields

    logger.debug("Parsed %d records from %s (%d fields per record)",
                 len(records), table_name,
                 len(schema.fields) if schema else 0)
    return records


# ── Display decode (read-only, for the Game Data grid) ───────────────────────
#
# parse_records() above is shared with the apply/diff pipeline (engine.py) and
# must not change. The functions below are a *display-only* decoder: they honor
# the override flags (no_entry_header / no_null_skip) and drive byte
# consumption through the format3 walker (pabgb_types.consume_bytes), so keyed
# tables with rich schema overrides — iteminfo, regioninfo, vehicleinfo,
# fieldinfo, stageinfo, characterinfo — render their real fields in the grid
# instead of stopping at the first variable-length field. When the walker can't
# safely step past a field, decoding stops there: earlier fields are kept and
# the rest are simply absent (never guessed).

_DISPLAY_PRIM_FMT = {
    "u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i",
    "u64": "Q", "i64": "q", "f32": "f", "f64": "d",
}


def _display_payload_start(entry: bytes, schema: "TableSchema",
                           key_size: int) -> int:
    """Byte offset of the first field, honoring the override flags."""
    if schema.no_entry_header:
        return 0
    eid_size = 2 if key_size == 2 else 4
    head = eid_size + 4
    if len(entry) < head:
        return 0
    nlen = struct.unpack_from("<I", entry, eid_size)[0]
    if nlen > 500 or head + nlen > len(entry):
        return head
    name_end = head + nlen
    if schema.no_null_skip:
        return name_end
    return (name_end + 1
            if name_end < len(entry) and entry[name_end] == 0 else name_end)


def _display_value(td: str, body: bytes, off: int, width: int) -> Any:
    """Human-readable value for a walked field. Strings decode inline;
    arrays show their element count; optionals show present/absent; anything
    without a scalar rendering falls back to compact hex."""
    td = (td or "").strip()
    fmt = _DISPLAY_PRIM_FMT.get(td)
    if fmt:
        try:
            return struct.unpack_from("<" + fmt, body, off)[0]
        except struct.error:
            return None
    if td == "CString":
        slen = struct.unpack_from("<I", body, off)[0]
        return body[off + 4:off + 4 + slen].decode("utf-8", "replace")
    if td == "LocalizableString":
        idx = struct.unpack_from("<Q", body, off + 1)[0]
        slen = struct.unpack_from("<I", body, off + 9)[0]
        if slen:
            return body[off + 13:off + 13 + slen].decode("utf-8", "replace")
        return f"loc#{idx}"          # text lives in the localization table
    if td.startswith("CArray<"):
        return f"[{struct.unpack_from('<I', body, off)[0]} items]"
    if td.startswith("COptional<"):
        return "—" if body[off] == 0 else "present"
    return body[off:off + width].hex()


def decode_record_display(entry_data: bytes, schema: "TableSchema",
                          key_size: int) -> dict[str, Any]:
    """Walk one entry into {field: display_value}, walker-driven."""
    from cdumm.semantic import pabgb_types as _pt
    off = _display_payload_start(entry_data, schema, key_size)
    end = len(entry_data)
    out: dict[str, Any] = {}
    for spec in schema.fields:
        if spec.type_descriptor:
            w = _pt.consume_bytes(spec.type_descriptor, entry_data, off, end)
            if w is None:
                break
            out[spec.name] = _display_value(
                spec.type_descriptor, entry_data, off, w)
            off += w
        elif spec.struct_fmt:
            w = spec.stream_size
            if off + w > end:
                break
            out[spec.name] = struct.unpack_from(
                "<" + spec.struct_fmt, entry_data, off)[0]
            off += w
        elif spec.field_type == "CString":
            if off + 4 > end:
                break
            slen = struct.unpack_from("<I", entry_data, off)[0]
            if off + 4 + slen > end:
                break
            out[spec.name] = entry_data[
                off + 4:off + 4 + slen].decode("utf-8", "replace")
            off += 4 + slen
        else:
            w = spec.stream_size or 0
            if w == 0 or off + w > end:
                break
            out[spec.name] = entry_data[off:off + w].hex()
            off += w
    return out


def parse_records_display(table_name: str, body_bytes: bytes,
                          header_bytes: bytes) -> dict[int, dict[str, Any]]:
    """Display-only variant of parse_records for the Game Data grid.

    Same {key: {field: value}} shape, but flag- and walker-aware so richly
    overridden tables decode fully. Never used by the apply/diff pipeline.
    """
    schema = get_schema(table_name)
    if schema is None:
        return {}
    key_size, offsets = parse_pabgh_index(header_bytes, table_name)
    if not offsets:
        return {}
    sorted_entries = sorted(offsets.items(), key=lambda x: x[1])
    records: dict[int, dict[str, Any]] = {}
    for idx, (key, entry_offset) in enumerate(sorted_entries):
        entry_end = (sorted_entries[idx + 1][1]
                     if idx + 1 < len(sorted_entries) else len(body_bytes))
        if entry_offset >= len(body_bytes):
            continue
        entry_data = body_bytes[entry_offset:entry_end]
        name = ""
        if not schema.no_entry_header:
            _eid, name, _ps = _parse_entry_header(entry_data, 0, key_size)
        fields = decode_record_display(entry_data, schema, key_size)
        fields["_key"] = key
        fields["_name"] = name
        records[key] = fields
    return records


def record_raw_bytes(table_name: str, body_bytes: bytes, header_bytes: bytes
                     ) -> dict[int, bytes]:
    """Return ``{record_key: raw entry bytes}`` for a PABGB table.

    Same record boundaries the parsers use (sorted PABGH offsets), but hands
    back the untouched bytes of each entry. Used by the gear-stat editor, which
    locates + edits stats structurally inside the opaque equipment records that
    the field decoder carries raw. Empty dict if the schema/index is missing.
    """
    if get_schema(table_name) is None:
        return {}
    _key_size, offsets = parse_pabgh_index(header_bytes, table_name)
    if not offsets:
        return {}
    sorted_entries = sorted(offsets.items(), key=lambda x: x[1])
    out: dict[int, bytes] = {}
    for idx, (key, entry_offset) in enumerate(sorted_entries):
        entry_end = (sorted_entries[idx + 1][1]
                     if idx + 1 < len(sorted_entries) else len(body_bytes))
        if entry_offset >= len(body_bytes):
            continue
        out[key] = body_bytes[entry_offset:entry_end]
    return out


def identify_table_from_path(entry_path: str) -> str | None:
    """Extract table name from a PAMT entry path.

    e.g., 'gamedata/inventory.pabgb' → 'inventory'
         'gamedata/binary__/client/bin/iteminfo.pabgb' → 'iteminfo'
    """
    if not entry_path:
        return None

    # Get filename without extension
    parts = entry_path.rsplit("/", 1)
    filename = parts[-1] if len(parts) > 1 else parts[0]

    if not filename.endswith(".pabgb"):
        return None

    table_name = filename[:-6]  # strip .pabgb
    return table_name if has_schema(table_name) else None
