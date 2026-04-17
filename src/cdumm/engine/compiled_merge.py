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

    for mod_name, mod_decomp in mod_versions:
        limit = min(len(original_decomp), len(mod_decomp))
        i = 0
        while i < limit:
            if mod_decomp[i] != original_decomp[i]:
                start = i
                while i < limit and mod_decomp[i] != original_decomp[i]:
                    i += 1
                end = i
                merged[start:end] = mod_decomp[start:end]
                edits.append(MergeEdit(mod_name, start, end))
            else:
                i += 1

    warnings: list[str] = []
    # Overlap detection — O(E²) but E is tiny in practice (a handful of
    # edit regions per mod). JMM uses the same quadratic scan.
    for j in range(len(edits)):
        for k in range(j + 1, len(edits)):
            a = edits[j]
            b = edits[k]
            if a.mod_name == b.mod_name:
                continue
            if a.start < b.end and b.start < a.end:
                lo = max(a.start, b.start)
                hi = min(a.end, b.end)
                warnings.append(
                    f"[CONFLICT] {a.mod_name} vs {b.mod_name}: "
                    f"bytes 0x{lo:X}–0x{hi:X} overlap (last mod wins)"
                )
    if warnings:
        logger.info("compiled merge: %d byte-range overlap(s) across mods",
                    len(warnings))
    return bytes(merged), warnings
