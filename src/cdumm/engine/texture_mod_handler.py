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
from typing import TYPE_CHECKING

from cdumm.engine.cdmods_paths import get_cdmods_root

if TYPE_CHECKING:
    from cdumm.storage.config import Config

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
    config: "Config | None" = None,
) -> tuple[str, int] | None:
    """Build a full PAZ + PAMT + PATHC overlay for new DDS textures.

    Thin DDS-only wrapper over :func:`build_new_files_overlay`, kept for
    the pure-texture import path and existing callers.

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
    if not dds_entries:
        return None
    result = build_new_files_overlay(
        [(vp, src, {}) for vp, src in dds_entries], game_dir, work_dir,
        config=config)
    if result is None:
        return None
    target_dir, _packed, registered = result
    return target_dir, registered


def build_new_files_overlay(
    new_entries: list[tuple[str, Path, dict]],
    game_dir: Path,
    work_dir: Path,
    config: "Config | None" = None,
) -> tuple[str, int, int] | None:
    """Pack files that have NO vanilla PAMT entry into ONE overlay group.

    Shared helper for the pure-texture import path (folder of loose DDS
    files) and the Crimson Browser path (mixed mod whose known files
    were repacked into their vanilla PAZ dirs but which also ships new
    files: preview DDSes, new prefabs, meshes, model properties).

    Everything goes into a single ``build_overlay`` call because
    ``_next_paz_directory(game_dir)`` returns the same number on every
    call (it reads the game dir, not ``work_dir``): two calls would
    write two groups to the same directory and the second would
    overwrite the first.

    1. Allocate a fresh numbered PAMT directory (0036+).
    2. DDS: ``compression_type=1`` (JMM partial payload) and registered
       in ``work_dir/meta/0.pathc`` with the m-values + last4 the overlay
       PAZ actually carries, so the texture loader's cross-checks pass.
    3. Non-DDS: compression/encryption from the per-entry ``options``
       dict (the caller derives them from the vanilla convention of the
       file's own numbered dir); without options, raw below 4096 bytes
       and LZ4 above, unencrypted.

    Args:
        new_entries: list of ``(virtual_path, source_file, options)``.
            ``options`` may carry ``compression_type`` (0 or 2 for
            non-DDS), ``encrypted`` (bool) and ``pamt_dir`` (the vanilla
            dir the file belongs to, used for folder-path lookups).
        game_dir: game installation root.
        work_dir: output directory.

    Returns:
        ``(target_paz_dir, files_packed, dds_registered)`` or ``None``
        on failure.
    """
    from cdumm.archive.overlay_builder import build_overlay
    from cdumm.archive.pathc_handler import update_entry
    from cdumm.engine.import_handler import _next_paz_directory

    if not new_entries:
        return None

    target_dir = _next_paz_directory(game_dir)
    entries: list[tuple[bytes, dict]] = []
    # Two indexes keep PATHC registration path-accurate even when two
    # DDS files in the overlay share the same basename (a real thing in
    # texture packs — e.g. two /foo/normal.dds and /bar/normal.dds).
    entry_paths_by_filename: dict[str, list[str]] = {}
    dds_content_by_entry_path: dict[str, bytes] = {}
    seen_paths: set[str] = set()

    for virtual_path, source_file, options in sorted(
            new_entries, key=lambda t: (t[0], str(t[1]))):
        entry_path = virtual_path.lstrip("/")
        if entry_path.lower() in seen_paths:
            logger.warning("New-file overlay: duplicate path %s — keeping "
                           "the first copy", entry_path)
            continue
        try:
            content = source_file.read_bytes()
        except Exception as e:
            logger.warning("New-file overlay: cannot read %s: %s",
                           source_file, e)
            continue
        if entry_path.lower().endswith(".dds"):
            # Validate DDS magic — a file named *.dds without the magic
            # is garbage and would waste a PAZ slot while PATHC skips it.
            if not content.startswith(b"DDS "):
                logger.warning(
                    "Texture overlay: %s is not a valid DDS file "
                    "(missing 'DDS ' magic) — skipping", source_file)
                continue
            meta = {
                "entry_path": entry_path,
                "pamt_dir": target_dir,
                "compression_type": 1,
            }
            filename = entry_path.rsplit("/", 1)[-1].lower()
            entry_paths_by_filename.setdefault(filename, []).append(entry_path)
            dds_content_by_entry_path[entry_path] = content
        else:
            comp = options.get("compression_type")
            if comp not in (0, 2):
                # Type 1 is the DDS partial-payload layout in
                # build_overlay; never hand it a non-DDS file.
                comp = 0 if len(content) < 4096 else 2
            meta = {
                "entry_path": entry_path,
                "pamt_dir": options.get("pamt_dir") or target_dir,
                "compression_type": comp,
            }
            if options.get("encrypted"):
                meta["encrypted"] = True
        seen_paths.add(entry_path.lower())
        entries.append((content, meta))

    if not entries:
        logger.error("New-file overlay: no valid inputs")
        return None

    pathc = None
    pathc_src = None
    staged_pathc = work_dir / "meta" / "0.pathc"
    vanilla_backup = get_cdmods_root(config, game_dir) / "vanilla" / "meta" / "0.pathc"
    game_pathc = game_dir / "meta" / "0.pathc"
    # Always pass the vanilla/game PATHC to build_overlay for last4 lookup,
    # never the staged one (which may already contain this mod's entries).
    lookup_pathc = vanilla_backup if vanilla_backup.exists() else game_pathc
    if dds_content_by_entry_path:
        # Prefer an already-staged copy in work_dir so additions stack.
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

    # ── Build PAZ + PAMT via overlay_builder (ONE call, ONE group) ──
    try:
        paz_bytes, pamt_bytes, overlay_entries = build_overlay(
            entries, game_dir=game_dir, vanilla_pathc_path=lookup_pathc,
        )
    except Exception as e:
        logger.error("build_overlay failed for new-file overlay: %s", e,
                     exc_info=True)
        return None

    out_paz_dir = work_dir / target_dir
    out_paz_dir.mkdir(parents=True, exist_ok=True)
    (out_paz_dir / "0.paz").write_bytes(paz_bytes)
    (out_paz_dir / "0.pamt").write_bytes(pamt_bytes)
    logger.info(
        "New-file overlay: %d entries (%d DDS), PAZ=%d bytes, PAMT=%d "
        "bytes written to %s/", len(entries), len(dds_content_by_entry_path),
        len(paz_bytes), len(pamt_bytes), target_dir)

    if pathc is None:
        return target_dir, len(entries), 0

    # ── Register each DDS in PATHC with m-values + last4 from the
    # overlay PAZ we just built, matched back by FULL virtual path. ──
    added_count = 0
    for oe in overlay_entries:
        oe_full = (
            f"{oe.dir_path}/{oe.filename}" if oe.dir_path else oe.filename
        ).lstrip("/")
        if oe_full in dds_content_by_entry_path:
            entry_path = oe_full
        else:
            candidates = entry_paths_by_filename.get(oe.filename.lower(), [])
            if len(candidates) == 0:
                continue  # non-DDS entry or auto-included .pabgh sibling
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

    return target_dir, len(entries), added_count


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
