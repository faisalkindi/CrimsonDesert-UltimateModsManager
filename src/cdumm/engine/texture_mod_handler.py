"""Texture mod handler for Crimson Desert.

Detects mods containing loose .dds texture files (no PAZ/PAMT) and
converts them to a full PAZ + PAMT + PATHC overlay so the game's
texture loader can actually find the pixel data.

Detection: folder contains .dds files but no .paz/.pamt files.

Pipeline:
1. Allocate a fresh PAMT directory number (0036+).
2. Call ``overlay_builder.build_overlay`` which produces PAZ (with
   JMM-parity partial DDS payloads) and PAMT (folder hierarchy, file
   records pointing at PAZ offsets).
3. Register the same paths in ``meta/0.pathc`` with m-values and last4
   from the overlay PAZ, so the texture loader's cross-checks pass.
4. CDUMM imports PAZ/PAMT via the standard unnumbered-PAZ fallback and
   the modified PATHC via the meta-file delta path.

Why: earlier versions only patched PATHC (148-byte template records
with zero pixel-data room). That worked for textures whose pixel data
was already in a vanilla PAZ, but mods that add *new* DDS paths (e.g.
Aerophus's Barber Unlocked preview icons) had no PAZ data to load and
rendered as black circles. This handler now ships the pixel data.
"""

import logging
import shutil
from pathlib import Path

from cdumm.archive.pathc_handler import (
    read_pathc, serialize_pathc, add_dds_file, add_folder_recursive,
    create_dds_record,
)

logger = logging.getLogger(__name__)


def detect_texture_mod(path: Path) -> dict | None:
    """Check if path contains a DDS texture mod.

    A texture mod is a folder with .dds files and no .paz/.pamt files.
    Returns metadata dict if texture mod, None otherwise.

    Args:
        path: directory to check (extracted zip or dropped folder)
    """
    if not path.is_dir():
        return None

    # Search root and one level deep for .dds files
    dds_files = list(path.rglob("*.dds"))
    if not dds_files:
        return None

    # Exclude if this contains PAZ/PAMT files (that's a PAZ mod, not texture)
    paz_files = list(path.rglob("*.paz")) + list(path.rglob("*.pamt"))
    if paz_files:
        return None

    # Find the DDS root: the common parent of all DDS files
    # For mods like "textures/armor/plate.dds", the root is the folder
    # For mods with a subfolder like "MyMod/textures/armor/plate.dds",
    # find the deepest common prefix
    dds_root = path
    # Check if all DDS files are under a single subfolder
    parents = set()
    for f in dds_files:
        rel = f.relative_to(path)
        if len(rel.parts) > 1:
            parents.add(rel.parts[0])

    if len(parents) == 1:
        candidate = path / parents.pop()
        # Check if this subfolder contains all the DDS files
        if all(f.is_relative_to(candidate) for f in dds_files):
            # Check if this looks like a mod wrapper (single subfolder with all content)
            # But only use it as root if the subfolder doesn't look like a game path
            sub_dds = list(candidate.rglob("*.dds"))
            if len(sub_dds) == len(dds_files):
                dds_root = candidate

    return {
        "dds_root": dds_root,
        "dds_files": dds_files,
        "dds_count": len(dds_files),
        "name": path.name,
    }


def build_texture_overlay(
    dds_entries: list[tuple[str, Path]],
    game_dir: Path,
    work_dir: Path,
) -> tuple[str, int] | None:
    """Build a full PAZ + PAMT + PATHC overlay for new DDS textures.

    Shared helper used by both the pure-texture import path (folder of
    loose DDS files) and the Crimson Browser path (mixed mod where CB
    routed known XMLs but left new DDSes unresolved).

    1. Allocate a fresh numbered PAMT directory (0036+).
    2. Read each DDS's bytes, build overlay entries with
       ``compression_type=1`` (DDS) and the caller-supplied VFS path.
    3. Call ``overlay_builder.build_overlay`` for PAZ + PAMT bytes.
    4. Write ``work_dir/<new_dir>/0.paz`` and ``0.pamt``.
    5. Register each DDS in ``work_dir/meta/0.pathc`` using the
       m-values + last4 that the overlay PAZ actually carries, so the
       texture loader's cross-checks pass.

    Args:
        dds_entries: list of ``(virtual_path, source_file)`` tuples.
            ``virtual_path`` is the full VFS path without leading slash
            (e.g. ``ui/texture/image/customizeimage/foo.dds``).
        game_dir: game installation root.
        work_dir: output directory — PAZ/PAMT + meta/0.pathc are written
            here. If ``meta/0.pathc`` already exists in ``work_dir``
            (e.g. another stage wrote it), its content is read first so
            additions stack.

    Returns:
        ``(target_paz_dir, registered_count)`` or ``None`` on failure.
    """
    from cdumm.archive.overlay_builder import build_overlay
    from cdumm.archive.pathc_handler import update_entry
    from cdumm.engine.import_handler import _next_paz_directory

    if not dds_entries:
        return None

    # ── Resolve PATHC source: prefer an already-staged copy in work_dir
    # so this helper can be called more than once and results stack. ──
    staged_pathc = work_dir / "meta" / "0.pathc"
    vanilla_backup = game_dir / "CDMods" / "vanilla" / "meta" / "0.pathc"
    game_pathc = game_dir / "meta" / "0.pathc"

    if staged_pathc.exists():
        pathc_src = staged_pathc
    elif vanilla_backup.exists():
        pathc_src = vanilla_backup
    elif game_pathc.exists():
        pathc_src = game_pathc
    else:
        logger.error("Texture overlay: meta/0.pathc not found")
        return None

    try:
        pathc = read_pathc(pathc_src)
    except Exception as e:
        logger.error("Failed to parse PATHC: %s", e)
        return None

    # Always pass the vanilla/game PATHC to build_overlay for last4 lookup,
    # never the staged one (which may already contain this mod's entries).
    lookup_pathc = vanilla_backup if vanilla_backup.exists() else game_pathc

    # ── Build entry list for build_overlay ────────────────────────────
    #
    # Two indexes keep PATHC registration path-accurate even when two
    # DDS files in the overlay share the same basename (a real thing in
    # texture packs — e.g. two /foo/normal.dds and /bar/normal.dds).
    # Previously we keyed by basename, so the second file overwrote the
    # first in the lookup dict and its PATHC row was written against
    # the wrong path. Now we carry the full virtual path through.
    target_dir = _next_paz_directory(game_dir)
    entries: list[tuple[bytes, dict]] = []
    entry_paths_by_filename: dict[str, list[str]] = {}
    dds_content_by_entry_path: dict[str, bytes] = {}

    for virtual_path, source_file in sorted(dds_entries):
        entry_path = virtual_path.lstrip("/")
        try:
            content = source_file.read_bytes()
        except Exception as e:
            logger.warning("Texture overlay: cannot read %s: %s", source_file, e)
            continue
        # Validate DDS magic — a file named *.dds without the magic is
        # garbage and would waste a PAZ slot while PATHC skips it below.
        # Reject at the door so a bad mod fails cleanly.
        if not content.startswith(b"DDS "):
            logger.warning(
                "Texture overlay: %s is not a valid DDS file "
                "(missing 'DDS ' magic) — skipping", source_file)
            continue
        entries.append((content, {
            "entry_path": entry_path,
            "pamt_dir": target_dir,
            "compression_type": 1,
        }))
        filename = entry_path.rsplit("/", 1)[-1].lower()
        entry_paths_by_filename.setdefault(filename, []).append(entry_path)
        dds_content_by_entry_path[entry_path] = content

    if not entries:
        logger.error("Texture overlay: no valid DDS inputs")
        return None

    # ── Build PAZ + PAMT via overlay_builder ─────────────────────────
    try:
        paz_bytes, pamt_bytes, overlay_entries = build_overlay(
            entries, game_dir=game_dir, vanilla_pathc_path=lookup_pathc,
        )
    except Exception as e:
        logger.error("build_overlay failed for texture overlay: %s", e,
                     exc_info=True)
        return None

    # ── Write PAZ + PAMT to work_dir/<target_dir>/ ───────────────────
    out_paz_dir = work_dir / target_dir
    out_paz_dir.mkdir(parents=True, exist_ok=True)
    (out_paz_dir / "0.paz").write_bytes(paz_bytes)
    (out_paz_dir / "0.pamt").write_bytes(pamt_bytes)
    logger.info(
        "Texture overlay: PAZ=%d bytes, PAMT=%d bytes written to %s/",
        len(paz_bytes), len(pamt_bytes), target_dir)

    # ── Register each DDS in PATHC with m-values + last4 from the
    # overlay PAZ we just built. Using those authoritative values means
    # PATHC and PAZ can never disagree on reserved1 / reserved2 bytes,
    # which the texture loader cross-checks.
    #
    # Match the overlay entry back to its source by FULL virtual path
    # (``dir_path/filename``), not basename alone — basename-only
    # matching silently lost one of every pair of files that share a
    # name but live in different folders.
    added_count = 0
    for oe in overlay_entries:
        # Reconstruct the virtual path the way build_overlay produced it.
        oe_full = (
            f"{oe.dir_path}/{oe.filename}" if oe.dir_path else oe.filename
        ).lstrip("/")
        # Fall back to basename lookup only if the exact virtual path
        # we supplied isn't represented — covers PAMT path-rewrites
        # that collapse folder prefixes.
        if oe_full in dds_content_by_entry_path:
            entry_path = oe_full
        else:
            candidates = entry_paths_by_filename.get(oe.filename.lower(), [])
            if len(candidates) == 0:
                continue  # e.g. auto-included .pabgh siblings, not DDS
            if len(candidates) > 1:
                logger.warning(
                    "PATHC register: ambiguous basename %s — overlay entry "
                    "matched to %s; %d other candidates skipped",
                    oe.filename, candidates[0], len(candidates) - 1)
            entry_path = candidates[0]
        content = dds_content_by_entry_path.get(entry_path, b"")
        if not content.startswith(b"DDS "):
            continue

        record_size = pathc.header.dds_record_size
        dds_rec = bytearray(record_size)
        dds_rec[: min(len(content), record_size)] = content[:record_size]
        dds_rec = bytes(dds_rec)

        try:
            dds_idx = pathc.dds_records.index(dds_rec)
        except ValueError:
            pathc.dds_records.append(dds_rec)
            dds_idx = len(pathc.dds_records) - 1

        m_values = oe.dds_m_values or (0, 0, 0, 0)
        vpath = "/" + entry_path
        update_entry(pathc, vpath, dds_idx, m_values)
        added_count += 1
        logger.debug(
            "PATHC register: %s -> dds_idx=%d, m=%s, last4=%#x",
            vpath, dds_idx, m_values, oe.dds_last4)

    if added_count == 0:
        logger.error(
            "Texture overlay: PAZ/PAMT written but zero DDS entries "
            "registered in PATHC — rejecting as broken")
        return None

    # ── Serialize PATHC ──────────────────────────────────────────────
    try:
        modified_bytes = serialize_pathc(pathc)
    except Exception as e:
        logger.error("Failed to serialize PATHC: %s", e)
        return None

    staged_pathc.parent.mkdir(parents=True, exist_ok=True)
    staged_pathc.write_bytes(modified_bytes)

    logger.info(
        "Texture overlay: registered %d DDS entries in PATHC (%d -> %d bytes)",
        added_count, pathc_src.stat().st_size, len(modified_bytes))

    return target_dir, added_count


def convert_texture_mod(
    mod_info: dict, game_dir: Path, work_dir: Path
) -> Path | None:
    """Convert a texture mod (pure DDS folder) to a PAZ + PAMT + PATHC overlay.

    Thin adapter over :func:`build_texture_overlay` that walks the
    mod_info dict produced by :func:`detect_texture_mod` and hands each
    DDS's ``(virtual_path, source_file)`` to the shared helper.
    """
    dds_root = mod_info["dds_root"]
    dds_files = mod_info["dds_files"]

    dds_entries: list[tuple[str, Path]] = []
    for dds_file in sorted(dds_files):
        rel = dds_file.relative_to(dds_root)
        dds_entries.append((rel.as_posix(), dds_file))

    result = build_texture_overlay(dds_entries, game_dir, work_dir)
    if result is None:
        return None
    return work_dir
