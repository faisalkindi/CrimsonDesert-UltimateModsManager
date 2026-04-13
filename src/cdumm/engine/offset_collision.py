"""Offset collision detection for JSON patch mods.

Given two (or more) lists of patch changes, determines whether any byte
ranges overlap — which would make simultaneous application unsafe.

A 'change' dict must contain at least:
    - 'offset' (int): absolute byte position in the decompressed file
    - 'original' (str): hex string of original bytes (length defines range size)
    - 'patched'  (str): hex string of replacement bytes
"""
from __future__ import annotations

from typing import NamedTuple


class CollisionInfo(NamedTuple):
    """Details about a detected collision between two changes."""
    label_a: str
    label_b: str
    offset: int
    length: int


def _change_range(change: dict) -> tuple[int, int]:
    """Return (start, end_exclusive) byte range for a single change.

    Uses 'original' length to determine size; falls back to 'patched'.
    Raises KeyError if 'offset' is missing (matches apply path behavior).
    """
    raw = change["offset"]
    offset = int(raw, 0) if isinstance(raw, str) else int(raw)
    hex_src = change.get("original") or change.get("patched")
    if not hex_src:
        return offset, offset  # zero-width range, no collision possible
    try:
        size = len(bytes.fromhex(hex_src))
    except ValueError:
        size = len(hex_src) // 2 or 1  # best-effort fallback
    return offset, offset + size


def _build_range_map(changes: list[dict]) -> list[tuple[int, int, str]]:
    """Build sorted list of (start, end, label) from changes."""
    result = []
    for c in changes:
        start, end = _change_range(c)
        label = c.get("label", f"offset 0x{start:X}")
        result.append((start, end, label))
    result.sort()
    return result


def detect_collisions(
    changes_a: list[dict],
    changes_b: list[dict],
    name_a: str = "Mod A",
    name_b: str = "Mod B",
) -> list[CollisionInfo]:
    """Identify overlapping byte ranges between two sets of changes.

    Args:
        changes_a: list of change dicts from mod A
        changes_b: list of change dicts from mod B
        name_a: display name for mod A
        name_b: display name for mod B

    Returns:
        List of CollisionInfo for each pair of overlapping changes.
    """
    ranges_a = _build_range_map(changes_a)
    ranges_b = _build_range_map(changes_b)
    collisions: list[CollisionInfo] = []

    for start_a, end_a, label_a in ranges_a:
        for start_b, end_b, label_b in ranges_b:
            # Check overlap: ranges overlap if start < other_end and end > other_start
            if start_a < end_b and end_a > start_b:
                overlap_start = max(start_a, start_b)
                overlap_end = min(end_a, end_b)
                collisions.append(CollisionInfo(
                    label_a=f"{name_a}: {label_a}",
                    label_b=f"{name_b}: {label_b}",
                    offset=overlap_start,
                    length=overlap_end - overlap_start,
                ))

    return collisions


def detect_collisions_multi(
    mod_changes: dict[str, list[dict]],
) -> list[CollisionInfo]:
    """Detect collisions across all pairs of mods.

    Args:
        mod_changes: {mod_name: list_of_change_dicts}

    Returns:
        All collisions found across all mod pairs.
    """
    mod_names = sorted(mod_changes.keys())
    all_collisions: list[CollisionInfo] = []

    for i, name_a in enumerate(mod_names):
        for name_b in mod_names[i + 1:]:
            collisions = detect_collisions(
                mod_changes[name_a], mod_changes[name_b],
                name_a, name_b)
            all_collisions.extend(collisions)

    return all_collisions


def group_collisions_matrix(
    mod_changes: dict[str, list[dict]],
) -> dict[tuple[str, str], int]:
    """Build a collision count matrix between all mod pairs.

    Returns {(mod_a, mod_b): collision_count} for pairs with collisions.
    """
    mod_names = sorted(mod_changes.keys())
    matrix: dict[tuple[str, str], int] = {}

    for i, name_a in enumerate(mod_names):
        for name_b in mod_names[i + 1:]:
            count = len(detect_collisions(
                mod_changes[name_a], mod_changes[name_b],
                name_a, name_b))
            if count > 0:
                matrix[(name_a, name_b)] = count

    return matrix
