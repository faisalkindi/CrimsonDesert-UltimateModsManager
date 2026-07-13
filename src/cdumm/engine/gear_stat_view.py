"""Every gear stat on an item, at its exact address.

Armour defence, weapon damage and enhancement values live in two places
inside a decoded ``iteminfo`` record:

  * ``sharpness_data.stat_list``            -- the item's base stats
  * ``enchant_data_list[t].enchant_stat_data.<list>`` -- one block per
    enhancement tier, four lists each

Both carry the same entry shape, ``{"stat": <id>, "change_mb": <value>}``,
and that is checked against the real CD 1.13 table in the tests: all
28,081 stat entries in vanilla have exactly those two keys, no exceptions.

This module walks a record the parser already decoded and hands back one
:class:`GearStat` per entry, each carrying the **exact Format 3 nested
path** to its value. Nothing is scanned, guessed or inferred.

Why that matters
----------------
The previous editor (PR #261/#269) located stats by scanning raw record
bytes for plausible-looking keys, because equipment records used to be
opaque blobs and there was no other way in. Since the 1.13 decode landed
there is. Measured against the real table, the scanner silently dropped
6,661 of 28,081 stats (24%), disagreed with the truth on 714 of 3,313
records, and invented stats for 33 records that have none. It also had to
dedupe by stat id, so an edit hit only the FIRST place a stat occurred --
and a helm carries the same stat id in a dozen places (base plus each
tier), so editing "defence" changed the base and left every upgrade tier
alone. The value drifted the moment the player enhanced the item.

Here, every occurrence is a separate addressable entry. Editing one tier
edits one tier; editing all of them is a decision the caller makes
explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The four stat lists inside an ``enchant_stat_data`` block. The label is
#: what a human sees next to the value; the game's own field name is what
#: goes in the path.
ENCHANT_STAT_LISTS: tuple[tuple[str, str], ...] = (
    ("stat_list_static", "flat"),
    ("stat_list_static_level", "per level"),
    ("max_stat_list", "max"),
    ("regen_stat_list", "regen"),
)

#: Every stat entry in vanilla CD 1.13 has exactly these two keys. An
#: entry that doesn't is a decode we don't understand, and we skip it
#: rather than write to an address we can't justify.
_ENTRY_KEYS = frozenset({"stat", "change_mb"})


@dataclass(frozen=True)
class GearStat:
    """One editable stat value, and where it lives."""

    path: str      #: Format 3 nested path, e.g. ``sharpness_data.stat_list[0].change_mb``
    stat: int      #: stat id -- resolve to a name with ``stat_names.stat_label``
    value: int     #: the current value
    group: str     #: ``"Base"`` or ``"Enhance +N"``
    kind: str      #: ``""`` for base, else ``"flat"`` / ``"per level"`` / ``"max"`` / ``"regen"``

    @property
    def where(self) -> str:
        """Human label for the group column: ``Base`` / ``Enhance +3 (flat)``."""
        return f"{self.group} ({self.kind})" if self.kind else self.group


def _entries(container: object, list_name: str) -> list:
    """The named stat list off a decoded block, or [] if it isn't there."""
    if not isinstance(container, dict):
        return []
    got = container.get(list_name)
    return got if isinstance(got, list) else []


def _usable(entry: object) -> bool:
    """A stat entry we're prepared to address."""
    return (isinstance(entry, dict)
            and _ENTRY_KEYS.issubset(entry)
            and isinstance(entry.get("stat"), int)
            and isinstance(entry.get("change_mb"), int))


def locate_gear_stats(record: dict) -> list[GearStat]:
    """Every gear stat on one decoded iteminfo record, in game order.

    Base stats first, then each enhancement tier. Returns [] for records
    that carry no stats (consumables, materials, quest items) -- which is
    most of the table, and is not an error.
    """
    out: list[GearStat] = []

    for i, entry in enumerate(_entries(record.get("sharpness_data"),
                                       "stat_list")):
        if not _usable(entry):
            continue
        out.append(GearStat(
            path=f"sharpness_data.stat_list[{i}].change_mb",
            stat=entry["stat"], value=entry["change_mb"],
            group="Base", kind=""))

    tiers = record.get("enchant_data_list")
    if not isinstance(tiers, list):
        return out

    for t, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            continue
        # `level` is the game's own tier number; fall back to position if
        # a record ever omits it, rather than mislabelling the row.
        level = tier.get("level")
        group = f"Enhance +{level if isinstance(level, int) else t}"
        block = tier.get("enchant_stat_data")
        for list_name, kind in ENCHANT_STAT_LISTS:
            for i, entry in enumerate(_entries(block, list_name)):
                if not _usable(entry):
                    continue
                out.append(GearStat(
                    path=(f"enchant_data_list[{t}].enchant_stat_data"
                          f".{list_name}[{i}].change_mb"),
                    stat=entry["stat"], value=entry["change_mb"],
                    group=group, kind=kind))

    return out


def locate_all_gear_stats(records: dict) -> dict:
    """``{record_key: [GearStat, ...]}`` for every record that has stats.

    `records` is ``{key: decoded_record}``. Records without stats are left
    out entirely, so ``key in result`` answers "is this equipment?".
    """
    found = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        stats = locate_gear_stats(record)
        if stats:
            found[key] = stats
    return found
