"""statusgroupinfo.pabgb ``status_info_list`` writer.

Nexus mod 2634 "Critical Rate Enhancement" (norva2) ships a Global Critical
Rate patch that retargets one slot of the item-activation stat groups::

    {key: 1000006, field: "status_info_list[3]", op: "set", new: 1000006}

The value is a ``statusinfo`` record key -- these lists hold references to
other records, not numbers.

Record layout
-------------
Derived from the live table and checked by exact tiling: the grammar below
consumes every one of the 8 records to the byte, with nothing left over and
nothing short::

    <u32 key><u32 name_len><name>          the record envelope
    <u8   is_blocked>
    <u32 count><count * u32 statusinfo key>   list 0
    <u32 count><count * u32 statusinfo key>   list 1
    <u32 count><count * u32 statusinfo key>   list 2
    <u32 W><W * u32>                          slot table for list 1
    <u32 W><W * u32>                          slot table for list 0

The two W-entry tables are reverse indexes, and ``W`` is exactly the number
of ``statusinfo`` records -- so it is a property of the GAME BUILD, not a
constant: 75 pre-1.16, **78** on CD 1.16, which added three statusinfo
records. It used to be hardcoded at 75, which made every record fail to
parse on 1.16 and refused every intent with a layout error that read as
"CDUMM cannot read this table" when the table was fine (GitHub #355). It is
now derived per record -- the two tables must agree with each other, and
the record must tile exactly, which a wrong width cannot do.

Each table holds one set entry (everything else is 0xFFFFFFFF) per element
of the list it serves, and the entry value is that element's position in the
list. That correspondence holds on all 8 records for both tables, which is
what pairs table 0 with list 1 and table 1 with list 0. The slot ids are
stable across records: slot 9 resolves to CriticalRate in every record that
sets it, slot 10 to DHIT, and so on.

Which list is ``status_info_list``
----------------------------------
The shipped schema (``schemas/pabgb_complete_schema.json``, StatusGroupInfo)
names three list-typed fields -- ``_statusInfoList``,
``_regenerateStatusInfoList`` and ``_elementalStatusInfoList`` -- and the
wire has exactly three lists. Declaration order does not settle which is
which: it is demonstrably not wire order for this table (it puts
``_regenStatusIndexList`` before ``_key``, and swaps ``_isBlocked`` with
``_stringKey``).

The contents do settle it, the same way on both record families:

* List 2 is ``[Temperature, Confusion, Electricity]`` on every record that
  has one -- the three elemental statuses, so it is
  ``_elementalStatusInfoList``.
* List 0 holds Hp, Fatal, Hunger, Stamina, Mp, Morale, Fury -- regenerating
  resources. List 1 holds DDD, DPV, DHIT, CriticalRate, AttackSpeedRate,
  resistances -- derived combat stats.
* On ``StatOnActivateByItem``, whose entire purpose is the stats an item
  grants, list 0 and list 2 are EMPTY and list 1 carries the stats. A group
  named for granting stats cannot have an empty ``_statusInfoList``.

So list 1 is ``status_info_list``. That is an inference from content rather
than a byte-level proof.

How far the ambiguity guard actually reaches
--------------------------------------------
The writer refuses whenever another list in the same record is also long
enough to satisfy the index. **That is a narrower guarantee than "the
answer is forced regardless of the naming"**, which is what an earlier
revision of this docstring claimed (#320 review). The guard only fires
where two lists overlap; above ``len(list0)`` only list 1 fits, so the
index resolves without the guard ever being consulted and the naming
inference is load-bearing and unchecked -- roughly a hundred index
positions across the table.

It happens to be forced for the two records mod 2634 targets, because
their list 0 and list 2 are empty, so no other list could take the
index at all. For those two the answer really is naming-independent.
Elsewhere it is the content argument above doing the work. Two further
confirmations, both from the review rather than this PR's own evidence:
every decoded value resolves to a real stat name, and record 1000007
``StatOnActivateByItemWithoutAttackSpeedRate`` holds exactly record
1000006's list 1 minus ``AttackSpeedRate`` -- the record's own name
describes list 1's contents.

The reverse index travels with the write
----------------------------------------

Table 0 is a reverse index over list 1. Slot ``s`` holds the POSITION in
this record's list 1 at which slot ``s``'s key sits, or ``0xFFFFFFFF``
when the record does not carry it. On the committed 1.15 fixture the
slot -> key correspondence is one global bijection: all 8 records agree
on all 53 occupied slots, with no slot naming two keys and no key
claiming two slots.

Writing list 1 and leaving table 0 alone therefore produces a state
vanilla never ships -- a slot still claiming a key while pointing at a
position that now holds a different one (#320 review). Whether the game
reads the list, the index, or rebuilds it at load is not known, so the
writer does not gamble on which: it updates table 0 alongside list 1, and
refuses whenever it cannot.

``learn_slot_keys`` recovers the correspondence from the table itself
rather than assuming a rule, because there is no rule -- slot 3 is key
1000074 while slot 4 is key 1000002.

Two edits have no correct index and are refused outright:

* one that would list a key twice. A reverse index maps a key to one
  position, so a repeated key is unrepresentable. This is what mod 2634
  asks for -- CriticalDamage at positions 2 and 3, CriticalRate gone --
  and it is why that mod no longer applies. Refusing is the answer the
  format gives; writing the list and leaving a contradictory index is
  not a better one.
* one naming a key the index has no slot for, since the slot that must
  point at it is then unknown.
"""
from __future__ import annotations

import logging
import re
import struct
from contextlib import suppress

from cdumm.semantic.parser import parse_pabgh_index
from cdumm.semantic.record_header import NAME_FIRST

logger = logging.getLogger(__name__)

_ENVELOPE = 8
_N_LISTS = 3
_STATUS_INFO_LIST = 1        # which of the three lists (see module docstring)
_N_TABLES = 2

#: There is deliberately NO reverse-index width constant here.
#:
#: The tables carry one slot per ``statusinfo`` record, so the width is a
#: property of the GAME BUILD: 75 when this writer was written, 78 on CD
#: 1.16, which added three statusinfo records. It WAS hardcoded at 75, and
#: that made ``parse_record_full`` return None for all 8 records on the
#: live build -- refusing every intent with "does not match the known
#: statusgroupinfo layout", which reads as "CDUMM cannot read this table"
#: when the table was fine (GitHub #355).
#:
#: It is now derived per record: the two tables must agree with each other
#: and the record must tile exactly. That is self-validating, since a wrong
#: width cannot land on the record's last byte -- the same "derive and
#: validate" move that fixed the store list (#338) and the entry keys
#: (#341).
_MAX_COUNT = 4096            # sanity bound while walking

#: These lists hold ``statusinfo`` record keys, so a value outside that
#: key space is a dangling reference in a list the game dereferences --
#: a plausible crash vector, and never what a mod means.
#:
#: These are the FLOOR and a STARTING ceiling, not the answer.
#: 1000000..1000074 is the repo's CD 1.13 snapshot
#: (``stat_names.STAT_NAMES_CD113``, 75 keys). On CD 1.16 the table has 78
#: records spanning 1000000..**1000078**, and the vanilla lists reference
#: keys right to that top -- so treating this as the exact range refused
#: references the game itself makes. ``_status_key_space`` widens the
#: ceiling from evidence and never lowers the floor.
#:
#: Note the space is NOT contiguous: 78 records span 79 slots, so the
#: ceiling cannot be derived from the reverse-index width either.
_MIN_STATUS_KEY = 1000000
_MAX_STATUS_KEY = 1000074

#: Table-0 slot value meaning "this record does not carry that key".
#: Record 1000007 shows it plainly: 5 occupied slots, 70 sentinels.
_SENTINEL = 0xFFFFFFFF

_FIELD_RE = re.compile(r"^status_info_list\[(\d+)\]$")


def _status_key_space(
    observed: set[int] | None = None,
) -> tuple[int, int]:
    """(min, max) statusinfo key the range check will accept.

    Widened from every source of evidence, never narrowed. The check
    exists to catch a value that is obviously not a record reference --
    a 5, a 2**31 -- not to be an exact membership test, so a bound that
    is too TIGHT is the harmful direction: it refuses mods that are
    correct.

    That is exactly what went wrong. The shipped snapshot is CD 1.13's 75
    keys (1000000..1000074), and on CD 1.16 the statusinfo table has 78
    records spanning 1000000..**1000078** -- and the vanilla
    statusgroupinfo lists reference keys right up to that top. The frozen
    bound would refuse a reference the game itself makes.

    ``observed`` is the set of keys the vanilla lists actually hold, which
    the caller has already parsed. It is direct, on-disk evidence from the
    build in front of us and costs nothing to collect, so it is folded in.

    Note the key space is NOT contiguous -- 78 records span 79 slots, so
    one key in the range is absent. That is why the bound cannot be
    derived from the reverse-index width (``lo + width - 1`` would be one
    short); the span has to come from real keys.

    Only the CEILING moves. ``_MIN_STATUS_KEY`` is a structural fact of
    the key space, so a source containing something below it is telling
    us it is not a statusinfo key set -- widening the floor to match
    would let a bare ``5`` through, which is the garbage this check
    exists to stop. Keys below the floor are therefore ignored, not
    trusted.
    """
    lo, hi = _MIN_STATUS_KEY, _MAX_STATUS_KEY
    sources: list[set[int]] = []
    with suppress(Exception):   # optional module; the literal bound applies
        from cdumm.engine.stat_names import STAT_NAMES_CD113 as names
        if names:
            sources.append(set(names))
    if observed:
        sources.append(set(observed))
    for src in sources:
        usable = [k for k in src if k >= lo]
        if usable:
            hi = max(hi, max(usable))
    return lo, hi


def _read_list(body: bytes, p: int, end: int) -> tuple[int, int] | None:
    """Return (element_start, count) for the list at ``p``, or None."""
    if p + 4 > end:
        return None
    count = struct.unpack_from("<I", body, p)[0]
    if count > _MAX_COUNT or p + 4 + 4 * count > end:
        return None
    return p + 4, count


def parse_record_full(
    body: bytes, start: int, end: int, header_order: str | None = None
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    """``(lists, tables)`` as (element_start, count) each, or None.

    Returns None unless the full grammar consumes the record EXACTLY --
    three lists then two 75-entry tables, ending on the last byte. A record
    that does not tile is not one this writer understands, so it is left
    alone rather than written into.

    ``header_order`` is the table's key/name order from
    ``record_header.table_header_order``; on a name-first table (game
    2.04) the envelope is the same 8 bytes plus the name, key last.
    """
    if end - start < _ENVELOPE:
        return None
    if header_order == NAME_FIRST:
        name_len = struct.unpack_from("<I", body, start)[0]
    else:
        name_len = struct.unpack_from("<I", body, start + 4)[0]
    p = start + _ENVELOPE + name_len
    if p >= end:
        return None
    p += 1                                    # is_blocked
    lists: list[tuple[int, int]] = []
    for _ in range(_N_LISTS):
        got = _read_list(body, p, end)
        if got is None:
            return None
        elem_start, count = got
        lists.append((elem_start, count))
        p = elem_start + 4 * count
    tables: list[tuple[int, int]] = []
    table_len: int | None = None
    for _ in range(_N_TABLES):
        got = _read_list(body, p, end)
        if got is None:
            return None
        elem_start, count = got
        # The two reverse-index tables carry one slot per statusinfo
        # record, so they must be the SAME width as each other. Their
        # actual width is a build property (75 pre-1.16, 78 on 1.16), so
        # it is derived here rather than asserted -- see the comment on
        # the constant note above. Exact tiling below is what validates it: a
        # wrong width cannot finish on the record's last byte.
        if table_len is None:
            table_len = count
        elif count != table_len:
            return None
        tables.append((elem_start, count))
        p = elem_start + 4 * count
    if not table_len:
        return None                           # zero-width tables are not a record
    if p != end:
        return None                           # did not tile exactly
    return lists, tables


def parse_record(body: bytes, start: int, end: int) -> list[tuple[int, int]] | None:
    """The record's three lists as (element_start, count), or None."""
    got = parse_record_full(body, start, end)
    return None if got is None else got[0]


def _elements(body: bytes, es: int, count: int) -> list[int]:
    return list(struct.unpack_from(f"<{count}I", body, es)) if count else []


def learn_slot_keys(
    body: bytes, offsets: dict, starts: list[int]
) -> dict[int, int] | None:
    """``{table-0 slot: statusinfo key}``, learned from the vanilla table.

    Table 0 is a reverse index over list 1: slot ``s`` holds the POSITION
    in that record's list 1 at which its key sits, or ``_SENTINEL`` when
    the record does not carry that key. So ``list1[table0[s]]`` names the
    key slot ``s`` stands for, and reading that across every record
    recovers the whole slot->key correspondence without needing to know
    why a given key lives at a given slot.

    The correspondence is global: on the committed 1.15 fixture all 8
    records agree on all 53 occupied slots, no slot naming two keys and
    no key claiming two slots. Returns None if any record disagrees --
    that would mean the reverse index is not what this function assumes,
    and no write should be attempted on that assumption.
    """
    slot_key: dict[int, int] = {}
    for key in sorted(offsets):
        o = offsets[key]
        i = starts.index(o)
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        got = parse_record_full(body, o, end)
        if got is None:
            continue                          # not our grammar; skip it
        lists, tables = got
        list1 = _elements(body, *lists[_STATUS_INFO_LIST])
        table0 = _elements(body, *tables[0])
        for slot, pos in enumerate(table0):
            if pos == _SENTINEL:
                continue
            if pos >= len(list1):
                logger.warning(
                    "statusgroupinfo: record %d table-0 slot %d points at "
                    "list-1 position %d of %d; reverse index is not the "
                    "assumed shape", key, slot, pos, len(list1))
                return None
            seen = slot_key.get(slot)
            if seen is not None and seen != list1[pos]:
                logger.warning(
                    "statusgroupinfo: table-0 slot %d names key %d in one "
                    "record and %d in another; reverse index is not global",
                    slot, seen, list1[pos])
                return None
            slot_key[slot] = list1[pos]
    if len(set(slot_key.values())) != len(slot_key):
        logger.warning("statusgroupinfo: table-0 slot->key is not injective")
        return None
    return slot_key


def build_statusgroupinfo_changes(
    vanilla_body: bytes, vanilla_header: bytes, intents: list
) -> tuple[list[dict], list[tuple[object, str]]]:
    """Apply ``status_info_list[i]`` set intents.

    Returns ``(changes, dropped)``: ``changes`` are ``{offset, original,
    patched}`` byte-change dicts absolute in the .pabgb body, one per
    element write; ``dropped`` is ``(intent, reason)`` for anything refused.
    Writes are length-preserving, so no .pabgh companion is emitted.
    """
    dropped: list[tuple[object, str]] = []
    try:
        _, offsets = parse_pabgh_index(vanilla_header, "statusgroupinfo")
    except Exception as e:  # noqa: BLE001 -- never crash the whole apply
        logger.error("statusgroupinfo writer: header unreadable: %s", e)
        return [], [(i, f"statusgroupinfo header unreadable: {e}")
                    for i in intents]
    starts = sorted(offsets.values())
    body_len = len(vanilla_body)

    # Every statusinfo key the vanilla table already references. Direct
    # evidence from THIS build, so the range check below cannot be
    # tighter than the game's own data (see _status_key_space).
    observed: set[int] = set()
    for _o in offsets.values():
        _i = starts.index(_o)
        _end = starts[_i + 1] if _i + 1 < len(starts) else body_len
        _got = parse_record(vanilla_body, _o, _end)
        if _got is None:
            continue
        for _es, _c in _got:
            observed.update(_elements(vanilla_body, _es, _c))
    lo_key, hi_key = _status_key_space(observed)

    changes: list[dict] = []
    #: record key -> accepted writes, batched so the reverse index below
    #: sees the record's FINAL list rather than one edit at a time. Two
    #: intents that individually look fine can together duplicate a key.
    pending: dict[int, list[tuple]] = {}
    for intent in intents:
        field = getattr(intent, "field", "") or ""
        m = _FIELD_RE.match(field)
        if m is None:
            dropped.append((intent,
                            f"field {field!r} is not status_info_list[N]"))
            continue
        op = getattr(intent, "op", "set") or "set"
        if op != "set":
            dropped.append((intent, (
                f"op {op!r} not supported for status_info_list "
                f"(only 'set')")))
            continue
        new = getattr(intent, "new", None)
        if type(new) is not int or not 0 <= new < 2 ** 32:
            dropped.append((intent, (
                f"value {new!r} is not a statusinfo record key "
                f"(a 32-bit integer)")))
            continue
        if not lo_key <= new <= hi_key:
            dropped.append((intent, (
                f"value {new} is outside the statusinfo key space "
                f"({lo_key}-{hi_key}); writing it would leave a dangling "
                f"record reference in a list the game dereferences")))
            continue
        raw_key = getattr(intent, "key", None)
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            dropped.append((intent, (
                f"record key {raw_key!r} is not an integer")))
            continue
        o = offsets.get(key)
        if o is None:
            dropped.append((intent, (
                f"statusgroupinfo has no record with key {key}")))
            continue
        i = starts.index(o)
        end = starts[i + 1] if i + 1 < len(starts) else body_len
        lists = parse_record(vanilla_body, o, end)
        if lists is None:
            dropped.append((intent, (
                f"record key {key} does not match the known "
                f"statusgroupinfo layout")))
            continue

        idx = int(m.group(1))
        fits = [n for n, (_s, c) in enumerate(lists) if idx < c]
        if not fits:
            dropped.append((intent, (
                f"record key {key} has no list with a "
                f"status_info_list[{idx}] "
                f"(lengths {[c for _s, c in lists]})")))
            continue
        if fits != [_STATUS_INFO_LIST]:
            # More than one list could take this index, so which one the
            # mod means is not forced by the data. Refuse rather than pick.
            # Note this guard only reaches overlapping indexes -- above
            # len(list0) only list 1 fits and the naming inference in the
            # module docstring carries the write unchecked. See the
            # "How far the ambiguity guard actually reaches" section.
            dropped.append((intent, (
                f"status_info_list[{idx}] is ambiguous on record key "
                f"{key}: lists {fits} could all hold that index")))
            continue

        elem_start, _count = lists[_STATUS_INFO_LIST]
        at = elem_start + 4 * idx
        original = vanilla_body[at:at + 4]
        patched = struct.pack("<I", new)
        if original == patched:
            continue                          # already holds that key
        pending.setdefault(key, []).append((intent, idx, new, at, original,
                                            patched))

    # ── keep the reverse index consistent with the list ──────────────────
    #
    # Table 0 is a reverse index over list 1 (#320 review). Writing list 1
    # and leaving table 0 alone leaves a slot claiming a key that no longer
    # sits where it points -- the vanilla table has zero such conflicts, so
    # that is a state the game never ships. Rather than write it and hope
    # the game reads the list and not the index, the write now carries the
    # index with it, and refuses outright where no consistent index exists.
    slot_key = learn_slot_keys(vanilla_body, offsets, starts)
    key_slot = ({k: s for s, k in slot_key.items()}
                if slot_key is not None else None)

    for rec_key, writes in pending.items():
        o = offsets[rec_key]
        i = starts.index(o)
        end = starts[i + 1] if i + 1 < len(starts) else body_len
        got = parse_record_full(vanilla_body, o, end)
        if got is None:                       # already validated above
            continue
        lists, tables = got
        list1 = _elements(vanilla_body, *lists[_STATUS_INFO_LIST])

        after = list(list1)
        for _it, idx, new, *_ in writes:
            after[idx] = new

        # A reverse index maps one key to one position, so a repeated key
        # has no representation at all. The mod that motivated this PR asks
        # for exactly that -- it puts one key at two positions and drops
        # another -- so the honest answer is to refuse the edit, not to
        # invent an index for a shape the format cannot express.
        dupes = {v for v in after if after.count(v) > 1}
        if dupes:
            for it, idx, new, *_ in writes:
                dropped.append((it, (
                    f"writing status_info_list[{idx}] = {new} on record "
                    f"{rec_key} would list {sorted(dupes)} more than once; "
                    f"table 0 is a reverse index over this list and cannot "
                    f"point one key at two positions, so there is no "
                    f"consistent record to write")))
            continue

        if key_slot is None:
            for it, idx, new, *_ in writes:
                dropped.append((it, (
                    "statusgroupinfo reverse index is not the assumed "
                    "shape on this build, so the write cannot be carried "
                    "into it")))
            continue

        unknown = sorted({new for _it, _idx, new, *_ in writes
                          if new not in key_slot})
        if unknown:
            for it, idx, new, *_ in writes:
                dropped.append((it, (
                    f"statusinfo key(s) {unknown} never appear in this "
                    f"table's reverse index, so the slot that must point "
                    f"at them is unknown and the index cannot be updated")))
            continue

        # Rebuild the slots this record's list actually determines.
        want = dict.fromkeys(
            (s for s, k in slot_key.items() if k in set(list1) | set(after)),
            _SENTINEL)
        for pos, k in enumerate(after):
            want[key_slot[k]] = pos

        tbl_start, _tcnt = tables[0]
        for slot, value in sorted(want.items()):
            tat = tbl_start + 4 * slot
            torig = vanilla_body[tat:tat + 4]
            tpatched = struct.pack("<I", value)
            if torig == tpatched:
                continue
            changes.append({
                "offset": tat,
                "original": torig.hex(),
                "patched": tpatched.hex(),
            })

        for _it, _idx, _new, at, original, patched in writes:
            changes.append({
                "offset": at,
                "original": original.hex(),
                "patched": patched.hex(),
            })

    changes.sort(key=lambda c: c["offset"])
    return changes, dropped
