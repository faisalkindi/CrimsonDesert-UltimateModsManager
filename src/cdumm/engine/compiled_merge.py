"""Byte-level merge for conflicting compiled-PAZ mods.

When two (or more) compiled-overlay mods ship their own version of the same
game file (e.g. ``gamedata/binary__/client/bin/iteminfo.pabgb``), CDUMM's
delta pipeline normally falls back to last-priority-wins. JMM V9.9.1 does
something smarter: it walks each mod's decompressed bytes against the
vanilla version, copies each mod's differing regions into a single merged
buffer, and reports byte-range overlaps between different mods as
conflicts.

Ported from JMM ``ModManager.cs:2245 MergeCompiledModFiles`` (MPL-2.0).

The semantic-merge variant (``SemanticMerge.MergeCompiledPabgb``) relies
on a ``.pabgh`` schema and is more invasive — not ported in this pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MergeEdit:
    """A single contiguous byte-range a mod overwrites against vanilla."""
    mod_name: str
    start: int   # inclusive
    end: int     # exclusive


def merge_compiled_mod_files(
    original_decomp: bytes,
    mod_versions: list[tuple[str, bytes]],
) -> tuple[bytes, list[str]]:
    """Byte-merge multiple mod versions of a file against the vanilla.

    Walk each mod's decompressed bytes. Every contiguous run where a mod
    differs from the vanilla gets copied into the merged buffer. Later
    mods overwrite earlier ones; overlaps are reported but not blocked.

    Parameters
    ----------
    original_decomp :
        The vanilla file's decompressed bytes.
    mod_versions :
        ``[(mod_name, mod_decomp_bytes), ...]`` in priority order (lowest
        priority first, highest last — so the highest-priority mod's bytes
        win in any overlap).

    Returns
    -------
    ``(merged_bytes, warnings)``. ``merged_bytes`` always has the same
    length as ``original_decomp`` (extra bytes past that length in a mod
    version are ignored — matching JMM's behaviour).
    """
    merged = bytearray(original_decomp)
    edits: list[MergeEdit] = []

    # Chunk-wise scan: most bytes are identical between mod and vanilla
    # (mods only edit small regions). Walking byte by byte in pure Python
    # against a 25 MB pabgb is brutally slow — minute(s) per mod. Compare
    # 64 KB chunks first; only fall back to byte-by-byte inside chunks
    # that actually differ. ~1000x speedup on typical loadouts.
    CHUNK = 65536
    for mod_name, mod_decomp in mod_versions:
        limit = min(len(original_decomp), len(mod_decomp))
        # Use memoryview slicing so the chunk comparisons don't allocate.
        orig_mv = memoryview(original_decomp)
        mod_mv = memoryview(mod_decomp)
        chunk_start = 0
        while chunk_start < limit:
            chunk_end = min(chunk_start + CHUNK, limit)
            # Fast path: whole chunk matches → skip it.
            if orig_mv[chunk_start:chunk_end].tobytes() == \
                    mod_mv[chunk_start:chunk_end].tobytes():
                chunk_start = chunk_end
                continue
            # Slow path: walk byte by byte INSIDE this chunk only.
            i = chunk_start
            while i < chunk_end:
                if mod_decomp[i] != original_decomp[i]:
                    start = i
                    while i < chunk_end and mod_decomp[i] != original_decomp[i]:
                        i += 1
                    # If the differing run continues past the chunk
                    # boundary, extend it without splitting the edit.
                    while i < limit and mod_decomp[i] != original_decomp[i]:
                        i += 1
                    end = i
                    merged[start:end] = mod_decomp[start:end]
                    edits.append(MergeEdit(mod_name, start, end))
                else:
                    i += 1
            chunk_start = i if i > chunk_end else chunk_end

    warnings: list[str] = []
    # Overlap detection. Original loop was O(E²) and assumed "E is tiny
    # in practice." That assumption breaks on tables like iteminfo.pabgb
    # where two mods can produce thousands of edit runs each — 9000² is
    # 81 million pure-Python iterations, ~minute(s) of wallclock. Real
    # bug: users hit it on Apply with conflicting item-table mods.
    #
    # Sweep-line approach: sort edits by start, walk in order, maintain
    # the currently-active edit; emit a warning only when the next edit
    # starts before the active one ends. O(E log E) — for 9000 edits
    # that's ~40k operations instead of 40 million.
    if len(edits) >= 2:
        sorted_edits = sorted(edits, key=lambda e: (e.start, e.end))
        active = sorted_edits[0]
        for e in sorted_edits[1:]:
            if e.start < active.end and e.mod_name != active.mod_name:
                lo = max(active.start, e.start)
                hi = min(active.end, e.end)
                warnings.append(
                    f"[CONFLICT] {active.mod_name} vs {e.mod_name}: "
                    f"bytes 0x{lo:X}-0x{hi:X} overlap (last mod wins)"
                )
            # Advance the active interval to whichever ends later. This
            # captures chains of overlaps without re-checking pairs.
            if e.end > active.end:
                active = e
    if warnings:
        logger.info("compiled merge: %d byte-range overlap(s) across mods",
                    len(warnings))
    return bytes(merged), warnings
