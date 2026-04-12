"""Semantic engine — orchestrates parsing, diffing, merging, and serialization.

Main entry point for the semantic system. Coordinates:
  1. Parse vanilla + mod binaries into structured records
  2. Diff and merge with conflict detection
  3. Serialize resolved records back to binary
  4. Manage resolution persistence in the database
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Any

from cdumm.semantic.changeset import SemanticMergeResult, TableChangeset
from cdumm.semantic.merger import (
    build_resolved_record,
    extract_flat_conflicts,
    merge_table,
)
from cdumm.semantic.parser import (
    get_schema,
    has_schema,
    identify_table_from_path,
    parse_pabgh_index,
    parse_records,
    supported_tables,
)

logger = logging.getLogger(__name__)


class SemanticEngine:
    """Orchestrates semantic diffing and merging for game data tables."""

    def __init__(self, db=None) -> None:
        """Initialize with optional database for resolution persistence.

        Args:
            db: Database instance (cdumm.storage.database.Database).
                If None, resolutions are not persisted.
        """
        self._db = db

    def is_supported(self, entry_path: str) -> bool:
        """Check if an entry path corresponds to a semantically parseable table."""
        return identify_table_from_path(entry_path) is not None

    def get_supported_tables(self) -> list[str]:
        """List all tables with known schemas."""
        return supported_tables()

    def analyze_bytes(
        self,
        table_name: str,
        vanilla_body: bytes,
        vanilla_header: bytes,
        mod_bodies: dict[str, bytes],
        mod_headers: dict[str, bytes] | None = None,
    ) -> SemanticMergeResult | None:
        """Main entry point: analyze vanilla vs mod(s) at the semantic level.

        Args:
            table_name: PABGB table name (e.g., "inventory", "skill")
            vanilla_body: raw .pabgb bytes (vanilla)
            vanilla_header: raw .pabgh bytes (vanilla)
            mod_bodies: {mod_name: raw .pabgb bytes} per mod
            mod_headers: {mod_name: raw .pabgh bytes} per mod (optional,
                         uses vanilla header for all mods if not provided)

        Returns:
            SemanticMergeResult with changeset and conflicts, or None on failure.
        """
        if not has_schema(table_name):
            logger.debug("No schema for table %s, skipping semantic analysis",
                         table_name)
            return None

        # Parse vanilla records
        vanilla_records = parse_records(table_name, vanilla_body, vanilla_header)
        if not vanilla_records:
            logger.debug("No vanilla records parsed for %s", table_name)
            return None

        # Parse each mod's records
        mod_records: dict[str, dict[int, dict[str, Any]]] = {}
        for mod_name, mod_body in mod_bodies.items():
            header = (mod_headers or {}).get(mod_name, vanilla_header)
            records = parse_records(table_name, mod_body, header)
            if records:
                mod_records[mod_name] = records

        if not mod_records:
            logger.debug("No mod records parsed for %s", table_name)
            return None

        # Load stored resolutions
        resolutions = self._load_resolutions(table_name)

        # Merge
        changeset = merge_table(table_name, vanilla_records, mod_records,
                                resolutions)

        # Extract flat conflicts for UI
        conflicts = extract_flat_conflicts(changeset)

        result = SemanticMergeResult(
            table_changeset=changeset,
            conflicts=conflicts,
        )

        logger.info("Semantic analysis for %s: %s", table_name, result.summary)
        return result

    def build_merged_body(
        self,
        table_name: str,
        vanilla_body: bytes,
        vanilla_header: bytes,
        changeset: TableChangeset,
    ) -> bytes | None:
        """Serialize resolved records back to binary PABGB format.

        Rebuilds the body by applying resolved field values to vanilla
        records. Only records with changes are modified; unchanged records
        are copied verbatim from vanilla.

        Returns modified .pabgb bytes, or None on failure.
        """
        schema = get_schema(table_name)
        if schema is None:
            return None

        # Parse vanilla for the base records and index
        vanilla_records = parse_records(table_name, vanilla_body, vanilla_header)
        if not vanilla_records:
            return None

        key_size, offsets = parse_pabgh_index(vanilla_header, table_name)
        if not offsets:
            return None

        # Build a map of record_key → resolved record
        resolved_records: dict[int, dict[str, Any]] = {}
        for rc in changeset.records:
            if rc.record_key in vanilla_records:
                resolved_records[rc.record_key] = build_resolved_record(
                    vanilla_records[rc.record_key], rc)

        if not resolved_records:
            return bytes(vanilla_body)  # nothing to change, return vanilla copy

        # Rebuild body: copy vanilla and patch resolved records
        body = bytearray(vanilla_body)
        sorted_entries = sorted(offsets.items(), key=lambda x: x[1])

        for idx, (key, entry_offset) in enumerate(sorted_entries):
            if key not in resolved_records:
                continue

            # Determine entry boundaries
            if idx + 1 < len(sorted_entries):
                entry_end = sorted_entries[idx + 1][1]
            else:
                entry_end = len(vanilla_body)

            if entry_offset >= len(vanilla_body):
                continue

            # Re-serialize the resolved record's payload
            resolved = resolved_records[key]
            new_payload = _serialize_record_payload(resolved, schema)
            if new_payload is None:
                continue

            # Find payload start in entry (skip header: id + name_len + name + null)
            entry_data = vanilla_body[entry_offset:entry_end]
            _, _, payload_start = _parse_entry_header_offset(entry_data)

            abs_payload_start = entry_offset + payload_start
            old_payload_len = entry_end - abs_payload_start

            if len(new_payload) != old_payload_len:
                # Size changed — can't do in-place safely. Abort entire
                # rebuild to prevent offset corruption of subsequent records.
                logger.warning(
                    "Record %d payload size changed (%d → %d), aborting "
                    "merged body rebuild. Full rewrite with index rebuild needed.",
                    key, old_payload_len, len(new_payload))
                return bytes(vanilla_body)  # return unmodified vanilla

            # Same size — safe in-place replacement
            body[abs_payload_start:abs_payload_start + old_payload_len] = new_payload

        return bytes(body)

    def apply_resolution(
        self,
        table_name: str,
        record_key: int,
        field_name: str,
        winning_mod: str,
    ) -> None:
        """Store a user's conflict resolution decision in the database."""
        if self._db is None:
            return
        try:
            self._db.connection.execute(
                "INSERT OR REPLACE INTO semantic_resolutions "
                "(table_name, record_key, field_name, winning_mod) "
                "VALUES (?, ?, ?, ?)",
                (table_name, record_key, field_name, winning_mod),
            )
            self._db.connection.commit()
            logger.info("Stored resolution: %s record %d field %s → %s",
                        table_name, record_key, field_name, winning_mod)
        except Exception as e:
            logger.warning("Failed to store resolution: %s", e, exc_info=True)

    def _load_resolutions(self, table_name: str) -> dict[str, str]:
        """Load stored resolutions from database.

        Returns {"{record_key}:{field_name}": winning_mod_name}.
        """
        if self._db is None:
            return {}
        try:
            cursor = self._db.connection.execute(
                "SELECT record_key, field_name, winning_mod "
                "FROM semantic_resolutions WHERE table_name = ?",
                (table_name,),
            )
            return {
                f"{row[0]}:{row[1]}": row[2]
                for row in cursor.fetchall()
            }
        except Exception:
            return {}


def _parse_entry_header_offset(entry_data: bytes) -> tuple[int, str, int]:
    """Parse entry header. Returns (entry_id, name, payload_start_offset)."""
    if len(entry_data) < 8:
        return 0, "", 0

    eid = struct.unpack_from("<I", entry_data, 0)[0]
    nlen = struct.unpack_from("<I", entry_data, 4)[0]

    if nlen > 500 or 8 + nlen > len(entry_data):
        return eid, "", 8

    name = ""
    if nlen > 0:
        try:
            name = entry_data[8:8 + nlen].decode("utf-8")
        except UnicodeDecodeError:
            pass

    name_end = 8 + nlen
    payload_start = name_end + 1 if name_end < len(entry_data) and entry_data[name_end] == 0 else name_end
    return eid, name, payload_start


def _serialize_record_payload(record: dict[str, Any],
                              schema) -> bytes | None:
    """Serialize a record dict back to binary payload using the schema.

    Returns bytes, or None if serialization fails.
    """
    from cdumm.semantic.parser import FieldSpec, _TYPE_MAP

    buf = bytearray()

    for spec in schema.fields:
        value = record.get(spec.name)
        if value is None:
            if spec.field_type == "CString":
                buf.extend(struct.pack("<I", 0))  # zero-length CString
            else:
                buf.extend(b"\x00" * spec.stream_size)
            continue

        if spec.field_type == "CString":
            if isinstance(value, str):
                encoded = value.encode("utf-8")
                buf.extend(struct.pack("<I", len(encoded)))
                buf.extend(encoded)
            else:
                buf.extend(struct.pack("<I", 0))
            continue

        if spec.struct_fmt:
            try:
                buf.extend(struct.pack(f"<{spec.struct_fmt}", value))
            except (struct.error, TypeError):
                buf.extend(b"\x00" * spec.stream_size)
            continue

        # Raw bytes (12B, 16B, etc.)
        if isinstance(value, str):
            # hex string → bytes
            try:
                raw = bytes.fromhex(value)
                buf.extend(raw[:spec.stream_size])
                if len(raw) < spec.stream_size:
                    buf.extend(b"\x00" * (spec.stream_size - len(raw)))
            except ValueError:
                buf.extend(b"\x00" * spec.stream_size)
        elif isinstance(value, bytes):
            buf.extend(value[:spec.stream_size])
            if len(value) < spec.stream_size:
                buf.extend(b"\x00" * (spec.stream_size - len(value)))
        else:
            buf.extend(b"\x00" * spec.stream_size)

    return bytes(buf)
