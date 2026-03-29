"""Texture mod handler for Crimson Desert.

Detects mods containing .dds texture files and converts them to
PATHC modifications (meta/0.pathc) that CDUMM can import as deltas.

Detection: folder contains .dds files but no .paz/.pamt files.

Pipeline:
1. Read vanilla meta/0.pathc
2. For each .dds file, compute virtual path hash and add to PATHC index
3. Write modified 0.pathc to work directory
4. CDUMM generates a delta against vanilla 0.pathc for revert support
"""

import logging
import shutil
from pathlib import Path

from cdumm.archive.pathc_handler import (
    read_pathc, serialize_pathc, add_dds_file, add_folder_recursive,
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
    """Convert a texture mod to a PATHC delta.

    Reads vanilla meta/0.pathc, adds all DDS texture entries,
    writes modified 0.pathc to work_dir/meta/0.pathc.

    Args:
        mod_info: dict from detect_texture_mod()
        game_dir: path to game installation root
        work_dir: temporary directory for output

    Returns:
        Path to work_dir containing modified meta/0.pathc, or None on failure.
    """
    # Find vanilla PATHC
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

    added_count = 0
    for dds_file in sorted(dds_files):
        rel = dds_file.relative_to(dds_root)
        vpath = "/" + rel.as_posix()

        try:
            dds_idx = add_dds_file(pathc, dds_file, vpath)
            added_count += 1
            logger.info("Added texture: %s (DDS index %d)", vpath, dds_idx)
        except Exception as e:
            logger.error("Failed to add texture %s: %s", vpath, e)
            return None

    if added_count == 0:
        logger.error("No textures were added to PATHC")
        return None

    # Serialize modified PATHC
    try:
        modified_bytes = serialize_pathc(pathc)
    except Exception as e:
        logger.error("Failed to serialize PATHC: %s", e)
        return None

    # Write to work_dir/meta/0.pathc
    out_dir = work_dir / "meta"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "0.pathc"
    out_path.write_bytes(modified_bytes)

    logger.info("Texture mod: added %d textures, PATHC %d -> %d bytes",
                added_count, pathc_src.stat().st_size, len(modified_bytes))

    return work_dir
