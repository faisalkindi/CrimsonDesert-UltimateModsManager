"""Re-anchor byte-offset patches onto a table a Format 3 mod rebuilt.

GitHub #293 (falobos76, via #191). #294 made the unsafe combination REFUSE
instead of corrupting the game. This makes it WORK.

THE PROBLEM

A Format 3 mod doesn't patch bytes: CDUMM parses the whole table, edits
records and re-serializes it. Records change size, so every byte offset after
the first edited record moves. A Format 2 mod's fixed offsets then point into
the middle of some other record -- the table is invalid and the game won't
start.

Measured on falobos76's actual files (his socket mods + mod 2714 "Infinite
Durability Only", against the live CD 1.13 iteminfo table):

    231 of his byte-offset changes anchor correctly in vanilla
    229 of those 231 would write to the WRONG bytes after the rebuild

THE FIX, WITHOUT NEEDING TO UNDERSTAND THE TABLE

The Format 3 whole-table change already carries both halves:

    {"offset": 0, "original": <entire vanilla body>,
                  "patched":  <entire rebuilt body>}

and a record the Format 3 mod did NOT touch is byte-identical in both. So no
schema, no record index, no per-table knowledge is needed: locate the patch by
the bytes AROUND it.

What makes this safe rather than clever:

  1. The patch's ``original`` must be present at the old offset in VANILLA,
     or the mod was built for a different game version and was already broken
     before any Format 3 mod touched anything.
  2. Context is taken BEFORE the patch only. A window running past it would
     break on a LATER edit and falsely refuse a patch that never moved.
  3. Displacement is BOUNDED by the table's total size delta -- records only
     shift because earlier records grew or shrank. Exactly one candidate may
     fall inside that bound, or we refuse.
  4. The ``original`` bytes must be present at the NEW offset. A remap that
     doesn't land on the bytes the author measured is not a remap.

Refused, correctly: a record whose BYTES the Format 3 mod changed. The two
mods genuinely disagree about them, and silently picking one is the same bug
class as #259 / #275 / #278 / #285.

NOT refused: a record that merely GREW while the patched bytes stayed put. A
guard that over-fires is its own bug.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Bytes of context BEFORE the patch used to locate it in the rebuilt table.
#: Widened (never guessed) when a window is ambiguous.
_CTX = 48
_CTX_MAX = 4096


class ReanchorRefused(Exception):
    """This patch cannot be re-anchored safely. Do not apply it."""


def _find_all(hay: bytes, needle: bytes, limit: int = 64) -> list[int]:
    out: list[int] = []
    i = hay.find(needle)
    while i >= 0 and len(out) < limit:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


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

    # (1) does the patch even anchor in vanilla?
    if vanilla[offset:end] != original:
        raise ReanchorRefused(
            f"the bytes at offset {offset} are not the ones this mod expects "
            f"(it was built for a different game version)")

    if vanilla == rebuilt:
        return offset

    # Fast path: everything UP TO the patch is byte-identical, so no earlier
    # record grew or shrank and the offset cannot have moved. Most patches
    # take this path, and it avoids searching a 6 MB buffer for them.
    if len(rebuilt) >= end and vanilla[:end] == rebuilt[:end]:
        return offset

    # (3) An offset cannot move further than the table's total size change.
    #
    # Game tables are repetitive -- item records look much like one another --
    # so a context window legitimately matches in several places. The first
    # version of this refused 76 of the 231 changes in falobos76's real mod
    # for that reason alone: he'd have lost a third of his mod to "ambiguous".
    #
    # But displacement is bounded. Records shift only because earlier records
    # changed size, so |new - old| can never exceed the whole table's size
    # delta -- 160 bytes on his data, while the spurious matches are megabytes
    # away. Use the bound to discriminate, and still require exactly ONE
    # candidate inside it so we are never choosing between plausible answers.
    bound = abs(len(rebuilt) - len(vanilla))

    ctx = _CTX
    while ctx <= _CTX_MAX:
        lo = max(0, offset - ctx)
        window = vanilla[lo:end]          # (2) backward context only
        hits = _find_all(rebuilt, window)

        if not hits:
            raise ReanchorRefused(
                "the surrounding bytes no longer exist in the rebuilt table "
                "-- a Format 3 mod has changed this same record, so the two "
                "mods genuinely disagree about it")

        cands = [h + (offset - lo) for h in hits]
        near = [c for c in cands if abs(c - offset) <= bound]

        if len(near) == 1:
            new_off = near[0]
            # (4) the remap must land on the author's bytes
            if rebuilt[new_off:new_off + len(original)] != original:
                raise ReanchorRefused(
                    "the re-anchored offset does not carry the expected bytes")
            return new_off

        if not near and len(cands) == 1:
            raise ReanchorRefused(
                f"the only anchor is {abs(cands[0] - offset)} bytes away, but "
                f"the table only changed size by {bound} -- refusing to move "
                f"a patch further than the table can have shifted")

        ctx *= 2          # still ambiguous: widen the context, never guess

    raise ReanchorRefused(
        f"could not find a single anchor within {bound} bytes even with a "
        f"{_CTX_MAX}-byte context")


def reanchor_changes(changes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Re-anchor a file's byte-offset changes onto its Format 3 rebuild.

    ``changes`` is one game_file's aggregated list, which may contain a
    Format 3 whole-table change (offset 0, original == the whole vanilla body,
    patched == the whole rebuilt body) plus byte-offset changes in vanilla
    coordinates.

    Returns ``(kept, refused)``. ``kept`` carries rewritten offsets; the ones
    that could not be re-anchored are in ``refused`` and have been removed.
    """
    def _olen(c) -> int:
        return len(c.get("original") or "") // 2

    # Which change is the Format 3 rebuild? Not "the big one" -- a size
    # threshold is a magic number that breaks on small tables. The rebuild is
    # the change at offset 0 that SPANS PAST every other change: it replaces
    # the whole body, so by definition it covers them all.
    others_end = max(
        (int(c["offset"]) + _olen(c) for c in changes
         if isinstance(c.get("offset"), int) and c.get("offset") != 0),
        default=0)

    whole = None
    for c in changes:
        if c.get("offset") != 0 or not c.get("patched"):
            continue
        if _olen(c) > 0 and _olen(c) >= others_end:
            whole = c
            break
    if whole is None or others_end == 0:
        return changes, []          # no rebuild, or nothing to re-anchor

    try:
        vanilla = bytes.fromhex(whole["original"])
        rebuilt = bytes.fromhex(whole["patched"])
    except ValueError:
        return changes, []          # not hex we can read; leave it alone

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
                "offset re-anchored: %d -> %d (a Format 3 mod rebuilt this "
                "table)", off, new_off)
            c = {**c, "offset": new_off, "_reanchored_from": off}
        kept.append(c)

    if refused:
        logger.warning(
            "offset re-anchor: %d of %d change(s) could not be moved onto the "
            "rebuilt table and were dropped rather than applied to the wrong "
            "bytes", len(refused), len(refused) + len(kept) - 1)

    return kept, refused
