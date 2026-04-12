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
    stream_size: int         # bytes consumed from binary stream
    field_type: str          # from schema: "direct_u32", "CString", etc.
    struct_fmt: str | None   # struct format char, None for complex types


@dataclass
class TableSchema:
    """Schema for a PABGB table — field definitions in read order."""
    table_name: str
    fields: list[FieldSpec]

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
        with open(schema_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("Failed to load PABGB schema: %s", e)
        _loaded_schemas = {}
        return _loaded_schemas

    schemas: dict[str, TableSchema] = {}
    for table_name, fields_raw in raw.items():
        fields = []
        for fr in fields_raw:
            fname = fr.get("f", "")
            ftype = fr.get("type", "")
            stream = fr.get("stream", 0)

            if not fname or stream is None:
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
                stream_size=stream,
                field_type=ftype,
                struct_fmt=struct_fmt,
            ))

        if fields:
            schemas[table_name.lower()] = TableSchema(
                table_name=table_name, fields=fields)

    _loaded_schemas = schemas
    logger.info("Loaded %d PABGB table schemas", len(schemas))
    return schemas


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

def _parse_entry_header(data: bytes, offset: int
                        ) -> tuple[int, str, int]:
    """Parse entry header at offset.

    Returns (entry_id, entry_name, payload_start_offset).
    """
    if offset + 8 > len(data):
        return 0, "", offset

    eid = struct.unpack_from("<I", data, offset)[0]
    nlen = struct.unpack_from("<I", data, offset + 4)[0]

    if nlen > 500 or offset + 8 + nlen > len(data):
        return eid, "", offset + 8

    name_start = offset + 8
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

        # Parse entry header
        eid, name, payload_start = _parse_entry_header(entry_data, 0)
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
