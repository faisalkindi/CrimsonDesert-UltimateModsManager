"""Stat id -> name, read from the game rather than remembered.

Gear stats in `iteminfo` are numeric ids (`{"stat": 1000007, "change_mb":
1000}`). The names live in the game's own `gamedata/statusinfo.pabgb`, so
that is where these come from: `load_stat_names()` reads the installed
game and returns the real table.

The snapshot below is a FALLBACK for when the game isn't reachable (CI, a
fresh clone, a user who hasn't pointed CDUMM at their install yet). It is
a snapshot of CD 1.13 and it will rot exactly the way every other
hardcoded table in this repo has rotted, so prefer the live read and
treat the snapshot as a convenience, not as truth.

Why not just use the community mapping: the widely-circulated
`buff_names_community.json` is wrong on at least seven of these ids --
including 1000006/1000007, which it has SWAPPED (it calls 1000007
"Critical Damage"; the game calls it CriticalRate). Every entry in it is
marked `verified: true`. A mod built against it boosts a different stat
than the author intended. Two independent checks say the table below is
the right one:

  * the game names its own items after the stat they grant, and all three
    testable cases agree exactly with statusinfo
    (`Item_Stat_AbyssGear_CriticalRate_LV1` carries 1000007 = CriticalRate,
    and likewise AttackSpeedRate=1000010, MoveSpeedRate=1000011)
  * 1000043 = GuardPVRate occurs 899 times in vanilla, overwhelmingly on
    shields

Both are pinned in tests/test_stat_names.py.
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

STATUSINFO_BODY = "gamedata/statusinfo.pabgb"
STATUSINFO_HEADER = "gamedata/statusinfo.pabgh"

#: Snapshot of gamedata/statusinfo.pabgb as shipped in CD 1.13.
#: Read from the game with load_stat_names() whenever you can.
STAT_NAMES_CD113: dict[int, str] = {
    1000000: "Hp",
    1000001: "Fatal",
    1000002: "DDD",
    1000003: "DPV",
    1000004: "DHIT",
    1000005: "DDV",
    1000006: "CriticalDamage",
    1000007: "CriticalRate",
    1000008: "AttackedDamageRate",
    1000009: "AttackedDamageReduction",
    1000010: "AttackSpeedRate",
    1000011: "MoveSpeedRate",
    1000012: "ClimbSpeedRate",
    1000013: "SwimSpeedRate",
    1000014: "Temperature",
    1000015: "Electricity",
    1000016: "FireResistance",
    1000017: "IceResistance",
    1000018: "ElectricityResistance",
    1000019: "EquipMainWeapon",
    1000020: "BareHandDDD",
    1000021: "EquipSubWeapon1",
    1000022: "Light",
    1000023: "FireB",
    1000024: "Confusion",
    1000025: "Morale",
    1000026: "Stamina",
    1000027: "Mp",
    1000028: "Fury",
    1000029: "Fresh",
    1000030: "Strength",
    1000031: "HitRate",
    1000032: "Agility",
    1000033: "Fishing",
    1000034: "RangeHitRate",
    1000035: "MaxDamageRate",
    1000036: "Pressure",
    1000037: "Stamina_UseResourceDecreaseRate",
    1000038: "Strengthening",
    1000039: "MoraleResistance",
    1000040: "Sink",
    1000041: "Puzzle",
    1000042: "Lapidification",
    1000043: "GuardPVRate",
    1000044: "EquipSubWeapon2",
    1000045: "EquipBow",
    1000046: "Mp_UseResourceDecreaseRate",
    1000047: "AddMoneyDropRate",
    1000048: "KnockOut",
    1000049: "EquipDropRate",
    1000050: "DPVRate",
    1000051: "DeadEyeMax",
    1000052: "Mining",
    1000053: "Banking",
    1000054: "Farming",
    1000055: "Ranching",
    1000056: "Logging",
    1000057: "Forging",
    1000058: "EquipLeftMainWeapon",
    1000059: "Refining",
    1000060: "Weaving",
    1000061: "Foodprocessing",
    1000062: "Crafting",
    1000063: "Mp_UseResourceIncreaseRate",
    1000064: "Stamina_UseResourceIncreaseRate",
    1000065: "Gathering",
    1000066: "Ornamenting",
    1000067: "Drawing",
    1000068: "Building",
    1000069: "Engineering",
    1000070: "BareFootDDD",
    1000071: "Difficulty",
    1000072: "Hunger",
    1000073: "Social",
    1000074: "KnockOutPVRate",
}


def parse_stat_names(body: bytes, header: bytes) -> dict[int, str]:
    """key -> name for every record in a statusinfo table.

    Only the record envelope is read (u32 key, u32 name length, name).
    The rest of the record is not decoded, because nothing here needs it
    -- and decoding fields we don't need is how schemas rot.
    """
    from cdumm.semantic.parser import parse_pabgh_index

    _, offsets = parse_pabgh_index(header, "statusinfo")
    out: dict[int, str] = {}
    for _key, off in sorted(offsets.items(), key=lambda kv: kv[1]):
        try:
            key = struct.unpack_from("<I", body, off)[0]
            n = struct.unpack_from("<I", body, off + 4)[0]
        except struct.error:
            continue
        if n == 0 or n > 200 or off + 8 + n > len(body):
            continue
        out[key] = body[off + 8:off + 8 + n].decode("utf-8", "replace")
    return out


def load_stat_names(con=None, game_dir: str | None = None) -> dict[int, str]:
    """The stat table for the installed game, or the 1.13 snapshot.

    Pass the game-index connection and game directory to read the real
    table. With no game available, returns STAT_NAMES_CD113 -- correct
    for 1.13, and stale the moment the game ships a new stat.
    """
    if con is None or not game_dir:
        return dict(STAT_NAMES_CD113)
    try:
        from cdumm.engine.game_index import extract_asset
        names = parse_stat_names(
            extract_asset(con, STATUSINFO_BODY, game_dir),
            extract_asset(con, STATUSINFO_HEADER, game_dir))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "statusinfo not readable (%s); falling back to the CD 1.13 "
            "stat-name snapshot, which may be stale for this game version",
            e)
        return dict(STAT_NAMES_CD113)
    if not names:
        return dict(STAT_NAMES_CD113)
    return names


def stat_label(stat_id: int, names: dict[int, str] | None = None) -> str:
    """'1000007 (CriticalRate)', or just the id when it isn't known.

    Unknown ids render as the bare number on purpose. An invented name is
    worse than a number: it reads as authoritative and sends the modder
    at the wrong stat.
    """
    table = STAT_NAMES_CD113 if names is None else names
    name = table.get(stat_id)
    return f"{stat_id} ({name})" if name else str(stat_id)
