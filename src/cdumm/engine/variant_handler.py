"""Multi-variant JSON patch mod support.

When a user drops an archive that contains several JSON patch files (e.g.
"Trust Me 2x / 5x / 10x / 20x / Pet Abyss"), CDUMM stores them as ONE mod
row with a ``variants`` column instead of N separate rows. The cog icon
on the mod card lets the user toggle which variants are active without
re-importing.

Variants row format (stored as JSON text in ``mods.variants``)::

    [
      {"label": "Trust Me 10x",
       "filename": "friendly_gain_x10.json",
       "version": "1.1_ArmorPatched",
       "author": "GildyBoye",
       "enabled": true,
       "group": 0},
      ...
    ]

* ``group = -1`` → independent toggle (checkbox)
* ``group >= 0`` → radio group (only one variant in the group may be enabled)

Mutually-exclusive groups are detected at import time by scanning each
variant's ``patches[].changes[]`` for shared byte ranges on the same
``game_file``. Two variants that touch overlapping bytes get the same
group id.

Layout on disk::

    CDMods/mods/<mod_id>/variants/<original-filename>.json   (one per variant)
    CDMods/mods/<mod_id>/merged.json                         (json_source target)

The merged file is regenerated whenever the cog's Apply button fires —
it just concatenates the ``patches`` lists from every enabled variant.
The rest of the apply pipeline (``process_json_patches_for_overlay``) is
unaware that variant mods exist.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Data helpers ─────────────────────────────────────────────────────

def _change_spans(change: dict) -> list[tuple[int, int]]:
    """Return ``[(start, end), ...]`` byte ranges a change occupies.

    For ``replace`` ops the span is the original byte count. For ``insert``
    ops the span is a zero-width marker at the offset (inserts at the same
    offset from two different variants always conflict — insert order is
    undefined).
    """
    try:
        offset = int(change.get("offset", 0))
    except (ValueError, TypeError):
        return []
    ct = change.get("type", "replace")
    if ct == "insert":
        return [(offset, offset)]
    original = change.get("original", "")
    try:
        length = len(bytes.fromhex(original)) if original else 0
    except ValueError:
        length = 0
    if length <= 0:
        patched = change.get("patched", "")
        try:
            length = len(bytes.fromhex(patched)) if patched else 0
        except ValueError:
            length = 0
    if length <= 0:
        return []
    return [(offset, offset + length)]


def _variant_ranges(data: dict) -> dict[str, list[tuple[int, int]]]:
    """Collect all ``(game_file -> [byte_range, ...])`` a variant patches."""
    out: dict[str, list[tuple[int, int]]] = {}
    for patch in data.get("patches", []):
        gf = (patch.get("game_file") or "").strip().lower()
        if not gf:
            continue
        for change in patch.get("changes", []):
            for span in _change_spans(change):
                out.setdefault(gf, []).append(span)
    return out


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])


# ── Conflict detection ───────────────────────────────────────────────

def detect_conflict_groups(variant_data: list[dict]) -> list[int]:
    """Return a list of ``group`` ids aligned with ``variant_data``.

    Two variants share a group id iff at least one patched byte range on
    the same ``game_file`` overlaps. Variants with no overlap against any
    peer get ``-1``.
    """
    n = len(variant_data)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ranges = [_variant_ranges(d) for d in variant_data]

    for i in range(n):
        for j in range(i + 1, n):
            shared = set(ranges[i].keys()) & set(ranges[j].keys())
            if not shared:
                continue
            conflict = False
            for gf in shared:
                for sa in ranges[i][gf]:
                    for sb in ranges[j][gf]:
                        if _spans_overlap(sa, sb):
                            conflict = True
                            break
                    if conflict:
                        break
                if conflict:
                    break
            if conflict:
                union(i, j)

    # Assign ids. Singletons → -1. Groups of size ≥ 2 get 0, 1, 2…
    roots_to_members: dict[int, list[int]] = {}
    for i in range(n):
        roots_to_members.setdefault(find(i), []).append(i)

    group_ids = [-1] * n
    next_id = 0
    for root, members in roots_to_members.items():
        if len(members) < 2:
            continue
        for m in members:
            group_ids[m] = next_id
        next_id += 1
    return group_ids


# ── Import helpers ──────────────────────────────────────────────────

def build_variants_metadata(
    presets: list[tuple[Path, dict]],
    initial_selection: set[Path] | None = None,
) -> list[dict]:
    """Turn picker results into the ``variants`` JSON blob stored on the mod.

    ``initial_selection`` — paths the user ticked at the picker. Those start
    ``enabled=True`` (one per conflict group; the first tick in each group
    wins). Remaining presets are stored disabled so the cog panel can
    later enable them without re-importing.

    When ``initial_selection`` is None, falls back to JMM-ish behaviour:
    enable the first variant in each conflict group + all independents.
    """
    group_ids = detect_conflict_groups([data for _, data in presets])
    selection = initial_selection or set()
    seen_groups: set[int] = set()
    variants: list[dict] = []
    for idx, ((path, data), group) in enumerate(zip(presets, group_ids)):
        label = (data.get("name") or path.stem).strip()
        version = data.get("version")
        author = data.get("author")
        description = data.get("description")

        if selection:
            # User-driven: enabled if ticked AND (group-wise) first winner.
            picked = path in selection
            if picked:
                if group == -1:
                    enabled = True
                elif group not in seen_groups:
                    enabled = True
                    seen_groups.add(group)
                else:
                    enabled = False  # radio-group: only first tick wins
            else:
                enabled = False
        else:
            # Fallback: auto-pick first per group + all independents.
            if group == -1:
                enabled = True
            elif group not in seen_groups:
                enabled = True
                seen_groups.add(group)
            else:
                enabled = False

        variants.append({
            "label": label,
            "filename": path.name,
            "version": version,
            "author": author,
            "description": description,
            "enabled": bool(enabled),
            "group": int(group),
        })
    return variants


def copy_variants_to_mod_dir(
    presets: list[tuple[Path, dict]],
    mod_dir: Path,
) -> Path:
    """Copy each picked JSON into ``<mod_dir>/variants/`` (unique names).

    Returns the variants directory path. Existing files are overwritten so
    a re-import refreshes stale patch data.
    """
    vdir = mod_dir / "variants"
    vdir.mkdir(parents=True, exist_ok=True)
    for src_path, _data in presets:
        dest = vdir / src_path.name
        try:
            shutil.copy2(src_path, dest)
        except Exception as e:
            logger.error("variant copy failed (%s → %s): %s", src_path, dest, e)
    return vdir


# ── merged.json synthesis ───────────────────────────────────────────

def synthesize_merged_json(
    mod_dir: Path,
    variants: list[dict],
    base_meta: dict | None = None,
) -> Path:
    """Write ``<mod_dir>/merged.json`` with patches from every enabled
    variant concatenated. ``mods.json_source`` should point at this file.

    ``base_meta`` may supply ``name`` / ``description`` / ``author`` /
    ``version`` — otherwise those are drawn from the first enabled
    variant.
    """
    vdir = mod_dir / "variants"
    enabled = [v for v in variants if v.get("enabled")]

    merged: dict = {
        "patches": [],
        "_variant_source": True,
    }
    if base_meta:
        for k in ("name", "author", "version", "description"):
            val = base_meta.get(k)
            if val:
                merged[k] = val

    for v in enabled:
        vpath = vdir / v["filename"]
        if not vpath.exists():
            logger.warning("variant file missing: %s", vpath)
            continue
        try:
            data = json.loads(vpath.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("variant parse failed (%s): %s", vpath, e)
            continue
        # Borrow top-level fields from the first variant if base_meta didn't
        # supply them.
        for k in ("name", "author", "version", "description"):
            if k not in merged and data.get(k):
                merged[k] = data[k]
        for p in data.get("patches", []):
            merged["patches"].append(p)

    dest = mod_dir / "merged.json"
    dest.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return dest


# ── Single-row import for multi-variant drops ───────────────────────

def import_multi_variant(
    presets: list[tuple[Path, dict]],
    source: Path,
    game_dir: Path,
    mods_dir: Path,
    db,
    existing_mod_id: int | None = None,
    initial_selection: set[Path] | None = None,
) -> dict | None:
    """Turn a multi-JSON picker selection into a single mod row.

    Every preset passed in is copied under ``variants/`` so the cog can
    toggle any of them later. ``initial_selection`` — paths the user
    ticked at the picker — starts enabled; the rest are stored disabled
    (and accessible via the cog panel).

    Layout produced::

        CDMods/mods/<mod_id>/variants/<original-json-filename>   (one per preset)
        CDMods/mods/<mod_id>/merged.json                         (enabled subset)

    Returns ``{"mod_id": int, "mod_name": str, "variants": [...],
    "merged_json": str}`` on success, or ``None`` on empty input.
    """
    if not presets:
        return None

    variants_meta = build_variants_metadata(presets, initial_selection)
    mod_name = derive_mod_name_from_source(source, fallback=presets[0][0].stem)

    # Stamp against current game version so the mod can be flagged if the
    # game updates beneath it.
    from cdumm.engine.version_detector import detect_game_version
    try:
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        game_ver_hash = None

    # Pull author/version from the FIRST variant so the card shows something.
    first = presets[0][1]
    author = first.get("author")
    version = first.get("version")
    description = first.get("description")

    if existing_mod_id is not None:
        mod_id = existing_mod_id
        db.connection.execute(
            "DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        old_dir = mods_dir / str(mod_id)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
    else:
        priority = db.connection.execute(
            "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods"
        ).fetchone()[0]
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, "
            "description, game_version_hash, configurable) "
            "VALUES (?, 'paz', ?, ?, ?, ?, ?, 1)",
            (mod_name, priority, author, version, description, game_ver_hash),
        )
        mod_id = cur.lastrowid

    mod_dir = mods_dir / str(mod_id)
    mod_dir.mkdir(parents=True, exist_ok=True)

    copy_variants_to_mod_dir(presets, mod_dir)

    merged_meta = {
        "name": mod_name,
        "author": author,
        "version": version,
        "description": description,
    }
    merged_path = synthesize_merged_json(mod_dir, variants_meta, merged_meta)

    db.connection.execute(
        "UPDATE mods SET json_source = ?, variants = ?, configurable = 1 "
        "WHERE id = ?",
        (str(merged_path), json.dumps(variants_meta), mod_id),
    )
    db.connection.commit()

    logger.info("variant mod: created id=%d '%s' with %d variants "
                "(%d enabled, %d conflict groups)",
                mod_id, mod_name, len(variants_meta),
                sum(1 for v in variants_meta if v["enabled"]),
                len({v["group"] for v in variants_meta if v["group"] >= 0})),

    return {
        "mod_id": mod_id,
        "mod_name": mod_name,
        "variants": variants_meta,
        "merged_json": str(merged_path),
    }


def update_variant_selection(
    mod_id: int, selection: list[dict], mods_dir: Path, db,
) -> None:
    """Called by the cog's Apply button. ``selection`` must mirror the
    variants-row order; only the ``enabled`` field is used here. Radio-
    group exclusivity is enforced: at most one ``enabled=True`` per group.
    """
    row = db.connection.execute(
        "SELECT variants FROM mods WHERE id = ?", (mod_id,)
    ).fetchone()
    if not row or not row[0]:
        logger.warning("variant update: mod %d has no variants column", mod_id)
        return
    try:
        existing = json.loads(row[0])
    except Exception as e:
        logger.error("variant update: bad JSON on mod %d: %s", mod_id, e)
        return

    if len(selection) != len(existing):
        logger.error("variant update: selection length mismatch "
                     "(got %d, have %d)", len(selection), len(existing))
        return

    # Enforce at-most-one-enabled per positive group id.
    seen_groups: set[int] = set()
    for i, (v, chosen) in enumerate(zip(existing, selection)):
        enabled = bool(chosen.get("enabled"))
        g = v.get("group", -1)
        if enabled and g >= 0:
            if g in seen_groups:
                enabled = False
            else:
                seen_groups.add(g)
        v["enabled"] = enabled

    mod_dir = mods_dir / str(mod_id)
    merged_meta_row = db.connection.execute(
        "SELECT name, author, version, description FROM mods WHERE id = ?",
        (mod_id,),
    ).fetchone()
    merged_meta = {
        "name": merged_meta_row[0] if merged_meta_row else None,
        "author": merged_meta_row[1] if merged_meta_row else None,
        "version": merged_meta_row[2] if merged_meta_row else None,
        "description": merged_meta_row[3] if merged_meta_row else None,
    }
    merged_path = synthesize_merged_json(mod_dir, existing, merged_meta)

    db.connection.execute(
        "UPDATE mods SET variants = ?, json_source = ? WHERE id = ?",
        (json.dumps(existing), str(merged_path), mod_id),
    )
    db.connection.commit()
    logger.info("variant mod: mod_id=%d updated selection (%d/%d enabled)",
                mod_id,
                sum(1 for v in existing if v["enabled"]),
                len(existing))


# ── Archive-name guess ───────────────────────────────────────────────

def derive_mod_name_from_source(source: Path, fallback: str) -> str:
    """Given the ZIP / folder the user dropped, produce a display name.

    Just prettifies the archive / folder name — per user preference
    (option b.i): ONE mod card per drop, named for the archive itself.
    """
    from cdumm.engine.import_handler import prettify_mod_name
    stem = source.stem if source.is_file() else source.name
    pretty = prettify_mod_name(stem) if stem else ""
    return pretty or fallback
