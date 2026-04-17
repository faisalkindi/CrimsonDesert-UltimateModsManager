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


def convert_texture_mod(
    mod_info: dict, game_dir: Path, work_dir: Path
) -> Path | None:
    """Convert a texture mod to a full PAZ + PAMT + PATHC overlay.

    Earlier versions produced only a modified ``meta/0.pathc`` and
    relied on the PATHC record's embedded DDS header to render textures.
    That's enough for textures that exist in vanilla PAZ (where the
    pixel data is already on disk), but for **new** texture paths like
    Aerophus's Barber Unlocked preview icons, the pixel data has
    nowhere to live and the game renders black circles.

    New flow:

    1. Allocate a fresh numbered PAMT directory (e.g. ``0036/``).
    2. For each DDS file, build a ``(content, metadata)`` tuple where
       ``entry_path`` is the full VFS path (e.g.
       ``ui/texture/image/customizeimage/foo.dds``) and
       ``compression_type`` is ``1`` (DDS).
    3. Hand the list to ``overlay_builder.build_overlay`` which
       produces PAZ (with JMM-parity partial DDS payloads) and PAMT
       (with the right folder hierarchy).
    4. Write ``work_dir/<new_dir>/0.paz`` and
       ``work_dir/<new_dir>/0.pamt`` — the unnumbered-PAZ fallback
       in ``_match_game_files`` will register both as a standalone mod.
    5. Also produce ``work_dir/meta/0.pathc`` with the same paths
       registered so the texture loader can resolve them.
       PATHC m-values + last4 come from the OverlayEntry that
       ``build_overlay`` returned — the same bytes that went into the
       overlay PAZ, so the loader never sees a mismatch.

    Args:
        mod_info: dict from :func:`detect_texture_mod`.
        game_dir: path to game installation root.
        work_dir: temporary directory for output.

    Returns:
        Path to ``work_dir`` containing PAZ + PAMT + PATHC ready for
        standard CDUMM import, or ``None`` on failure.
    """
    from cdumm.archive.overlay_builder import build_overlay
    from cdumm.archive.pathc_handler import update_entry
    from cdumm.engine.import_handler import _next_paz_directory

    # ── 1. Read vanilla PATHC (for later registration + last4 lookup) ──
    vanilla_backup = game_dir / "CDMods" / "vanilla" / "meta" / "0.pathc"
    game_pathc = game_dir / "meta" / "0.pathc"

    if vanilla_backup.exists():
        pathc_src = vanilla_backup
    elif game_pathc.exists():
        pathc_src = game_pathc
    else:
        logger.error("Texture mod: meta/0.pathc not found in game directory")
        return None

    try:
        pathc = read_pathc(pathc_src)
    except Exception as e:
        logger.error("Failed to parse PATHC: %s", e)
        return None

    dds_root = mod_info["dds_root"]
    dds_files = mod_info["dds_files"]

    # ── 2. Build entry list for build_overlay ────────────────────────
    target_dir = _next_paz_directory(game_dir)
    entries: list[tuple[bytes, dict]] = []
    entry_path_by_filename: dict[str, str] = {}
    dds_content_by_entry_path: dict[str, bytes] = {}

    for dds_file in sorted(dds_files):
        rel = dds_file.relative_to(dds_root)
        # entry_path is the VFS path WITHOUT leading slash — that's what
        # PAMT folder records store and what build_overlay's fallback
        # path-extractor expects.
        entry_path = rel.as_posix()
        content = dds_file.read_bytes()
        entries.append((content, {
            "entry_path": entry_path,
            "pamt_dir": target_dir,
            "compression_type": 1,
        }))
        entry_path_by_filename[dds_file.name.lower()] = entry_path
        dds_content_by_entry_path[entry_path] = content

    if not entries:
        logger.error("No textures to convert")
        return None

    # ── 3. Build PAZ + PAMT via overlay_builder ──────────────────────
    try:
        paz_bytes, pamt_bytes, overlay_entries = build_overlay(
            entries, game_dir=game_dir, vanilla_pathc_path=pathc_src,
        )
    except Exception as e:
        logger.error("build_overlay failed for texture mod: %s", e,
                     exc_info=True)
        return None

    # ── 4. Write PAZ + PAMT to work_dir/<target_dir>/ ────────────────
    out_paz_dir = work_dir / target_dir
    out_paz_dir.mkdir(parents=True, exist_ok=True)
    (out_paz_dir / "0.paz").write_bytes(paz_bytes)
    (out_paz_dir / "0.pamt").write_bytes(pamt_bytes)
    logger.info(
        "Texture mod: PAZ=%d bytes, PAMT=%d bytes written to %s/",
        len(paz_bytes), len(pamt_bytes), target_dir)

    # ── 5. Register each DDS in PATHC with m-values + last4 from the
    # overlay PAZ we just built. Using those authoritative values means
    # PATHC and PAZ can never disagree on reserved1 / reserved2 bytes,
    # which the texture loader cross-checks.
    added_count = 0
    for oe in overlay_entries:
        if oe.filename.lower() not in entry_path_by_filename:
            continue  # e.g. auto-included .pabgh siblings, not DDS
        entry_path = entry_path_by_filename[oe.filename.lower()]
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
        # Use the path as written into PAMT for hash consistency.
        vpath = "/" + entry_path
        update_entry(pathc, vpath, dds_idx, m_values)
        added_count += 1
        logger.debug(
            "PATHC register: %s -> dds_idx=%d, m=%s, last4=%#x",
            vpath, dds_idx, m_values, oe.dds_last4)

    # ── 6. Serialize PATHC ───────────────────────────────────────────
    try:
        modified_bytes = serialize_pathc(pathc)
    except Exception as e:
        logger.error("Failed to serialize PATHC: %s", e)
        return None

    out_dir = work_dir / "meta"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "0.pathc"
    out_path.write_bytes(modified_bytes)

    logger.info(
        "Texture mod: registered %d DDS entries in PATHC (%d -> %d bytes)",
        added_count, pathc_src.stat().st_size, len(modified_bytes))

    return work_dir
