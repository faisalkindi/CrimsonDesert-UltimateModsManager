"""Field schema loader for Format 3 mods (JMM-compatible).

JMM's IteminfoBlobPatcher (CD JSON Mod Manager v9.9.3, decompiled)
uses a parallel "field schema" file separate from the PABGB record
schema. The field schema maps the friendly names mod authors use
(``drops``, ``attack``, ``resetHour``) to a write position inside
the entry blob — either a ``rel_offset`` or a ``tid`` to search
for.

Format::

    field_schema/iteminfo.json
    {
      "drops":   {"tid": "0xAABBCCDD", "value_offset": 5,
                  "type": "i32"},
      "attack":  {"rel_offset": 12, "type": "u32"},
      "_note":   "underscore-prefixed keys are ignored — comments"
    }

The schema is community-curated. JMM ships none. CDUMM does the
same and uses this loader to pick up whatever the user (or future
upstream sync) drops into ``field_schema/`` next to the exe.
"""
from __future__ import annotations

import json
import logging
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldSchemaEntry:
    """A single entry in a field_schema/<table>.json file."""
    data_type: str = "i32"
    value_offset: int = 5
    tid: int | None = None
    rel_offset: int | None = None


def _coerce_tid(raw) -> int | None:
    """Accept ``"0x12345678"`` (string) or ``305419896`` (int).

    JMM's reader does the same — both forms appear in
    community-authored schemas.
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            cleaned = raw.replace("0x", "").replace("0X", "").replace(" ", "")
            return int(cleaned, 16)
        except ValueError:
            return None
    return None


def field_schema_path(table_name: str,
                      search_root: Path | None = None) -> Path | None:
    """Return the absolute path to ``field_schema/<table>.json``,
    or None if no candidate location exists.

    Search order:
      1. ``<search_root>/field_schema/<table>.json`` (explicit
         test argument)
      2. ``$CDUMM_FIELD_SCHEMA_ROOT/field_schema/<table>.json``
         (env override — useful for tests + power users who want
         to point CDUMM at a hand-edited schema dir)
      3. PyInstaller _MEIPASS/field_schema/<table>.json (frozen exe)
      4. Repo root ``field_schema/<table>.json`` (dev / installed)
    """
    import os
    candidates: list[Path] = []
    if search_root is not None:
        candidates.append(search_root / "field_schema"
                          / f"{table_name}.json")
    env_root = os.environ.get("CDUMM_FIELD_SCHEMA_ROOT")
    if env_root:
        candidates.append(Path(env_root) / "field_schema"
                          / f"{table_name}.json")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "field_schema"
                          / f"{table_name}.json")
    repo_root = Path(__file__).parent.parent.parent.parent
    candidates.append(repo_root / "field_schema"
                      / f"{table_name}.json")

    for p in candidates:
        if p.exists():
            return p
    return candidates[0] if candidates else None


def load_field_schema(table_name: str,
                      search_root: Path | None = None
                      ) -> dict[str, FieldSchemaEntry]:
    """Load ``field_schema/<table>.json`` if present.

    Returns a ``{field_name: FieldSchemaEntry}`` mapping. Missing
    or malformed files yield an empty dict (logged at debug) — the
    caller's apply path falls back gracefully.
    """
    path = field_schema_path(table_name, search_root=search_root)
    if path is None or not path.exists():
        logger.debug("No field_schema for table '%s'", table_name)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        logger.warning(
            "Failed to load field_schema for '%s' from %s: %s",
            table_name, path, e)
        return {}

    if not isinstance(raw, dict):
        logger.warning(
            "field_schema for '%s' is not a JSON object", table_name)
        return {}

    result: dict[str, FieldSchemaEntry] = {}
    for key, val in raw.items():
        # Skip underscore-prefixed keys (JMM convention: comments /
        # author annotations inside the schema file itself).
        if key.startswith("_"):
            continue
        if not isinstance(val, dict):
            continue

        tid = _coerce_tid(val.get("tid"))
        rel_offset = val.get("rel_offset")
        if rel_offset is not None and not isinstance(rel_offset, int):
            rel_offset = None
        # Negative rel_offset would write into the previous entry's
        # bytes (only the upper bound is checked at apply time).
        # Reject at load to surface the typo before any write.
        if rel_offset is not None and rel_offset < 0:
            logger.warning(
                "field_schema entry '%s' in '%s' has negative "
                "rel_offset=%d; skipping (would write before entry "
                "start)", key, table_name, rel_offset)
            continue

        # An entry with neither a TID nor a rel_offset has no way to
        # locate the field — drop it. Surfacing a parse-time error
        # here is friendlier than silently failing at apply time.
        if tid is None and rel_offset is None:
            logger.debug(
                "field_schema entry '%s' in '%s' has neither tid "
                "nor rel_offset, skipping", key, table_name)
            continue

        data_type = val.get("type") or "i32"
        if not isinstance(data_type, str):
            data_type = "i32"
        value_offset = val.get("value_offset", 5)
        if not isinstance(value_offset, int):
            value_offset = 5
        # Negative value_offset overlaps the TID itself or earlier
        # bytes — corrupts the type tag. Reject so the author sees
        # the bad entry, not a silent corruption.
        if value_offset < 0:
            logger.warning(
                "field_schema entry '%s' in '%s' has negative "
                "value_offset=%d; skipping",
                key, table_name, value_offset)
            continue

        result[key] = FieldSchemaEntry(
            tid=tid,
            rel_offset=rel_offset,
            value_offset=value_offset,
            data_type=data_type,
        )

    if result:
        logger.info(
            "Loaded field_schema for '%s': %d field(s) from %s",
            table_name, len(result), path)
    return result


def locate_field(body: bytes, blob_start: int, blob_end: int,
                 entry: FieldSchemaEntry) -> int | None:
    """Return the absolute byte offset where the field's value
    should be written, or None if it can't be located.

    Two modes (matching JMM's IteminfoBlobPatcher):
      * ``rel_offset``: ``blob_start + rel_offset``
      * ``tid``: search ``[blob_start, blob_end)`` for the 4-byte
        TID marker, return ``tid_pos + value_offset``

    ``blob_end`` is the exclusive upper bound — the search must
    not match a TID belonging to the next entry.
    """
    if entry.rel_offset is not None:
        return blob_start + entry.rel_offset

    if entry.tid is not None:
        tid_bytes = struct.pack("<I", entry.tid & 0xFFFFFFFF)
        # bytes.find with start/end keeps us inside the entry
        pos = body.find(tid_bytes, blob_start, blob_end)
        if pos < 0:
            return None
        return pos + entry.value_offset

    return None
