"""Re-anchor byte-offset patches onto a table a Format 3 mod rebuilt.

GitHub #293 (falobos76, via #191). #294 made the unsafe combination REFUSE
instead of corrupting the game. This makes it WORK.

THE PROBLEM

A Format 3 mod doesn't patch bytes: CDUMM parses the whole table, edits
records and re-serializes it. Records change size, so every byte offset
after the first edited record moves. A Format 2 mod's fixed offsets then
point into the middle of some other record -- the table is invalid and the
game won't start.

THE FIX, WITHOUT NEEDING TO UNDERSTAND THE TABLE

The Format 3 whole-table change already carries both halves:

    {"offset": 0, "original": <entire vanilla body>,
                  "patched":  <entire rebuilt body>}

and a record the Format 3 mod did NOT touch is byte-identical in both. So we
don't need the schema, the record index, or any per-table knowledge: take the
CONTEXT around the old offset in vanilla and find it in the rebuilt table.

    vanilla:  ... [ctx_before][target bytes][ctx_after] ...
    rebuilt:  ......... [ctx_before][target bytes][ctx_after] ...
                        ^ new offset

Three things make this safe rather than clever:

  1. The window must match EXACTLY ONCE. Zero matches means the bytes moved
     or changed; several means the anchor is ambiguous. Either way we refuse
     -- we never "pick the first one".
  2. The patch's own ``original`` bytes must be present at the old offset in
     vanilla. If they aren't, the mod was built for a different game version
     and was already broken before any of this.
  3. The ``original`` bytes must be present at the NEW offset in rebuilt. A
     remap that doesn't land on the bytes the author measured is not a remap.

If a Format 3 mod edited the very record a byte patch points into, the window
won't match and we refuse THAT patch -- which is correct: the two mods really
do disagree about those bytes, and silently picking one would be the same
class of bug we keep fixing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Bytes of context either side of the patched span used to locate it in the
#: rebuilt table. Long enough to be unique in a multi-MB table; if it isn't,
#: we widen rather than guess.
_CTX = 48
_CTX_MAX = 512


class ReanchorRefused(Exception):
    """This patch cannot be re-anchored safely. Do not apply it."""


def _unique_find(hay: bytes, needle: bytes) -> int:
    first = hay.find(needle)
    if first < 0:
        return -1
    if hay.find(needle, first + 1) >= 0:
        return -2          # ambiguous
    return first


def reanchor_offset(vanilla: bytes, rebuilt: bytes, offset: int,
                    original: bytes) -> int:
    """Map ``offset`` (a vanilla coordinate) into the rebuilt table.

    Raises ReanchorRefused rather than returning a guess.
    """
    end = offset + len(original)
    if end > len(vanilla):
        raise ReanchorRefused(
            f"offset {offset} + {len(original)} bytes runs past the end of "
            f"the vanilla table ({len(vanilla)} bytes)")

    # (2) does the patch even anchor in vanilla?
    if vanilla[offset:end] != original:
        raise ReanchorRefused(
            f"the bytes at offset {offset} are not the ones this mod expects "
            f"(it was built for a different game version)")

    # unchanged tables need no remap at all
    if vanilla == rebuilt:
        return offset

    # Fast path: if everything UP TO the patch is byte-identical, no earlier
    # record grew or shrank, so the offset cannot have moved. This is most
    # patches, and it avoids searching a 6 MB buffer for them.
    if len(rebuilt) >= end and vanilla[:end] == rebuilt[:end]:
        return offset

    # Context is taken BEFORE the patch only, never after.
    #
    # A window that ran past the patch would break on a LATER edit: the bytes
    # ahead of an untouched record can change, and then a patch that never
    # moved at all gets falsely refused. Bytes BEFORE it cannot contain a
    # later edit by definition -- and if an EARLIER edit lands inside that
    # context, the window won't match and we refuse, which is exactly right:
    # a Format 3 mod rewrote the very record this patch points into, so the
    # two mods genuinely disagree.
    ctx = _CTX
    while ctx <= _CTX_MAX:
        lo = max(0, offset - ctx)
        window = vanilla[lo:end]
        found = _unique_find(rebuilt, window)
        if found >= 0:
            new_off = found + (offset - lo)
            # (3) the remap must land on the author's bytes
            if rebuilt[new_off:new_off + len(original)] != original:
                raise ReanchorRefused(
                    "re-anchored offset does not carry the expected bytes")
            return new_off
        if found == -1:
            raise ReanchorRefused(
                "the surrounding bytes no longer exist in the rebuilt table "
                "-- a Format 3 mod has changed this same record, so the two "
                "mods genuinely disagree about it")
        ctx *= 2           # ambiguous: widen the context, don't guess

    raise ReanchorRefused(
        "could not find a unique anchor even with a 512-byte context")


def reanchor_changes(changes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Re-anchor a file's byte-offset changes onto its Format 3 rebuild.

    ``changes`` is one game_file's aggregated list, which may contain:
      * a Format 3 whole-table change: offset 0, original == the whole
        vanilla body, patched == the whole rebuilt body;
      * byte-offset changes in vanilla coordinates.

    Returns ``(changes, refused)``. Offsets are rewritten IN PLACE on the
    returned list; ``refused`` holds the ones that could not be re-anchored,
    already removed.
    """
    def _olen(c) -> int:
        return len(c.get("original") or "") // 2

    # Which change is the Format 3 rebuild? Not "the big one" -- a size
    # threshold is a magic number that breaks on small tables. The rebuild is
    # the change at offset 0 that SPANS PAST every other change: it replaces
    # the whole body, so by definition it covers them all. That's a statement
    # about what the change IS, not about how big it happens to be.
    others_end = max(
        (int(c["offset"]) + _olen(c) for c in changes
         if isinstance(c.get("offset"), int) and c.get("offset") != 0),
        default=0)

    whole = None
    for c in changes:
        if c.get("offset") != 0 or not c.get("patched"):
            continue
        if _olen(c) >= others_end and _olen(c) > 0:
            whole = c
            break
    if whole is None or others_end == 0:
        return changes, []           # no rebuild (or nothing to re-anchor)

    try:
        vanilla = bytes.fromhex(whole["original"])
        rebuilt = bytes.fromhex(whole["patched"])
    except ValueError:
        return changes, []           # not hex we can read; leave it alone

    kept: list[dict] = []
    refused: list[dict] = []
    for c in changes:
        if c is whole:
            kept.append(c)
            continue
        off = c.get("offset")
        if not isinstance(off, int) or off <= 0:
            kept.append(c)
            continue
        try:
            original = bytes.fromhex(c.get("original") or "")
        except ValueError:
            kept.append(c)
            continue
        if not original:
            kept.append(c)
            continue

        try:
            new_off = reanchor_offset(vanilla, rebuilt, off, original)
        except ReanchorRefused as e:
            logger.warning(
                "offset re-anchor REFUSED for %s at offset %d: %s",
                c.get("label") or c.get("_source_mod_name") or "a mod",
                off, e)
            refused.append({**c, "_refuse_reason": str(e)})
            continue

        if new_off != off:
            logger.info(
                "offset re-anchored: %d -> %d (the table was rebuilt by a "
                "Format 3 mod)", off, new_off)
            c = {**c, "offset": new_off, "_reanchored_from": off}
        kept.append(c)

    return kept, refused
