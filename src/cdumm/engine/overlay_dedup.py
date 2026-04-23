"""Collapse duplicate overlay entries for the same (pamt_dir, entry_path).

Runs as the first step of Phase 1b (overlay build) in ``apply_engine``.
When more than one apply phase contributes an entry for the same
target file inside the same PAZ directory — JSON Phase 1a and an ENTR
rewrite both targeting the same prefab, for example — today the
overlay builder picks a priority winner silently and the other mod's
changes are lost. After this pass, a true byte-level merge against
vanilla produces one entry with non-overlapping edits from every
contributor; overlap regions go to the highest-precedence (lowest
CDUMM priority number) mod.

Reuses :func:`cdumm.engine.compiled_merge.merge_compiled_mod_files`,
which already handles the three-way byte merge and overlap detection
for ENTR-vs-ENTR collisions; this module just routes N-way overlay
collisions through the same algorithm.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Optional

from cdumm.engine.compiled_merge import merge_compiled_mod_files

logger = logging.getLogger(__name__)


# Priority sentinel for overlay entries without an explicit priority —
# treat as lowest-precedence (= highest CDUMM priority number).
_NO_PRIORITY = 999_999


def merge_duplicate_overlay_entries(
    overlay_entries: list[tuple[bytes, dict]],
    vanilla_resolver: Callable[[str, str], Optional[bytes]],
) -> tuple[list[tuple[bytes, dict]], list[str]]:
    """Collapse multiple contributors for the same overlay target.

    Parameters
    ----------
    overlay_entries :
        List of ``(body_bytes, metadata)`` tuples — the flat list
        ``apply_engine._overlay_entries`` accumulates across all
        phases. Metadata must carry ``pamt_dir`` and ``entry_path``
        for grouping; optional ``priority`` (int) for merge ordering
        and ``mod_name`` (str) for diagnostics.
    vanilla_resolver :
        ``callable(pamt_dir, entry_path) -> bytes | None``. Returns
        the vanilla bytes of the target entry. When it returns None
        we cannot safely merge, so we fall back to priority-pick.

    Returns
    -------
    ``(merged_entries, warnings)``:

    - ``merged_entries`` — the de-duplicated list. Same shape as the
      input; groups of size 1 pass through. Groups larger than 1 are
      collapsed into one entry.
    - ``warnings`` — user-facing strings (overlap reports,
      vanilla-unavailable fallbacks). Caller surfaces them via the
      existing soft-warning / InfoBar path.
    """
    groups: dict[tuple[str, str], list[tuple[bytes, dict]]] = defaultdict(list)
    order: list[tuple[str, str]] = []
    for body, meta in overlay_entries:
        key = (meta.get("pamt_dir", ""), meta.get("entry_path", ""))
        if key not in groups:
            order.append(key)
        groups[key].append((body, meta))

    merged_entries: list[tuple[bytes, dict]] = []
    warnings: list[str] = []

    for key in order:
        group = groups[key]
        if len(group) == 1:
            merged_entries.append(group[0])
            continue

        pamt_dir, entry_path = key
        try:
            vanilla = vanilla_resolver(pamt_dir, entry_path)
        except Exception as e:
            logger.warning(
                "overlay-dedup: vanilla resolver raised for %s/%s: %s",
                pamt_dir, entry_path, e)
            vanilla = None

        if vanilla is None:
            best = min(group, key=_priority_key)
            contributor_names = [m.get("mod_name", "?") for _, m in group]
            warnings.append(
                f"Cannot merge {entry_path} — vanilla unavailable. "
                f"Using priority winner "
                f"'{best[1].get('mod_name', 'unknown')}' over "
                f"{contributor_names}.")
            merged_entries.append(best)
            continue

        # Sort by CDUMM priority DESC so the lowest-priority-number
        # (highest-precedence) mod feeds LAST to merge_compiled_mod_files
        # — its bytes win on any overlap.
        sorted_group = sorted(group, key=_priority_key, reverse=True)
        mod_versions = [
            (meta.get("mod_name", "unknown"), body)
            for body, meta in sorted_group
        ]

        try:
            merged_body, merge_warnings = merge_compiled_mod_files(
                vanilla, mod_versions)
        except Exception as e:
            logger.warning(
                "overlay-dedup: merge failed for %s/%s: %s — "
                "falling back to priority-pick", pamt_dir, entry_path, e)
            merged_entries.append(min(group, key=_priority_key))
            warnings.append(
                f"Could not merge overlapping edits for {entry_path}: {e}")
            continue

        warnings.extend(merge_warnings)

        if merged_body == vanilla:
            # Every contributor's delta was reverted by a higher-
            # precedence contributor — emitting an overlay entry
            # that equals vanilla would round-trip the file to
            # vanilla for no reason.
            logger.debug(
                "overlay-dedup: merge for %s produced vanilla — "
                "dropping entry", entry_path)
            continue

        winner_meta = min(group, key=_priority_key)[1]
        merged_meta = dict(winner_meta)
        merged_meta["_merged_from"] = [m.get("mod_name", "?") for _, m in group]
        merged_entries.append((bytes(merged_body), merged_meta))
        logger.info(
            "overlay-dedup: merged %d contributors for %s/%s: %s",
            len(group), pamt_dir, entry_path,
            merged_meta["_merged_from"])

    return merged_entries, warnings


def _priority_key(body_meta: tuple[bytes, dict]) -> int:
    """Sort key — lower value means HIGHER CDUMM precedence (priority=1
    is the top slot, priority=10 is lower in the load order)."""
    _, meta = body_meta
    try:
        return int(meta.get("priority", _NO_PRIORITY))
    except (TypeError, ValueError):
        return _NO_PRIORITY
