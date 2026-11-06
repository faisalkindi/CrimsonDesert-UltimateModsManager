"""knowledgeinfo.pabgb ``is_default`` writer (recipe/elemental unlocks).

The "Unlock All Equipment and AbyssGear Recipes" (Nexus 2726) and
"Unlock All Elementals" (Nexus 2664) mods do exactly one thing: set
``is_default`` to 1 on a list of knowledge records, so the recipe is
known from the start. Together that is 166 intents, and every one of
them applied zero bytes.

The blocker was not a missing decoder but the schema's wire type.
``KnowledgeInfo._isDefault`` is declared ``direct_15B`` -- a Pearl
Abyss tagged primitive -- and ``validate_intents`` refuses those,
because the schema says how many bytes the field occupies but not
where inside them the value sits. On disk the value is a single byte,
and this writer locates it.

Record layout, verified on all 6219 records of the live 1.15 table::

    +0            u32   key            (equals the PABGH index key)
    +4            u32   name length
    +8            .     name bytes     (``Knowledge_...``)
    name_end+0    u8    always 0
    name_end+1    u32   varies per record (a reader/hash reference)
    name_end+5    u8    is_default     <- the byte this writer sets
    name_end+6    u8    always 13

(Only the *byte* at +6 is invariant; the u32 starting there is not --
the three bytes after it vary. Checking the wider read would refuse
records it shouldn't.)

Why ``name_end+5`` is ``is_default`` and not one of the record's other
booleans:

* Sweeping every offset in ``name_end+0 .. +79``, exactly two are
  boolean-valued (in ``{0, 1}``) across all 6219 records *and* zero on
  all 166 keys the three mods target -- ``+5`` and ``+17``. Every other
  offset is either constant or takes many values.
* ``+17`` is 1 on 51 records and all 51 are ``Knowledge_Skill_Farming/
  Ranching/Logging/Mining_I..III`` -- a life-skill flag, not a
  start-known flag.
* ``+5`` is 1 on 562 records, and they are the ones a character must
  have from the start: ``Knowledge_Hp``, ``Knowledge_CriticalRate``,
  ``Knowledge_AttackSpeedRate``, ``Knowledge_MoveSpeedRate``,
  ``Knowledge_Fatal``, ``Knowledge_KnockOut``, plus the UI gimmick
  icons and node knowledges.
* The alternative assignment contradicts itself: the only other
  direct_15B boolean this could be is ``_isBlocked``, and 562 blocked
  records including every base stat knowledge would mean a character
  with no stats at all.

Not verified in-game by CDUMM. The mod authors verified these files
against DMM 1.4.8 on game 1.14.00, which is what makes "vanilla is 0 on
all 166 targets" meaningful evidence rather than a coincidence.

Writes are length-preserving (one byte, same width), so the companion
``.pabgh`` needs no rebuild.
"""
from __future__ import annotations

import logging
import struct

from cdumm.semantic.record_header import is_name_first, name_first_refusal

logger = logging.getLogger(__name__)

#: Byte offset of ``is_default`` measured from the end of the record's
#: name, which is where the layout stops depending on the name length.
IS_DEFAULT_OFFSET = 5

#: Field spellings that route here. ``is_default`` is what current DMM
#: emits; the camelCase form is accepted so a hand-written or older mod
#: isn't silently dropped.
_FIELDS = {"is_default", "isDefault", "_isDefault"}


def _record_bounds(header: bytes, body: bytes) -> dict[int, tuple[int, int]]:
    from cdumm.semantic.parser import parse_pabgh_index
    _keys, offs = parse_pabgh_index(header, "knowledgeinfo")
    ordered = sorted(offs.items(), key=lambda kv: kv[1])
    return {
        k: (o, ordered[i + 1][1] if i + 1 < len(ordered) else len(body))
        for i, (k, o) in enumerate(ordered)
    }


def locate_is_default(body: bytes, lo: int, hi: int,
                      key: int) -> int | None:
    """Absolute offset of the record's ``is_default`` byte, or None.

    Refuses rather than guesses when the record head doesn't match the
    expected shape -- a desynced read here would flip an unrelated byte.
    """
    if hi - lo < 12:
        return None
    if struct.unpack_from("<I", body, lo)[0] != key:
        return None
    name_len = struct.unpack_from("<I", body, lo + 4)[0]
    if not 0 < name_len < 250:
        return None
    name_end = lo + 8 + name_len
    pos = name_end + IS_DEFAULT_OFFSET
    if pos >= hi:
        return None
    # The two structural constants that frame the field. If either is
    # missing, this record isn't the shape we derived the offset from.
    if body[name_end] != 0:
        return None
    if name_end + 6 >= hi or body[name_end + 6] != 13:
        return None
    if body[pos] not in (0, 1):
        return None
    return pos


def build_knowledgeinfo_changes(
    vanilla_body: bytes, vanilla_header: bytes, intents: list
) -> tuple[list[dict], list[tuple[object, str]]]:
    """Apply ``is_default`` set intents.

    Returns ``(changes, dropped)``; each change is a one-byte
    ``{offset, original, patched}``. No .pabgh companion is emitted --
    nothing moves.
    """
    # Game 2.04's name-first records are refused outright: this writer only
    # knows the key-first layout (#493).
    if is_name_first(vanilla_body, vanilla_header, "knowledgeinfo"):
        why = name_first_refusal("knowledgeinfo.pabgb")
        return [], [(it, why) for it in intents]
    dropped: list[tuple[object, str]] = []
    bounds = _record_bounds(vanilla_header, vanilla_body)
    writes: dict[int, int] = {}

    for it in intents:
        field = getattr(it, "field", "") or ""
        if field not in _FIELDS:
            dropped.append((it, (f"field {field!r} is not knowledgeinfo's "
                                 f"is_default")))
            continue
        if (getattr(it, "op", "set") or "set") != "set":
            dropped.append((it, (f"op {getattr(it, 'op', None)!r} not "
                                 f"supported for knowledgeinfo (only 'set')")))
            continue
        val = getattr(it, "new", None)
        if isinstance(val, bool):
            val = int(val)
        if val not in (0, 1):
            dropped.append((it, (f"value {val!r} is not a boolean 0/1; "
                                 f"is_default is a single flag byte")))
            continue
        key = getattr(it, "key", None)
        if not isinstance(key, int) or key not in bounds:
            dropped.append((it, f"no knowledgeinfo record with key {key!r}"))
            continue
        lo, hi = bounds[key]
        pos = locate_is_default(vanilla_body, lo, hi, key)
        if pos is None:
            dropped.append((it, (f"knowledgeinfo record key={key} does not "
                                 f"match the expected record layout; "
                                 f"refusing to write")))
            continue
        entry = getattr(it, "entry", "") or ""
        if entry:
            name_len = struct.unpack_from("<I", vanilla_body, lo + 4)[0]
            name = vanilla_body[lo + 8:lo + 8 + name_len].decode(
                "utf-8", "replace")
            if name != entry:
                dropped.append((it, (f"key {key} is {name!r}, but the intent "
                                     f"names {entry!r}")))
                continue
        writes[pos] = val

    changes = []
    for pos, val in sorted(writes.items()):
        original = vanilla_body[pos:pos + 1]
        if original[0] == val:
            continue                      # already at the requested value
        changes.append({
            "offset": pos,
            "original": original.hex(),
            "patched": bytes((val,)).hex(),
        })
    return changes, dropped
