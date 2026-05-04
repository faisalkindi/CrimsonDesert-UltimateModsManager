"""buffinfo.pabgb byte walker , Phase 1 (prefix decode only).

Status: WORK IN PROGRESS , does NOT yet decode `_buffDataList` items
or the `data.base.{...}` substructure that NattKh-dialect Format 3
mods (e.g. Adfaz Double Resource Buff, Nexus 2276) target. The
prefix decode landed first so subsequent passes have a verified
foundation to build on.

Why this exists
---------------
Adfaz's mod ships intents like::

    {"entry": "BuffLevel_Socket_ContributionExp",
     "key": 1000114,
     "field": "buff_data_list[0].data.base.absent_flag",
     "op": "set", "new": 0}

CDUMM's Format 3 apply path needs a parser that, given vanilla
``buffinfo.pabgb`` bytes, can resolve those dotted-indexed paths to
``(byte_offset, byte_width)`` tuples so ``_intents_to_v2_changes``
can emit the right byte patches.

What's verified (Phase 1)
-------------------------
Per-entry layout, first 8+slen bytes:

* ``[0..3]``: ``entry_key`` , u32 LE, the table key. Matches the
  intent's ``key`` field.
* ``[4..7]``: ``slen`` , u32 LE, length of the name string in bytes.
* ``[8..8+slen]``: ``name`` , UTF-8 string. Matches the intent's
  ``entry`` field.

Verified across 280 entries from Crimson Desert v1.05 vanilla.

What's NOT verified
-------------------
* The layout of bytes ``[8+slen..end]``. Initial cross-entry
  inspection showed a ``(tag_byte, u32)`` repeating pattern with
  per-entry counts varying in the second u32 , consistent with the
  engine schema's ``_buffDataList: direct_u32`` count followed by
  variable-length buff data items, but field-to-byte assignments
  are NOT yet pinned.
* The internal structure of each ``buff_data_list[i]`` item.
* The substructure under ``buff_data_list[i].data.base.{...}`` ,
  Adfaz's mod targets fields named ``absent_flag``, ``asset_path``,
  ``flags_a``, ``category``, plus raw byte-position accessors
  ``by58``, ``by69``, ``by132`` etc.

Next-session checklist
----------------------
1. Decode the fixed-width prefix between the name and the first
   variable-length field. Engine schema declaration order is
   ``_buffDataList, _isBlocked (direct_15B), _maxLevel, _minLevel,
   _buffLevelCalculateType (direct_15B), _sequencerFileName, ...``.
   In-entry order may differ , confirm by cross-comparing offsets
   across entries with known schema values.
2. Decode ``_buffDataList`` item structure. Use entries with
   small counts (1-2 items) as the unit-of-test. The
   ``BuffLevel_Comma_Symptom`` entry (count=3, total size 577) is a
   good first target.
3. Decode ``data.base.{...}`` substructure. Adfaz's mod's intents
   are the oracle: the apply outcome must produce bytes consistent
   with the ``new`` value at the path it specifies.
4. Wire ``locate_buff_field()`` returning ``(offset, width, dtype)``
   into CDUMM's ``_intents_to_v2_changes`` dispatch in
   ``format3_apply.py``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class BuffinfoEntryHeader:
    """Decoded prefix of a single buffinfo.pabgb entry.

    Verified field-by-field across all 280 entries of CD v1.05
    vanilla buffinfo.pabgb. ``body_start`` is the byte offset where
    the variable-length buff_data_list items begin.
    """
    entry_key: int
    name: str
    is_blocked: int  # 1 byte at body+0; always 0 in v1.05 vanilla
    is_blocked_offset: int  # byte offset within entry
    buff_data_count: int  # u32 at body+1..+4
    buff_data_count_offset: int  # byte offset within entry
    body_start: int  # offset where buff_data items begin (= prefix_end + 5)
    prefix_end: int  # byte offset just past the name string (= 8 + slen)


def parse_entry_prefix(entry_bytes: bytes) -> BuffinfoEntryHeader:
    """Decode the fixed prefix of a buffinfo entry.

    Verified layout (Phase 2):
      [0..3]            entry_key (u32)
      [4..7]            slen (u32)
      [8..7+slen]       name (utf-8)
      [prefix_end]      _isBlocked (1 byte; always 0 in v1.05 vanilla)
      [prefix_end+1..]  _buffDataList count (u32)
      [body_start..]    _buffDataList items (variable-length, NOT
                        decoded yet)

    Raises ``ValueError`` on truncation or implausible counts. The
    count sanity ceiling (10000) is set well above the observed
    maximum (200) but low enough to catch pointer-misread bugs.
    """
    if len(entry_bytes) < 8:
        raise ValueError(
            f"buffinfo entry too short for prefix: {len(entry_bytes)}B"
        )
    entry_key = struct.unpack_from("<I", entry_bytes, 0)[0]
    slen = struct.unpack_from("<I", entry_bytes, 4)[0]
    if slen > 1_000_000 or 8 + slen > len(entry_bytes):
        raise ValueError(
            f"buffinfo entry has implausible name length {slen} "
            f"(entry size {len(entry_bytes)}B)"
        )
    name = entry_bytes[8:8 + slen].decode("utf-8", errors="replace")
    prefix_end = 8 + slen

    if prefix_end + 5 > len(entry_bytes):
        raise ValueError(
            f"buffinfo entry truncated at body header: need 5 bytes "
            f"after name, got {len(entry_bytes) - prefix_end}"
        )
    is_blocked = entry_bytes[prefix_end]
    buff_data_count = struct.unpack_from(
        "<I", entry_bytes, prefix_end + 1)[0]
    if buff_data_count > 10_000:
        raise ValueError(
            f"buffinfo entry has implausible buff_data_list count "
            f"{buff_data_count} for entry {name!r}"
        )

    return BuffinfoEntryHeader(
        entry_key=entry_key,
        name=name,
        is_blocked=is_blocked,
        is_blocked_offset=prefix_end,
        buff_data_count=buff_data_count,
        buff_data_count_offset=prefix_end + 1,
        body_start=prefix_end + 5,
        prefix_end=prefix_end,
    )


def locate_buff_field(
    entry_bytes: bytes, field_path: str,
) -> tuple[int, int, str] | None:
    """Resolve a dotted-indexed field path to ``(byte_offset, width,
    dtype)`` within an entry, or ``None`` if not yet supported.

    Phase 1 stub: returns ``None`` for any field path , the body
    decoder is not yet implemented. Callers should treat ``None`` as
    "this field path can't be applied to bytes yet" and surface a
    clear skip message via the validator.

    Future phases will walk the variable-length body and resolve
    paths like ``buff_data_list[0].data.base.absent_flag`` to a
    concrete byte offset.
    """
    return None
