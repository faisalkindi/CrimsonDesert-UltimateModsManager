"""DropSet record parser + serializer for dropsetinfo.pabgb.

Ported from NattKh's CrimsonGameMods/dropset_editor.py
(github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS).

NattKh's tool exports Format 3 mods that target this table with a
`drops` field set to a list of dicts. This module decodes the vanilla
binary layout and re-encodes a modified record so we can apply those
mods.

Used by Format 3 to translate `{op:set, field:drops, new:[...]}`
intents into byte-level changes the existing apply pipeline can land.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field as dc_field
from typing import Optional


@dataclass
class ItemDrop:
    """One row of a DropSet's drops list. Layout per NattKh."""
    flag: int
    item_key: int
    unk3: int = 0
    unk4: int = 0
    unk1_flag: bytes = b"\x00" * 5
    unk_cond_flag: int = 0
    unk_post_cond: int = 0
    rates: int = 0
    rates_100: int = 0
    unk2: int = 0
    max_amt: int = 0
    min_amt: int = 0
    unk3_flags: int = 0xFFFF
    item_key_dup: int = 0
    extra_u8: Optional[int] = None
    extra_u32: Optional[int] = None
    friendly_data: Optional[bytes] = None


@dataclass
class DropSet:
    """One record in dropsetinfo.pabgb."""
    key: int
    name: str
    is_blocked: int = 0
    drop_roll_type: int = 0
    drop_roll_count: int = 0
    drop_condition_string: str = ""
    drop_tag_name_hash: int = 0
    drops: list[ItemDrop] = dc_field(default_factory=list)
    nee_slot_count: int = -1
    need_weight: int = 0
    total_drop_rate: int = 0
    original_string: str = ""


def _parse_drop_entry(buf: bytes, pos: int) -> tuple[ItemDrop, int]:
    """Parse a single drop entry starting at `pos`. Returns (drop, new_pos)."""
    flag = buf[pos]
    pos += 1
    item_key = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    unk3 = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    unk4 = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    unk1_flag = bytes(buf[pos:pos + 5])
    pos += 5
    unk_cond_flag = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    unk_post_cond = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    rates = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    rates_100 = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    unk2 = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    max_amt = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    min_amt = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    unk3_flags = struct.unpack_from("<H", buf, pos)[0]
    pos += 2
    item_key_dup = struct.unpack_from("<I", buf, pos)[0]
    pos += 4

    extra_u8 = None
    extra_u32 = None
    friendly_data = None
    if unk4 == 13:
        extra_u8 = buf[pos]
        pos += 1
    elif unk4 == 10:
        extra_u32 = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
    elif unk4 == 7:
        friendly_data = bytes(buf[pos:pos + 28])
        pos += 28

    return ItemDrop(
        flag=flag, item_key=item_key, unk3=unk3, unk4=unk4,
        unk1_flag=unk1_flag, unk_cond_flag=unk_cond_flag,
        unk_post_cond=unk_post_cond,
        rates=rates, rates_100=rates_100, unk2=unk2,
        max_amt=max_amt, min_amt=min_amt, unk3_flags=unk3_flags,
        item_key_dup=item_key_dup,
        extra_u8=extra_u8, extra_u32=extra_u32, friendly_data=friendly_data,
    ), pos


def _serialize_drop_entry(drop: ItemDrop) -> bytes:
    """Encode one ItemDrop to bytes. Layout matches `_parse_drop_entry`."""
    buf = bytearray()
    buf.append(drop.flag & 0xFF)
    buf += struct.pack("<I", drop.item_key)
    buf += struct.pack("<I", drop.unk3)
    buf += struct.pack("<I", drop.unk4)
    if len(drop.unk1_flag) != 5:
        raise ValueError(
            f"unk1_flag must be 5 bytes, got {len(drop.unk1_flag)}")
    buf += drop.unk1_flag
    buf += struct.pack("<I", drop.unk_cond_flag)
    buf += struct.pack("<I", drop.unk_post_cond)
    buf += struct.pack("<Q", drop.rates)
    buf += struct.pack("<Q", drop.rates_100)
    buf += struct.pack("<I", drop.unk2)
    buf += struct.pack("<Q", max(0, drop.max_amt))
    buf += struct.pack("<Q", max(0, drop.min_amt))
    buf += struct.pack("<H", drop.unk3_flags)
    buf += struct.pack("<I", drop.item_key_dup)
    # Tagged-trailer guards. Earlier the conditions silently passed
    # (no trailer emitted) when the tagged field was None, which let
    # FIX 16's `_drop_dict_to_item_drop` produce short records when
    # the JSON intent specified `unk4=7|10|13` without the matching
    # trailer field AND the template either didn't exist (empty
    # parsed.drops) or had a different unk4. Effect: serialized
    # record was 1 / 4 / 28 bytes short and every subsequent record
    # in the table shifted, corrupting the entire DropSet body.
    # Round 4 audit catch.
    if drop.unk4 == 13:
        if drop.extra_u8 is None:
            raise ValueError(
                f"unk4=13 requires extra_u8 (u8 trailing byte) but "
                f"none was provided for item_key={drop.item_key}"
            )
        buf.append(drop.extra_u8 & 0xFF)
    elif drop.unk4 == 10:
        if drop.extra_u32 is None:
            raise ValueError(
                f"unk4=10 requires extra_u32 (u32 trailing word) but "
                f"none was provided for item_key={drop.item_key}"
            )
        buf += struct.pack("<I", drop.extra_u32)
    elif drop.unk4 == 7:
        if drop.friendly_data is None:
            raise ValueError(
                f"unk4=7 requires friendly_data (28-byte trailer) but "
                f"none was provided for item_key={drop.item_key}"
            )
        if len(drop.friendly_data) != 28:
            raise ValueError(
                f"friendly_data must be 28 bytes, got "
                f"{len(drop.friendly_data)}")
        buf += drop.friendly_data
    return bytes(buf)


def parse_dropset_record(record: bytes) -> DropSet:
    """Parse a complete DropSet record (key + name + body + drops)."""
    pos = 0
    key = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    name_len = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    name = record[pos:pos + name_len].decode("ascii", errors="replace")
    pos += name_len
    is_blocked = record[pos]
    pos += 1
    drop_roll_type = record[pos]
    pos += 1
    drop_roll_count = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    dcs_len = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    drop_condition_string = ""
    if dcs_len > 0:
        drop_condition_string = record[pos:pos + dcs_len].decode(
            "ascii", errors="replace")
        pos += dcs_len
    drop_tag_name_hash = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    drop_count = struct.unpack_from("<I", record, pos)[0]
    pos += 4

    drops = []
    for _ in range(drop_count):
        drop, pos = _parse_drop_entry(record, pos)
        drops.append(drop)

    nee_slot_count = struct.unpack_from("<h", record, pos)[0]
    pos += 2
    need_weight = struct.unpack_from("<q", record, pos)[0]
    pos += 8
    total_drop_rate = struct.unpack_from("<q", record, pos)[0]
    pos += 8
    code_len = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    original_string = record[pos:pos + code_len].decode(
        "latin-1", errors="replace")
    pos += code_len

    if pos != len(record):
        raise ValueError(
            f"Record parser consumed {pos} of {len(record)} bytes "
            f"(key={key}, name={name!r})")

    return DropSet(
        key=key, name=name, is_blocked=is_blocked,
        drop_roll_type=drop_roll_type, drop_roll_count=drop_roll_count,
        drop_condition_string=drop_condition_string,
        drop_tag_name_hash=drop_tag_name_hash,
        drops=drops,
        nee_slot_count=nee_slot_count, need_weight=need_weight,
        total_drop_rate=total_drop_rate,
        original_string=original_string,
    )


def _drop_dict_to_item_drop(
    d: dict,
    template: Optional[ItemDrop] = None,
) -> ItemDrop:
    """Convert a JSON drop dict (NattKh export shape) into an ItemDrop.

    NattKh's exports include the user-meaningful fields (item_key,
    rates, rates_100, min_amt, max_amt). The unk* fields and tagged
    extras are NOT in the export, so we copy them from a template
    (typically the record's first existing drop) or use sensible
    defaults.

    NattKh's add_item helper at dropset_editor.py:270 uses the same
    template-fallback pattern.
    """
    # Tagged-extras (unk4 + its trailing block) MUST fall back to the
    # template, not default to 0/None. NattKh's exports only include
    # the user-meaningful fields (item_key, rates, min/max amount); the
    # tagged trailers (28-byte friendly_data for unk4=7, u32 for
    # unk4=10, u8 for unk4=13) are part of the binary record layout.
    # Defaulting them to 0/None on `op=set` strips the trailer, makes
    # the record the wrong byte length, and shifts every subsequent
    # record. DropSet_Friendly_Talk mods (Trust Me workalike) gave 0
    # friendship in-game because of this. kori228's #58 report
    # 2026-05-01.
    if "unk4" in d:
        unk4 = int(d["unk4"])
    elif template is not None:
        unk4 = template.unk4
    else:
        unk4 = 0
    if "extra_u8" in d:
        extra_u8 = d["extra_u8"]
    elif template is not None and template.unk4 == unk4:
        extra_u8 = template.extra_u8
    else:
        extra_u8 = None
    if "extra_u32" in d:
        extra_u32 = d["extra_u32"]
    elif template is not None and template.unk4 == unk4:
        extra_u32 = template.extra_u32
    else:
        extra_u32 = None
    if "friendly_data" in d:
        fd = d["friendly_data"]
        if isinstance(fd, str):
            fd = bytes.fromhex(fd)
        friendly_data = fd
    elif template is not None and template.unk4 == unk4:
        friendly_data = template.friendly_data
    else:
        friendly_data = None
    return ItemDrop(
        flag=d.get("flag", template.flag if template else 1),
        item_key=int(d["item_key"]),
        unk3=d.get("unk3", template.unk3 if template else 0),
        unk4=unk4,
        unk1_flag=(template.unk1_flag if template else b"\x00" * 5),
        unk_cond_flag=d.get(
            "unk_cond_flag",
            template.unk_cond_flag if template else 0xFFFFFFFF),
        unk_post_cond=d.get(
            "unk_post_cond",
            template.unk_post_cond if template else 0),
        rates=int(d.get("rates", 0)),
        rates_100=int(d.get("rates_100", 0)),
        unk2=int(d.get("unk2", 0)),
        max_amt=int(d.get("max_amt", 0)),
        min_amt=int(d.get("min_amt", 0)),
        unk3_flags=int(d.get("unk3_flags", 0xFFFF)),
        item_key_dup=int(d.get("item_key_dup", d["item_key"])),
        extra_u8=extra_u8,
        extra_u32=extra_u32,
        friendly_data=friendly_data,
    )


def build_drops_replacement_change(
    record_bytes: bytes,
    intent_key: int,
    intent_entry: str,
    new_drops_json: list[dict],
) -> Optional[dict]:
    """Translate a Format 3 `op=set, field=drops, new=[...]` intent
    into a v2-style change dict that replaces the record's body
    (everything after the entry header).

    The change uses `entry` + `rel_offset=0`. CDUMM's name-offset
    resolver maps an entry name to the byte position AFTER the
    `key + name_len + name` header (see
    `_build_name_offsets_generic`), so a rel_offset of 0 anchors
    at the body start, NOT at the record start. We slice off the
    header bytes from both `original` and `patched` to match.

    Returns a dict with `entry`, `rel_offset=0`, `original`, `patched`
    (all hex), or None on parse failure.
    """
    try:
        parsed = parse_dropset_record(record_bytes)
    except Exception:
        return None
    if parsed.key != intent_key:
        return None

    name_bytes = parsed.name.encode("latin-1", errors="replace")
    header_len = 4 + 4 + len(name_bytes)  # key u32 + name_len u32 + name

    template = parsed.drops[0] if parsed.drops else None
    parsed.drops = [
        _drop_dict_to_item_drop(d, template) for d in new_drops_json
    ]
    new_record = serialize_dropset_record(parsed)

    if record_bytes[:header_len] != new_record[:header_len]:
        # Header changed (e.g., name rewritten). The current writer
        # never touches the header, so this would be a serializer bug.
        return None

    return {
        "entry": parsed.name or intent_entry,
        "rel_offset": 0,
        "original": record_bytes[header_len:].hex(),
        "patched": new_record[header_len:].hex(),
        "label": f"{parsed.name or intent_entry}.drops",
    }


def serialize_dropset_record(ds: DropSet) -> bytes:
    """Encode a DropSet back to its on-disk bytes."""
    buf = bytearray()
    buf += struct.pack("<I", ds.key)
    name_bytes = ds.name.encode("latin-1", errors="replace")
    buf += struct.pack("<I", len(name_bytes))
    buf += name_bytes
    buf.append(ds.is_blocked & 0xFF)
    buf.append(ds.drop_roll_type & 0xFF)
    buf += struct.pack("<I", ds.drop_roll_count)
    dcs_bytes = ds.drop_condition_string.encode(
        "latin-1", errors="replace") if ds.drop_condition_string else b""
    buf += struct.pack("<I", len(dcs_bytes))
    buf += dcs_bytes
    buf += struct.pack("<I", ds.drop_tag_name_hash)
    buf += struct.pack("<I", len(ds.drops))
    for drop in ds.drops:
        buf += _serialize_drop_entry(drop)
    buf += struct.pack("<h", ds.nee_slot_count)
    buf += struct.pack("<q", ds.need_weight)
    buf += struct.pack("<q", ds.total_drop_rate)
    orig_bytes = ds.original_string.encode("latin-1", errors="replace")
    buf += struct.pack("<I", len(orig_bytes))
    buf += orig_bytes
    return bytes(buf)
