import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from cdumm.engine.delta_engine import generate_delta, get_changed_byte_ranges, save_delta
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

SCRIPT_TIMEOUT = 60  # seconds

import time

# Thread-local progress callback for import operations.
# Set by ImportWorker before calling import functions.
import threading
_progress_local = threading.local()

def set_import_progress_cb(cb):
    _progress_local.cb = cb

def _emit_progress(pct, msg):
    cb = getattr(_progress_local, 'cb', None)
    if cb:
        cb(pct, msg)


def _validate_modified_pamt(modified_bytes: bytes, rel_path: str) -> None:
    """Parse ``modified_bytes`` as a PAMT to validate it before import.

    v3.1.7.1 hotfix for issues #37 and #38: the previous implementation
    wrote bytes to ``NamedTemporaryFile(suffix=".pamt")`` which
    produced paths like ``tmpXXXXXXXX.pamt``. ``parse_pamt`` then tried
    ``int(pamt_stem)`` on ``tmpXXXXXXXX`` — a ValueError that
    false-positived valid mods as corrupt.

    Fix: write to a TemporaryDirectory using the real basename from
    ``rel_path`` (e.g. ``0.pamt``) so the stem is numeric. Raises
    ``ValueError`` on genuinely corrupt PAMTs; returns None on success.
    """
    import os as _os
    from cdumm.archive.paz_parse import parse_pamt
    basename = _os.path.basename(rel_path.replace("\\", "/"))
    # Fall back to '0.pamt' if rel_path somehow lacks a basename — the
    # '0' stem keeps int() happy.
    if not basename or not basename.lower().endswith(".pamt"):
        basename = "0.pamt"
    with tempfile.TemporaryDirectory(prefix="cdumm_pamt_validate_") as tmpdir:
        tmp_path = _os.path.join(tmpdir, basename)
        with open(tmp_path, "wb") as f:
            f.write(modified_bytes)
        try:
            parse_pamt(tmp_path)
        except ValueError as err:
            raise ValueError(
                f"Mod ships a corrupt {rel_path}: {err}. "
                "The PAMT can't be parsed. Re-download the mod from "
                "its source — the archive is damaged.") from err


def _next_priority(db: Database) -> int:
    """Get the next available priority value for a new mod."""
    cursor = db.connection.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
    return cursor.fetchone()[0]


def _json_mod_display_name(jp_data: dict, fallback: str) -> str:
    """Pick the best human display name for a JSON-patch mod.

    JMM spec nests title under 'modinfo.title'; older/simpler mods put
    'name' or 'title' at the top level. Read all four in priority order
    so Dark Mode Map doesn't get stuck with its zip filename.
    """
    mi = jp_data.get("modinfo")
    mi = mi if isinstance(mi, dict) else {}
    return (mi.get("title")
            or mi.get("name")
            or jp_data.get("title")
            or jp_data.get("name")
            or fallback)


def _json_mod_modinfo(jp_data: dict) -> dict:
    """Build the canonical modinfo dict for a JSON-patch mod.

    Merges modinfo.* with top-level equivalents so the DB row carries
    title/version/author/description regardless of which style the mod
    author used.
    """
    mi = jp_data.get("modinfo")
    mi = mi if isinstance(mi, dict) else {}
    return {
        "name": (mi.get("title") or mi.get("name")
                 or jp_data.get("title") or jp_data.get("name")),
        "version": mi.get("version") or jp_data.get("version"),
        "author": mi.get("author") or jp_data.get("author"),
        "description": (mi.get("description")
                        or jp_data.get("description")),
    }


def _pick_cb_display_name(manifest_id: str | None, archive_stem: str) -> str:
    """Choose a user-facing display name for a Crimson Browser mod.

    CB's manifest.id is a machine identifier (e.g. "my_cool_hud_v1").
    Authors sometimes set it to a lazy placeholder like "mm" or "test"
    and forget to change it, then the Nexus-parsed archive filename
    (e.g. "Witcher HUD-1432-1-...") carries the real display name.

    Heuristic: trust manifest.id only when it looks intentional:
      * contains a space, OR
      * is ≥ 5 chars, OR
      * has mixed case.
    Otherwise fall back to prettify_mod_name(archive_stem).

    Empty archive_stem defeats the fallback; keep id in that case
    (better something than nothing).
    """
    mid = (manifest_id or "").strip()
    stem = (archive_stem or "").strip()

    def _looks_intentional(s: str) -> bool:
        if not s:
            return False
        if " " in s:
            return True
        if len(s) >= 5:
            return True
        if s != s.lower() and s != s.upper():
            return True   # mixed case
        return False

    if _looks_intentional(mid):
        return mid
    if stem:
        pretty = prettify_mod_name(stem)
        return pretty or mid or stem
    return mid


def prettify_mod_name(raw: str) -> str:
    """Clean up a raw mod name for display.

    - Strip NexusMods IDs and timestamps (e.g. '-934-2-1775958271')
    - Strip version suffixes (e.g. 'v1.2', 'v0.3.1', '1.03.00')
    - Split CamelCase (e.g. 'CDLootMultiplier' -> 'CD Loot Multiplier')
    - Replace underscores, hyphens, dots with spaces
    - Title case
    - Collapse whitespace
    """
    import re
    name = raw.strip()

    # #151 last-line defense: if the caller handed us a path stem
    # coming from one of CDUMM's own mkdtemp() calls, that's always
    # wrong for a user-facing mod name. Butanokaabii reported
    # 'Cdumm Variant 2yfxupya' landing on a mod card when the
    # variant-picker pre-extract at fluent_window.py:2428 leaked to
    # the worker. Return a generic placeholder so the user sees a
    # noticeable "please rename me" label instead of the internal
    # tmp-dir stem. Upstream fix still needed in fluent_window.py;
    # this guards against any future path that routes here.
    _CDUMM_TMP_PREFIXES = (
        "cdumm_variant_", "cdumm_swap_",
        "cdumm_cfg_", "cdumm_preset_",
    )
    _stem_lc = name.lower()
    if any(_stem_lc.startswith(p) for p in _CDUMM_TMP_PREFIXES):
        logger.warning(
            "prettify_mod_name: refusing to surface internal tmp-dir "
            "stem %r — caller should have passed a real archive name",
            raw)
        return "Imported Mod"

    # Strip file extensions
    for ext in ('.zip', '.7z', '.rar', '.json', '.bsdiff'):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]

    # Strip NexusMods suffix: -{digits}-{version}-{10-digit timestamp}
    name = re.sub(r'-\d+-[\w.-]+-\d{10,}$', '', name)

    # Strip embedded version attached to word: 'Flightv2.5' -> 'Flight', 'Mod_v2' -> 'Mod_'
    name = re.sub(r'[_\s.-]*v\d+[\.\d]*(?=[\s_.\-]|$)', '', name, flags=re.IGNORECASE)
    # Strip '.v.N' patterns: 'Ragdoll.v.2' -> 'Ragdoll'
    name = re.sub(r'\.v\.\d+', '', name)

    # Strip trailing version patterns: v1.2, v1.2.3, (1.03.00), _v2
    name = re.sub(r'[\s_.\-]*[(\[]?v?\d+\.\d+[\.\d]*[)\]]?\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[\s_.\-]*\(\d+[\.\d]*\)\s*$', '', name)
    # Strip trailing 'vN.N.N' or 'vN': 'Wing v4' -> 'Wing', 'Rush v0.2.2' -> 'Rush'
    name = re.sub(r'\s+v\d+[\.\d]*\s*$', '', name, flags=re.IGNORECASE)
    # Strip trailing pure numbers: 'MEGA STACKS 999999' -> 'MEGA STACKS'
    name = re.sub(r'\s+\d{3,}\s*$', '', name)
    # Strip trailing single/double digit glued to word: 'Wing4' -> 'Wing', 'ModV2' -> 'Mod'
    name = re.sub(r'(?<=[a-zA-Z])\d+\s*$', '', name)
    # Strip trailing multiplier suffixes: '20x', 'x10', '10x'
    name = re.sub(r'\s+\d+x\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+x\d+\s*$', '', name, flags=re.IGNORECASE)

    # Strip middle version-like segments: '1.03.00' in 'glider-stamina-1.03.00-100pct'
    name = re.sub(r'[\s_-]+\d+\.\d+\.\d+[\s_-]*', ' ', name)
    # Strip trailing version segment: 'Trimmer 1.03.00' -> 'Trimmer'
    name = re.sub(r'\s+\d+\.\d+[\.\d]*\s*$', '', name)

    # Split CamelCase: 'CDLootMultiplier' -> 'CD Loot Multiplier'
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)

    # Replace separators with spaces
    name = name.replace('_', ' ').replace('-', ' ')
    # Replace dots with spaces only if not between digits
    name = re.sub(r'\.(?!\d)', ' ', name)
    name = re.sub(r'(?<!\d)\.', ' ', name)

    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()

    # Title case everything. Only keep 2-3 letter acronyms uppercase (CD, UI, QTE, NPC, LOD)
    _ACRONYMS = {'cd', 'ui', 'qte', 'npc', 'lod', 'hud', 'dds', 'asi', 'dll', 'fps', 'fov', 'dps'}
    words = []
    for word in name.split():
        wl = word.lower()
        if wl in _ACRONYMS:
            words.append(word.upper())
        elif wl in ('x2', 'x3', 'x5', 'x10', 'x20', 'x50', 'x100'):
            words.append(wl)
        else:
            words.append(word.capitalize())

    return ' '.join(words)


class ModImportResult:
    """Result of importing a mod."""

    def __init__(self, name: str, mod_type: str = "paz") -> None:
        self.name = prettify_mod_name(name)
        self.mod_type = mod_type
        self.changed_files: list[dict] = []  # [{file_path, delta_path, byte_start, byte_end}]
        self.error: str | None = None
        # ``info`` is for non-fatal diagnostic text — surfaced to the
        # user as a yellow InfoBar instead of a red error toast.
        # Used by importers that successfully recognize a mod but want
        # to flag a partial-apply state (e.g., Format 3 with mixed
        # supported / skipped intents). UI worker treats only ``error``
        # as fatal; ``info`` is shown alongside the success path.
        self.info: str | None = None
        self.health_issues: list = []  # list[HealthIssue] from mod_health_check
        self.mod_id: int | None = None
        self.asi_staged: list[str] = []  # ASI file paths staged for GUI-side install


def install_companion_asis(extract_dir: Path, asi_mgr) -> list:
    """Install any .asi files found anywhere under `extract_dir`.

    Used by the variant-pack import path (which extracts the zip
    itself and never goes through `import_from_zip`). Without this,
    mixed zips that ship `.asi` alongside JSON variants would import
    the JSONs but leave the ASI uninstalled.

    Bug from ZapZockt 2026-04-26 (GitHub #49, Character Creator v4.9):
    the zip ships CharacterCreator.asi + multiple FemaleAnimations.json
    files. The picker handled the JSONs; the ASI was silently dropped.

    Stages only the `.asi` plus its same-stem companion `.ini`
    (e.g. `Foo.asi` + `Foo.ini`) into a curated subdir before calling
    `asi_mgr.install`. Passing the raw extract_dir would let
    AsiManager rglob and sweep every `.ini` in the tree, including
    JSON-variant config .ini files that don't belong in bin64.

    Returns whatever `asi_mgr.install` returns (list of installed
    plugin identifiers), or `[]` if no ASIs were present.
    """
    if not extract_dir or not Path(extract_dir).is_dir():
        return []
    extract_dir = Path(extract_dir)
    asi_files = [p for p in extract_dir.rglob("*.asi") if p.is_file()]
    if not asi_files:
        return []
    try:
        import tempfile as _tmp
        import shutil as _sh
        staging = Path(_tmp.mkdtemp(prefix="cdumm_companion_asi_"))
        try:
            for asi in asi_files:
                _sh.copy2(asi, staging / asi.name)
                # Companion .ini: same stem in same directory.
                companion = asi.with_suffix(".ini")
                if companion.is_file():
                    _sh.copy2(companion, staging / companion.name)
            return asi_mgr.install(staging) or []
        finally:
            _sh.rmtree(staging, ignore_errors=True)
    except Exception as e:
        logger.error("install_companion_asis failed for %s: %s",
                     extract_dir, e, exc_info=True)
        return []


def detect_format(path: Path) -> str:
    """Detect import format: 'zip', '7z', 'folder', 'script', 'json_patch', 'bsdiff', or 'unknown'."""
    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
    if suffix == ".7z":
        return "7z"
    if suffix in (".bat", ".py"):
        return "script"
    if suffix == ".json":
        if detect_json_patch(path) is not None:
            return "json_patch"
        # NattKh's Format 3 (field-names + intents) lives in a .json
        # but doesn't have a patches[] array, so the standard detector
        # rejects it. We return a dedicated format string so the
        # dispatch can emit a "coming in v3.3" message instead of the
        # generic 'unsupported file format'.
        from cdumm.engine.json_patch_handler import is_natt_format_3
        if is_natt_format_3(path):
            return "natt_format_3"
    if suffix in (".bsdiff", ".xdelta"):
        return "bsdiff"
    # Check if it's a zip without extension
    if path.is_file():
        try:
            with zipfile.ZipFile(path) as _:
                return "zip"
        except zipfile.BadZipFile:
            pass
    # RAR archives — extracted via 7-Zip
    if suffix == ".rar":
        return "rar"
    return "unknown"


import json
import re

from cdumm.engine.crimson_browser_handler import detect_crimson_browser, convert_to_paz_mod
from cdumm.engine.json_patch_handler import detect_json_patch, convert_json_patch_to_paz, import_json_fast
from cdumm.engine.texture_mod_handler import detect_texture_mod, convert_texture_mod

# Pattern for valid game file paths: NNNN/N.paz, NNNN/N.pamt, meta/0.papgt, meta/0.pathc
_GAME_FILE_RE = re.compile(r'^(\d{4}/\d+\.(?:paz|pamt)|meta/\d+\.(?:papgt|pathc))$')


def _verify_and_fix_pamt_crc(pamt_bytes: bytes, rel_path: str) -> bytes:
    """Verify PAMT CRC and fix it if wrong.

    PAMT header: first 4 bytes = hashlittle(data[12:], 0xC5EDE).
    If the stored hash doesn't match, recompute and return fixed bytes.
    """
    import struct
    from cdumm.archive.hashlittle import compute_pamt_hash
    stored_hash = struct.unpack_from("<I", pamt_bytes, 0)[0]
    actual_hash = compute_pamt_hash(pamt_bytes)
    if stored_hash != actual_hash:
        logger.info("Auto-fixed PAMT CRC for %s (stored=%08X, actual=%08X)",
                     rel_path, stored_hash, actual_hash)
        fixed = bytearray(pamt_bytes)
        struct.pack_into("<I", fixed, 0, actual_hash)
        return bytes(fixed)
    return pamt_bytes


def _read_modinfo(extracted_dir: Path) -> dict | None:
    """Read modinfo.json from extracted mod directory if present.

    Searches the root and one level deep (for nested zips).
    Returns dict with keys: name, version, author, description (all optional).
    """
    from cdumm.engine.json_repair import load_json_tolerant

    for candidate in [extracted_dir / "modinfo.json",
                      *extracted_dir.glob("*/modinfo.json")]:
        if candidate.exists():
            try:
                data = load_json_tolerant(candidate)
                if isinstance(data, dict):
                    logger.info("Found modinfo.json: %s", {k: data.get(k) for k in ("name", "version", "author")})
                    return data
            except Exception as e:
                logger.warning("Failed to parse modinfo.json: %s", e)
    return None


def _try_convert_crimson_browser(
    extracted_dir: Path, game_dir: Path, work_dir: Path
) -> Path | None:
    """If extracted_dir is a Crimson Browser mod, convert to standard PAZ format.

    Returns the converted directory (inside work_dir), or None if not CB format.
    """
    manifest = detect_crimson_browser(extracted_dir)
    if manifest is None:
        return None

    mod_id = manifest.get("id", "unknown")
    logger.info("Detected Crimson Browser mod: %s", mod_id)
    converted = convert_to_paz_mod(manifest, game_dir, work_dir)
    if converted:
        logger.info("CB mod converted to standard PAZ format in %s", converted)
    else:
        logger.error("CB mod conversion failed for %s", mod_id)
    return converted


def _register_xml_patches(
    extracted_dir: Path, mod_id: int, mod_name: str,
    db: Database, deltas_dir: Path,
) -> list[Path]:
    """Scan extracted mod content for XML XPath patch / merge files.

    For each detected patch / merge file:
      1. Copy it into ``deltas_dir`` with a unique name.
      2. Derive the target game-file path from the file's position within
         ``extracted_dir`` (JMM convention: strip ``.patch`` / ``.merge``
         suffix, keep the rest of the relative path).
      3. Insert a ``mod_deltas`` row with ``kind=xml_patch|xml_merge``,
         ``delta_path`` pointing to the copy, ``file_path`` = target.

    Returns the list of source paths we claimed, so the caller can ignore
    them during regular import (prevents the CB / loose-file passes from
    re-processing patch bodies as full-file replacements).
    """
    # Detector chain: try XML first, then CSS, then HTML. Each pair
    # is (detect_fn, derive_target_fn). The first detector that
    # returns a non-None kind claims the file.
    from cdumm.engine.xml_patch_handler import (
        detect_patch_file as detect_xml,
        derive_target_from_patch_path as derive_xml,
    )
    from cdumm.engine.css_patch_handler import (
        detect_patch_file as detect_css,
        derive_target_from_patch_path as derive_css,
    )
    from cdumm.engine.html_patch_handler import (
        detect_patch_file as detect_html,
        derive_target_from_patch_path as derive_html,
    )
    claimed: list[Path] = []
    valid_kinds = {
        "xml_patch", "xml_merge", "css_patch", "css_merge",
        "html_patch", "html_merge",
    }
    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue
        kind = None
        derive_fn = None
        for det, drv in ((detect_xml, derive_xml),
                         (detect_css, derive_css),
                         (detect_html, derive_html)):
            kind = det(f)
            if kind is not None:
                derive_fn = drv
                break
        if kind not in valid_kinds:
            continue
        target = derive_fn(f, extracted_dir)
        if not target:
            logger.warning("partial_patch: could not derive target for %s", f)
            continue

        # Copy the patch file into deltas_dir. Unique filename: mod_id + hash
        # of the target path so two mods targeting the same file don't clash.
        import hashlib
        tag = hashlib.md5(target.encode("utf-8")).hexdigest()[:8]
        dest_name = f"{mod_id}_{kind}_{tag}_{f.name}"
        dest = deltas_dir / dest_name
        try:
            shutil.copy2(f, dest)
        except Exception as e:
            logger.error("xml_patch: copy failed (%s → %s): %s", f, dest, e)
            continue

        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, is_new, kind) "
            "VALUES (?, ?, ?, 0, 0, 0, ?)",
            (mod_id, target, str(dest), kind),
        )
        claimed.append(f)
        logger.info("xml_patch: registered %s → target=%s (kind=%s)",
                    f.name, target, kind)
    if claimed:
        db.connection.commit()
    return claimed


def _detect_xml_replacements(extracted_dir: Path) -> list[dict]:
    """Detect OG_<name>__<suffix>.xml replacement files.

    Returns list of dicts with keys: source_path, target_name.
    The OG_ naming convention means: replace <name>.xml with this file's content.
    """
    results = []
    for f in extracted_dir.rglob("*.xml"):
        stem = f.stem  # e.g. "OG_inventory__mymod"
        # Case-insensitive prefix check — Linux-authored mods may
        # ship `og_foo__bar.xml`. Windows is case-insensitive, so
        # accept either.
        if len(stem) < 3 or stem[:3].upper() != "OG_":
            continue
        # Split on __ (double underscore) to separate target name from suffix
        rest = stem[3:]  # remove "OG_" prefix
        parts = rest.rsplit("__", 1)
        if len(parts) < 2:
            logger.warning("OG_ XML file missing __suffix: %s", f.name)
            continue
        target_name = parts[0] + ".xml"
        results.append({"source_path": f, "target_name": target_name})
        logger.info("Detected OG_ XML replacement: %s -> %s", f.name, target_name)
    return results


def _detect_plain_xml_replacements(extracted_dir: Path) -> list[dict]:
    """Detect plain `.xml` files (no `OG_` prefix, no Crimson Browser
    manifest) that may be full-file replacements at vanilla basenames.

    Some mod authors ship gamepad / UI XML mods as raw XML files in a
    folder with a `modinfo.json`, relying on the basename matching a
    vanilla PAMT entry. The caller is responsible for verifying each
    `target_name` actually exists in the game's PAMT before
    registering, so non-matching XML files (readmes, UI snippets that
    don't replace any vanilla file) get filtered out.

    Returns list of `{source_path, target_name}` dicts.
    """
    results = []
    for f in extracted_dir.rglob("*.xml"):
        stem = f.stem
        # OG_-prefixed files are handled by `_detect_xml_replacements`,
        # don't double-process them here.
        if len(stem) >= 3 and stem[:3].upper() == "OG_":
            continue
        # Skip XML files inside numbered PAZ dirs (those are PAZ mod
        # content, not loose replacements at the top level).
        if any(p.isdigit() and len(p) == 4 for p in f.parts):
            continue
        results.append({"source_path": f, "target_name": f.name})
    return results


def _import_og_xml_as_mod(
    og_xml: list[dict],
    game_dir: Path,
    db,
    deltas_dir: Path,
    mod_name: str,
    existing_mod_id: int | None = None,
    modinfo: dict | None = None,
) -> ModImportResult | None:
    """Convert a list of OG_ XML replacements into a real PAZ-style
    mod with a row in `mods` and one row per resolved entry in
    `mod_deltas`. Returns the populated result on success, or None
    when none of the OG_ targets resolve against the game's PAMT
    (caller falls through to other detectors).
    """
    from cdumm.engine.json_patch_handler import _find_pamt_entry
    from cdumm.engine.crimson_browser_handler import fix_xml_format

    resolved: list[tuple[dict, "object"]] = []
    skipped: list[str] = []
    for og in og_xml:
        # Reject obviously-empty OG_ files. A 0-byte (or near-empty)
        # XML file would land as a 3-byte BOM-only "XML" after the
        # CRLF/BOM fixup and brick the target file with no diagnostic.
        try:
            size = og["source_path"].stat().st_size
        except OSError:
            size = 0
        if size < 16:
            logger.warning(
                "OG_ XML file too small (%d bytes), skipping: %s",
                size, og["source_path"].name)
            skipped.append(f"{og['source_path'].name} (empty)")
            continue
        entry = _find_pamt_entry(og["target_name"], game_dir)
        if entry is None:
            logger.warning(
                "OG_ XML target not found in game: %s", og["target_name"])
            skipped.append(f"{og['source_path'].name} (no target)")
            continue
        resolved.append((og, entry))

    if not resolved:
        return None

    # Pull author metadata from modinfo if present, else fall back
    author = (modinfo or {}).get("author", "") if modinfo else ""
    version = (modinfo or {}).get("version", "1.0") if modinfo else "1.0"
    description = (modinfo or {}).get("description", "") if modinfo else ""
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]
    conflict_mode = "normal"
    if modinfo:
        cm = modinfo.get("conflict_mode", "normal")
        if isinstance(cm, str) and cm.strip().lower() in ("normal", "override"):
            conflict_mode = cm.strip().lower()
    target_language = (modinfo or {}).get("target_language") if modinfo else None

    # Stamp the mod with the current game version so the
    # outdated-mod sweep (`bug_report.py:158`, `version_detector.py`)
    # can flag this mod after the next game patch. Other importers
    # all do this; without it, OG_ mods get silently excluded.
    try:
        from cdumm.engine.version_detector import detect_game_version
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        game_ver_hash = None

    # Trust the modinfo display name verbatim; only auto-prettify the
    # zip stem fallback. Otherwise "OG Compass" becomes "O G Compass".
    display_name = (
        modinfo["name"]
        if modinfo and modinfo.get("name")
        else prettify_mod_name(mod_name)
    )

    # Insert the mod row (or reuse on update)
    if existing_mod_id is not None:
        mod_id = existing_mod_id
        db.connection.execute(
            "UPDATE mods SET name=?, author=?, version=?, description=?, "
            "conflict_mode=?, target_language=?, game_version_hash=? "
            "WHERE id=?",
            (display_name, author, version, description,
             conflict_mode, target_language, game_ver_hash, mod_id))
        # Wipe stale deltas for this mod. Delete the rows now (commit
        # at the end), but defer the rmtree until after the new deltas
        # are written + committed — otherwise a mid-loop exception
        # rolls back the DB but leaves the on-disk files gone.
        db.connection.execute(
            "DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        _old_delta_dir_to_clean = deltas_dir / str(mod_id)
    else:
        priority = _next_priority(db)
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, "
            "description, conflict_mode, target_language, game_version_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (display_name, "paz", priority, author, version,
             description, conflict_mode, target_language, game_ver_hash))
        mod_id = cursor.lastrowid
        _old_delta_dir_to_clean = None

    mod_delta_dir = deltas_dir / str(mod_id)
    mod_delta_dir.mkdir(parents=True, exist_ok=True)

    result = ModImportResult(mod_name)
    result.mod_id = mod_id

    # When re-importing an existing mod, write each new delta to a
    # `.entr.new` sibling first, then atomically rename to `.entr`
    # only after commit succeeds. Without this, a mid-loop write
    # failure rolls back the DB but leaves overwritten `.entr` files —
    # the restored old DB rows then reference filenames whose contents
    # are from the failed new import (silent corruption).
    pending_renames: list[tuple[Path, Path]] = []  # (final, .new)
    try:
        for og, entry in resolved:
            xml_bytes = og["source_path"].read_bytes()
            try:
                xml_bytes = fix_xml_format(xml_bytes)
            except Exception:
                pass
            # Per-mod subdir prevents cross-mod silent overwrite. Per-target
            # filename keeps two OG_ files in the SAME mod from clobbering
            # each other (their targets differ, so safe_names differ).
            safe_name = og["target_name"].replace("/", "_") + ".entr"
            final_delta_path = mod_delta_dir / safe_name
            if existing_mod_id is not None and final_delta_path.exists():
                staging_path = final_delta_path.with_suffix(".entr.new")
                staging_path.write_bytes(xml_bytes)
                pending_renames.append((final_delta_path, staging_path))
            else:
                final_delta_path.write_bytes(xml_bytes)

            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                "byte_start, byte_end, entry_path) VALUES (?, ?, ?, ?, ?, ?)",
                (mod_id, entry.paz_file, str(final_delta_path),
                 entry.offset, entry.offset + entry.comp_size, entry.path))
            result.changed_files.append({
                "file_path": entry.paz_file,
                "entry_path": entry.path,
                "delta_path": str(final_delta_path),
            })
            logger.info(
                "OG_ XML replacement registered: %s -> %s (mod_id=%d)",
                og["source_path"].name, og["target_name"], mod_id)
        db.connection.commit()
    except Exception:
        db.connection.rollback()
        # Clean up any `.entr.new` staging files we wrote — leave the
        # old `.entr` files intact for the restored DB rows.
        for _, staging_path in pending_renames:
            try:
                staging_path.unlink()
            except OSError:
                pass
        raise

    # Commit succeeded — atomically swap each `.entr.new` into place.
    # If any swap fails, surface the partial state on result.info so
    # the user knows to re-import. Without this, the apply pipeline
    # would silently use stale content for the unrenamed entries.
    import os as _os
    rename_failures: list[str] = []
    for final_path, staging_path in pending_renames:
        try:
            _os.replace(str(staging_path), str(final_path))
        except OSError as e:
            rename_failures.append(staging_path.name)
            logger.warning(
                "Failed to swap %s into place: %s",
                staging_path.name, e)
            try:
                staging_path.unlink()
            except OSError:
                pass
    if rename_failures:
        names = ", ".join(rename_failures[:3])
        if len(rename_failures) > 3:
            names += f", +{len(rename_failures) - 3} more"
        partial_warn = (
            f"WARNING: {len(rename_failures)} OG_ XML file(s) could "
            f"not be installed atomically: {names}. The mod is "
            f"imported but apply may use stale content for those "
            f"entries — re-import to fix."
        )
        result.info = (
            f"{result.info}\n{partial_warn}" if result.info else partial_warn)

    # Commit succeeded — now selectively remove stale `.entr` files
    # left over from a previous import of this same mod_id (existing
    # mod path). We only delete `.entr` files NOT in the new delta
    # set, preserving any sibling source archives (source.json etc).
    if _old_delta_dir_to_clean is not None and _old_delta_dir_to_clean.exists():
        kept = {Path(cf["delta_path"]).name for cf in result.changed_files}
        for f in _old_delta_dir_to_clean.glob("*.entr"):
            if f.is_file() and f.name not in kept:
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning(
                        "Failed to remove stale OG_ delta %s: %s", f, e)

    if skipped:
        # Surface skip reasons so the user knows why the imported mod
        # has fewer changes than the OG_ files in the ZIP.
        names = ", ".join(skipped[:3])
        if len(skipped) > 3:
            names += f", +{len(skipped) - 3} more"
        result.info = (
            f"Imported {len(resolved)} OG_ XML replacement(s); "
            f"{len(skipped)} skipped: {names}"
        )

    return result


def _try_paz_entry_import(
    mod_paz_path: Path, vanilla_paz_path: Path, rel_path: str,
    extracted_dir: Path, game_dir: Path, mod_id: int, db,
    deltas_dir: Path, result,
) -> bool:
    """Decompose a modified PAZ file into ENTR deltas per PAMT entry.

    Instead of storing a FULL_COPY or SPRS delta of the entire PAZ,
    this compares each PAMT entry's decompressed content against vanilla
    and stores only the changed entries as ENTR deltas. This way two mods
    modifying different entries in the same PAZ compose correctly.

    Returns True if successful, False to fall back to byte-level deltas.
    """
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.archive.paz_crypto import lz4_decompress, decrypt
    from cdumm.engine.delta_engine import save_entry_delta
    from cdumm.engine.json_patch_handler import _extract_from_paz
    import sys as _sys

    _entr_start = time.perf_counter()
    _entr_timings = {
        "parse_pamts": 0.0,
        "raw_bytes_skip": 0.0,    # seek+read+compare raw bytes (cheap path)
        "extract_compare": 0.0,   # _extract_from_paz (LZ4) + content compare
        "encryption_detect": 0.0, # speculative LZ4 decompress to detect encryption
        "save_delta": 0.0,        # save_entry_delta disk write
        "db_insert": 0.0,         # mod_deltas row inserts
        "other": 0.0,
    }
    _entr_counters = {
        "total_entries": 0,
        "skipped_identical": 0,
        "changed": 0,
        "raw_bytes_matched": 0,
    }

    dir_name = rel_path.split("/")[0]  # e.g. "0008"
    paz_index = int(rel_path.split("/")[1].split(".")[0])  # e.g. 0 from "0.paz"

    # Find PAMTs — mod's PAMT (if shipped) or vanilla PAMT
    vanilla_pamt = game_dir / "CDMods" / "vanilla" / dir_name / "0.pamt"
    if not vanilla_pamt.exists():
        vanilla_pamt = game_dir / dir_name / "0.pamt"
    if not vanilla_pamt.exists():
        logger.debug("No PAMT found for %s, skipping entry-level import", rel_path)
        return False

    mod_pamt = extracted_dir / dir_name / "0.pamt"
    if not mod_pamt.exists():
        # Mod doesn't ship a PAMT — use vanilla PAMT for both
        mod_pamt = vanilla_pamt

    # Reuse the vanilla PAMT LRU cache (shared with health_check). For a
    # batch where 6+ mods touch dir 0009, this saves ~3s per mod after
    # the first cache miss. Mod PAMTs are cached too — within one mod's
    # lifecycle health_check parses it first, then we hit the cache here.
    from cdumm.engine.mod_health_check import _load_vanilla_pamt

    _t = time.perf_counter()
    try:
        _van_paz_dir = str(
            (game_dir / "CDMods" / "vanilla" / dir_name)
            if (game_dir / "CDMods" / "vanilla" / dir_name / "0.pamt").exists()
            else (game_dir / dir_name)
        )
        van_entries = _load_vanilla_pamt(str(vanilla_pamt), _van_paz_dir)

        _mod_paz_dir = (
            str(extracted_dir / dir_name)
            if mod_pamt != vanilla_pamt
            else str(game_dir / dir_name)
        )
        mod_entries = _load_vanilla_pamt(str(mod_pamt), _mod_paz_dir)
    except Exception as e:
        logger.debug("Failed to parse PAMTs for %s: %s", rel_path, e)
        return False
    _entr_timings["parse_pamts"] = time.perf_counter() - _t

    # Filter to entries in this specific PAZ file
    van_by_path = {e.path: e for e in van_entries if e.paz_index == paz_index}
    mod_by_path = {e.path: e for e in mod_entries if e.paz_index == paz_index}

    if not van_by_path or not mod_by_path:
        logger.debug("No entries for PAZ index %d in %s", paz_index, rel_path)
        return False

    def _extract_entry(entry, paz_path):
        """Extract and decompress a single entry from a PAZ file.

        `_extract_from_paz` mutates `entry._encrypted_override` when it
        detects encryption. VanillaPamtEntry (the cache's NamedTuple) is
        immutable, so we convert to a mutable PazEntry dataclass first.
        Only pays the conversion cost for entries that actually need
        decompression (raw-bytes fast-path skips this entirely).
        """
        from cdumm.archive.paz_parse import PazEntry as _PazEntry
        if not isinstance(entry, _PazEntry):
            entry = _PazEntry(
                path=entry.path,
                paz_file=str(paz_path),
                offset=entry.offset,
                comp_size=entry.comp_size,
                orig_size=entry.orig_size,
                flags=entry.flags,
                paz_index=entry.paz_index,
            )
        return _extract_from_paz(entry, paz_path=str(paz_path))

    changed = 0
    paz_file_path = f"{dir_name}/{paz_index}.paz"

    # ── Fast-path: bulk raw-bytes filter via Rust ─────────────────
    # For entries with identical offset+comp_size in mod and vanilla,
    # read the raw bytes from both PAZs and compare. Native code keeps
    # this near disk-I/O speed even for 100K+ entries. Entries whose
    # offset or size differ in mod vs vanilla are marked "changed"
    # unconditionally (the raw-bytes compare can't apply).
    _raw_candidates: list[tuple[str, object, object]] = []
    _raw_changed: list[tuple[str, object, object]] = []

    for entry_path, mod_entry in mod_by_path.items():
        van_entry = van_by_path.get(entry_path)
        if van_entry is None:
            continue  # New entry — handled separately
        _entr_counters["total_entries"] += 1
        if (mod_entry.offset == van_entry.offset
                and mod_entry.comp_size == van_entry.comp_size):
            _raw_candidates.append((entry_path, mod_entry, van_entry))
        else:
            _raw_changed.append((entry_path, mod_entry, van_entry))

    if _raw_candidates:
        try:
            from cdumm_native import filter_changed_entries as _native_filter
        except ImportError:
            _native_filter = None

        _t = time.perf_counter()
        if _native_filter is not None:
            pairs = [
                (e[1].paz_index, e[1].offset, e[1].comp_size,
                 e[2].offset, e[2].comp_size)
                for e in _raw_candidates
            ]
            changed_idxs = _native_filter(
                str(mod_paz_path), str(vanilla_paz_path), pairs)
            changed_set = set(changed_idxs)
            for i, tup in enumerate(_raw_candidates):
                if i in changed_set:
                    _raw_changed.append(tup)
                else:
                    _entr_counters["raw_bytes_matched"] += 1
                    _entr_counters["skipped_identical"] += 1
        else:
            # Python fallback — original per-entry loop.
            for entry_path, mod_entry, van_entry in _raw_candidates:
                try:
                    with open(mod_paz_path, "rb") as f:
                        f.seek(mod_entry.offset)
                        mod_raw = f.read(mod_entry.comp_size)
                    with open(vanilla_paz_path, "rb") as f:
                        f.seek(van_entry.offset)
                        van_raw = f.read(van_entry.comp_size)
                    if mod_raw == van_raw:
                        _entr_counters["raw_bytes_matched"] += 1
                        _entr_counters["skipped_identical"] += 1
                    else:
                        _raw_changed.append((entry_path, mod_entry, van_entry))
                except Exception as e:
                    logger.warning("Raw-bytes compare failed for %s: %s",
                                   entry_path, e)
        _entr_timings["raw_bytes_skip"] += time.perf_counter() - _t

    # Now iterate only the entries that genuinely differ
    for entry_path, mod_entry, van_entry in _raw_changed:
        try:
            # Entry differs — decompress both and store mod's content
            _t = time.perf_counter()
            van_content = _extract_entry(van_entry, vanilla_paz_path)
            mod_content = _extract_entry(mod_entry, mod_paz_path)
            _same_decomp = van_content == mod_content
            _entr_timings["extract_compare"] += time.perf_counter() - _t

            if _same_decomp:
                _entr_counters["skipped_identical"] += 1
                continue  # Decompressed content is the same

            # Detect encryption: try decompressing the vanilla entry.
            # If decompress fails, the entry is encrypted.
            _t = time.perf_counter()
            encrypted = van_entry.encrypted
            if not encrypted and van_entry.compressed and van_entry.compression_type == 2:
                try:
                    with open(vanilla_paz_path, "rb") as f:
                        f.seek(van_entry.offset)
                        raw = f.read(van_entry.comp_size)
                    lz4_decompress(raw, van_entry.orig_size)
                except Exception:
                    encrypted = True
            _entr_timings["encryption_detect"] += time.perf_counter() - _t

            metadata = {
                "pamt_dir": dir_name,
                "entry_path": van_entry.path,
                "paz_index": van_entry.paz_index,
                "compression_type": van_entry.compression_type,
                "flags": van_entry.flags,
                "vanilla_offset": van_entry.offset,
                "vanilla_comp_size": van_entry.comp_size,
                "vanilla_orig_size": van_entry.orig_size,
                "encrypted": encrypted,
            }

            _t = time.perf_counter()
            safe_name = van_entry.path.replace("/", "_") + ".entr"
            delta_path = deltas_dir / str(mod_id) / safe_name
            save_entry_delta(mod_content, metadata, delta_path)
            _entr_timings["save_delta"] += time.perf_counter() - _t

            _t = time.perf_counter()
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                "byte_start, byte_end, entry_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mod_id, paz_file_path, str(delta_path),
                 van_entry.offset, van_entry.offset + van_entry.comp_size,
                 van_entry.path))
            _entr_timings["db_insert"] += time.perf_counter() - _t

            result.changed_files.append({
                "file_path": paz_file_path,
                "entry_path": van_entry.path,
                "delta_path": str(delta_path),
            })
            changed += 1
            _entr_counters["changed"] += 1

        except Exception as e:
            logger.warning("Entry comparison failed for %s: %s", entry_path, e)
            continue

    if changed == 0:
        logger.warning("No changed entries in %s (%d mod entries vs %d vanilla entries). "
                        "Mod content may match current game version or decomposition failed.",
                        rel_path, len(mod_by_path), len(van_by_path))
        return False

    _t = time.perf_counter()
    db.connection.commit()
    _entr_timings["db_insert"] += time.perf_counter() - _t

    _entr_total = time.perf_counter() - _entr_start
    _accounted = sum(_entr_timings.values())
    _entr_timings["other"] = max(0.0, _entr_total - _accounted)

    if _entr_total >= 1.0:
        _hot = sorted(_entr_timings.items(), key=lambda kv: kv[1], reverse=True)
        _parts = " ".join(
            f"{n}={int(dt * 1000)}ms" for n, dt in _hot if dt >= 0.05
        )
        print(
            f"[ENTR-TIMING] {rel_path}: total={int(_entr_total * 1000)}ms "
            f"entries={_entr_counters['total_entries']} "
            f"changed={_entr_counters['changed']} "
            f"raw_same={_entr_counters['raw_bytes_matched']} "
            f"{_parts}",
            file=_sys.stderr,
        )

    logger.info("Entry-level PAZ import: %s — %d/%d entries changed",
                rel_path, changed, len(mod_by_path))
    return True


def _find_loose_file_candidates(path: Path, max_depth: int = 5) -> list[dict]:
    """Recursively search for all loose-file mod roots (files/NNNN/ pattern).

    Returns a list of manifest dicts, one per found variant.
    """
    results: list[dict] = []
    seen_bases: set[str] = set()

    def _check_candidate(candidate: Path) -> dict | None:
        base_key = str(candidate)
        if base_key in seen_bases:
            return None
        # Pattern 1: mod.json + files/
        mod_json = candidate / "mod.json"
        files_dir = candidate / "files"
        if mod_json.exists() and files_dir.exists():
            try:
                with open(mod_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "modinfo" in data:
                    modinfo = data["modinfo"]
                    seen_bases.add(base_key)
                    return {
                        "format": "loose_file_mod",
                        "id": modinfo.get("title", candidate.name),
                        "files_dir": "files",
                        "_manifest_path": mod_json,
                        "_base_dir": candidate,
                        "_modinfo": modinfo,
                    }
            except Exception:
                pass
        # Pattern 2: mod.json + game files at root (no files/ directory)
        # Game file paths like gamedata/, sequencer/, ui/ sit next to mod.json.
        # These get resolved to PAZ directories via PAMT lookup.
        # Skip if the directory contains numbered game dirs with PAZ/PAMT files
        # (that's a standalone PAZ mod, not a loose-file mod).
        if mod_json.exists() and not files_dir.exists():
            try:
                with open(mod_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "modinfo" in data:
                    # Check if this is a standalone PAZ mod (has NNNN/0.paz).
                    # Authors may nest the numbered dirs under game_files/
                    # (JMM 9.9.2 layout). Treat both placements identically.
                    paz_search_roots = [candidate]
                    gf_dir = candidate / "game_files"
                    if gf_dir.is_dir():
                        paz_search_roots.append(gf_dir)
                    is_standalone_paz = any(
                        d.is_dir() and d.name.isdigit() and len(d.name) == 4
                        and (d / "0.paz").exists()
                        for root in paz_search_roots
                        for d in root.iterdir()
                    )
                    if is_standalone_paz:
                        pass  # Let _match_game_files handle it as a PAZ mod
                    else:
                        # Check if there are actual game files alongside mod.json
                        has_game_files = any(
                            f.is_file() and f.name != "mod.json"
                            for f in candidate.iterdir()
                        ) or any(
                            d.is_dir() and d.name != "files"
                            for d in candidate.iterdir()
                        )
                    if not is_standalone_paz and has_game_files:
                        modinfo = data["modinfo"]
                        seen_bases.add(base_key)
                        # Use "." as files_dir so convert_to_paz_mod scans
                        # the candidate root for game files
                        return {
                            "format": "loose_file_mod",
                            "id": modinfo.get("title", candidate.name),
                            "files_dir": ".",
                            "_manifest_path": mod_json,
                            "_base_dir": candidate,
                            "_modinfo": modinfo,
                        }
            except Exception:
                pass
        # Pattern 3: bare files/NNNN/
        if files_dir.exists() and files_dir.is_dir():
            try:
                has_numbered = any(
                    d.is_dir() and d.name.isdigit() and len(d.name) == 4
                    for d in files_dir.iterdir()
                )
            except OSError:
                has_numbered = False
            if has_numbered:
                seen_bases.add(base_key)
                return {
                    "format": "loose_file_mod",
                    "id": candidate.name,
                    "files_dir": "files",
                    "_manifest_path": None,
                    "_base_dir": candidate,
                    "_modinfo": {"title": candidate.name},
                }
        # Pattern 4: bare NNNN/ directly in the candidate (no files/ wrapper)
        # Mods that ship e.g. 0010/actionchart/xml/file.xml at the root.
        # Skip if the numbered directory contains 0.paz (that's a standalone
        # PAZ mod, handled by _match_game_files).
        try:
            has_direct_numbered = any(
                d.is_dir() and d.name.isdigit() and len(d.name) == 4
                and not (d / "0.paz").exists()
                and any(f.is_file() for f in d.rglob("*"))
                for d in candidate.iterdir()
            )
        except OSError:
            has_direct_numbered = False
        if has_direct_numbered:
            seen_bases.add(base_key)
            return {
                "format": "loose_file_mod",
                "id": candidate.name,
                "files_dir": ".",
                "_manifest_path": None,
                "_base_dir": candidate,
                "_modinfo": {"title": candidate.name},
            }
        return None

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        hit = _check_candidate(directory)
        if hit:
            results.append(hit)
            return  # don't recurse into a found mod root
        try:
            children = [d for d in directory.iterdir() if d.is_dir()
                        and not d.name.startswith((".", "_"))]
        except OSError:
            return
        for child in children:
            _walk(child, depth + 1)

    _walk(path, 0)
    return results


def find_loose_file_variants(path: Path) -> list[dict]:
    """Public API: find all loose-file mod variants in a directory tree.

    Used by the GUI to detect multi-variant mods and show a picker.
    """
    return _find_loose_file_candidates(path, max_depth=5)


# Maximum allowed ratio between max-intent-count and min-intent-count
# variants in a Format 3 pack. CrimsonWings ships every level with the
# same 365 intents (ratio 1.0). A pack where one variant has 10 intents
# and another has 1000 is almost certainly not the same mod's variants.
_F3_VARIANT_INTENT_RATIO_MAX = 2.0
# Minimum length of the shared stem prefix. Two unrelated F3 mods often
# share single-letter prefixes by accident; require at least 3 chars.
_F3_VARIANT_MIN_COMMON_PREFIX = 3


def _f3_variant_distinguishing_id(filename: str, common_prefix: str) -> str:
    """Strip the shared prefix and known F3 suffixes from a filename
    to surface the per-variant distinguishing piece.
    e.g. 'CrimsonWings_10pct.field.json' with prefix 'CrimsonWings_'
         returns '10pct'.
    """
    stem = filename
    for suffix in (".field.json", ".json"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem.startswith(common_prefix):
        stem = stem[len(common_prefix):]
    # Strip any trailing/leading punctuation that the prefix split
    # left behind (e.g. underscore separator).
    return stem.strip("._- ") or filename


def _scan_format3_variant_pack(path: Path) -> list[tuple[Path, str]] | None:
    """Pure-detection variant of `find_format3_variants` — no side
    effects.

    Returns a list of (json_path, variant_id) when the path matches
    the variant-pack pattern. Returns None otherwise. Callers that
    only need to ANSWER "is this a variant pack" can use this to
    avoid the materialisation side effect.
    """
    from cdumm.engine.json_patch_handler import is_natt_format_3
    f3_jsons: list[tuple[Path, dict]] = []
    try:
        for p in path.rglob("*.json"):
            if not p.is_file():
                continue
            if "_f3_variants" in p.parts:
                continue
            if not is_natt_format_3(p):
                continue
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            f3_jsons.append((p, data))
    except OSError:
        return None

    if len(f3_jsons) < 2:
        return None
    targets = {data.get("target") for _, data in f3_jsons}
    if len(targets) != 1:
        return None
    names = [p.name for p, _ in f3_jsons]
    prefix = _common_prefix(names)
    if len(prefix) < _F3_VARIANT_MIN_COMMON_PREFIX:
        return None
    counts = [len(data.get("intents") or []) for _, data in f3_jsons]
    if min(counts) <= 0:
        return None
    if max(counts) / min(counts) > _F3_VARIANT_INTENT_RATIO_MAX:
        return None

    return [(src, _f3_variant_distinguishing_id(src.name, prefix))
            for src, _ in f3_jsons]


def find_format3_variants(path: Path) -> list[dict]:
    """Detect a Format 3 variant pack AND materialise each variant
    into its own subdirectory so the existing folder-variant picker
    can consume it.

    A variant pack is 2+ Format 3 JSONs that:
      * share a common stem prefix of at least 3 characters
      * all declare the same `target` table
      * have similar intent counts (max/min ratio <= 2.0)

    Returns:
        [{"id": "10pct", "_base_dir": Path(<path>/_f3_variants/10pct)}]

    Empty list when the conditions are not met.

    SIDE EFFECT: writes into `path/_f3_variants/`. Use only on a temp
    extraction directory, not the user's source folder. Use
    `_scan_format3_variant_pack` for read-only detection.
    """
    import shutil as _shutil
    detected = _scan_format3_variant_pack(path)
    if not detected:
        return []

    materialise_root = path / "_f3_variants"
    try:
        materialise_root.mkdir(exist_ok=True)
    except OSError as e:
        logger.warning("Could not create F3 variant staging dir: %s", e)
        return []

    out: list[dict] = []
    for src_path, variant_id in detected:
        variant_dir = materialise_root / variant_id
        try:
            variant_dir.mkdir(exist_ok=True)
            dst = variant_dir / src_path.name
            if not dst.exists():
                _shutil.copy2(src_path, dst)
        except OSError as e:
            logger.warning("Could not materialise F3 variant %s: %s",
                           variant_id, e)
            continue
        out.append({"id": variant_id, "_base_dir": variant_dir})

    return out


def _common_prefix(strings: list[str]) -> str:
    """Longest common leading substring across all strings."""
    if not strings:
        return ""
    s1 = min(strings)
    s2 = max(strings)
    for i, ch in enumerate(s1):
        if i >= len(s2) or s2[i] != ch:
            return s1[:i]
    return s1


def detect_loose_file_mod(path: Path) -> dict | None:
    """Detect mods that ship loose game files with a mod.json metadata file.

    Format: mod.json (with "modinfo" key) + files/ directory containing
    replacement files at their PAMT paths (e.g., files/0004/sound/.../file.wem).

    Returns a CB-compatible manifest dict for convert_to_paz_mod, or None.
    """
    candidates = _find_loose_file_candidates(path, max_depth=5)
    if len(candidates) == 1:
        logger.info("Detected loose file mod: %s", candidates[0]["id"])
        return candidates[0]
    if len(candidates) > 1:
        # Multiple variants found — caller should use find_loose_file_variants()
        # and show a picker. Return None so the import doesn't silently pick one.
        logger.info("Found %d loose file variants, picker needed", len(candidates))
        return None
    return None


def _match_game_files(
    extracted_dir: Path, game_dir: Path, snapshot: SnapshotManager
) -> list[tuple[str, Path, bool]]:
    """Find files in extracted_dir that match known game file paths.

    Returns list of (relative_posix_path, absolute_extracted_path, is_new).
    is_new=True means the file doesn't exist in vanilla (mod adds it).

    Detects standalone PAZ mods that ship their own directory (e.g., 0036/)
    with completely different content from vanilla. These get assigned a new
    directory number instead of being treated as modifications to vanilla.
    """
    matches: list[tuple[str, Path, bool]] = []

    # First: detect if this is a standalone directory mod
    # (ships 0.paz + 0.pamt in a numbered dir but content is unrelated to vanilla)
    standalone_remap = _detect_standalone_mod(extracted_dir, game_dir, snapshot)

    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue

        parts = f.relative_to(extracted_dir).parts
        matched = False

        # Build candidate paths
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])

            # Skip meta/0.papgt from standalone mods (CDUMM rebuilds it)
            if candidate == "meta/0.papgt" and standalone_remap:
                matched = True
                break

            # Remap standalone mod directories to their assigned number
            if standalone_remap:
                for old_dir, new_dir in standalone_remap.items():
                    if candidate.startswith(old_dir + "/"):
                        candidate = new_dir + candidate[len(old_dir):]
                        matches.append((candidate, f, True))
                        matched = True
                        break
                if matched:
                    break

            # Try exact match against snapshot (existing vanilla files)
            if snapshot.get_file_hash(candidate) is not None:
                matches.append((candidate, f, False))
                matched = True
                break

            # Check if it looks like a game file by pattern
            if _GAME_FILE_RE.match(candidate):
                game_file = game_dir / candidate.replace("/", os.sep)
                is_new = not game_file.exists()
                matches.append((candidate, f, is_new))
                matched = True
                break

        if matched:
            continue

    # If no matches found, check for unnumbered PAZ/PAMT mods
    # (e.g., mod ships "modname/0.paz" + "modname/0.pamt" without a numbered dir)
    if not matches:
        paz_files = list(extracted_dir.rglob("*.paz"))
        pamt_files = list(extracted_dir.rglob("*.pamt"))
        papgt_files = list(extracted_dir.rglob("0.papgt"))

        if paz_files and pamt_files:
            # Group by parent directory — each paz+pamt pair gets its own dir
            dirs_with_mods: dict[Path, tuple[list, list]] = {}
            for pf in paz_files:
                dirs_with_mods.setdefault(pf.parent, ([], []))[0].append(pf)
            for pf in pamt_files:
                dirs_with_mods.setdefault(pf.parent, ([], []))[1].append(pf)

            for mod_dir, (pazs, pamts) in dirs_with_mods.items():
                if pazs and pamts:
                    next_dir = _next_paz_directory(game_dir)
                    logger.info("Unnumbered PAZ mod in %s -> assigning %s",
                                mod_dir.name, next_dir)
                    for f in pazs + pamts:
                        matches.append((f"{next_dir}/{f.name}", f, True))

        elif paz_files and not pamt_files:
            # Some mods only ship PAZ without PAMT — still try to import
            next_dir = _next_paz_directory(game_dir)
            logger.info("PAZ-only mod detected (no PAMT), assigning directory %s", next_dir)
            for f in paz_files:
                matches.append((f"{next_dir}/{f.name}", f, True))

    return matches


def _detect_standalone_mod(
    extracted_dir: Path, game_dir: Path, snapshot: SnapshotManager
) -> dict[str, str] | None:
    """Detect if a mod ships standalone PAZ/PAMT in a numbered directory.

    A standalone mod has its own 0.paz + 0.pamt that are completely different
    from vanilla (different PAMT or PAZ size). These should get their own
    directory number instead of being treated as modifications.

    Returns {old_dir_prefix: new_dir} remap dict, or None if not standalone.
    The old_dir_prefix is relative to extracted_dir (e.g., "FatStacks10x/0036").
    """
    remap: dict[str, str] = {}

    # Search recursively for numbered directories containing 0.paz + 0.pamt
    for d in extracted_dir.rglob("*"):
        if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
            continue
        dir_name = d.name
        mod_pamt = d / "0.pamt"
        mod_paz = d / "0.paz"
        if not mod_pamt.exists() or not mod_paz.exists():
            continue

        # Compare mod's files against vanilla (check both game dir and vanilla backup)
        vanilla_backup_dir = game_dir / "CDMods" / "vanilla"
        game_pamt = game_dir / dir_name / "0.pamt"
        game_paz = game_dir / dir_name / "0.paz"
        backup_pamt = vanilla_backup_dir / dir_name / "0.pamt"
        backup_paz = vanilla_backup_dir / dir_name / "0.paz"

        if not game_pamt.exists():
            continue  # no vanilla dir = truly new, handled elsewhere

        mod_pamt_size = mod_pamt.stat().st_size
        mod_paz_size = mod_paz.stat().st_size

        # Check against vanilla backup first (accurate), then game dir (may be modded)
        vanilla_pamt_size = backup_pamt.stat().st_size if backup_pamt.exists() else game_pamt.stat().st_size
        vanilla_paz_size = backup_paz.stat().st_size if backup_paz.exists() else (game_paz.stat().st_size if game_paz.exists() else 0)

        # A standalone mod has completely different content — different PAMT size
        # indicates entirely different file entries (truly a new directory).
        # Same PAMT size means same file entries, just modified content —
        # this is a regular patch even if PAZ size changed (file appending).
        if mod_pamt_size == vanilla_pamt_size:
            is_standalone = False
            logger.info("Modified vanilla: %s (same PAMT size, treating as patch, "
                         "PAZ %d vs %d)", dir_name, mod_paz_size, vanilla_paz_size)
        elif int(dir_name) < 36:
            # Vanilla directories (0000-0035): always treat as patch, even if
            # PAMT sizes differ (game update may have added/removed entries).
            # ENTR decomposition handles different PAMTs by matching entries
            # by path, so it works regardless of PAMT size differences.
            is_standalone = False
            logger.info("Modified vanilla: %s (PAMT size differs %d vs %d, but "
                         "vanilla dir — treating as patch for ENTR decomposition)",
                         dir_name, mod_pamt_size, vanilla_pamt_size)
        else:
            is_standalone = True
            logger.info("Standalone: %s has different PAMT (mod=%d vs vanilla=%d, "
                         "PAZ %d vs %d)",
                         dir_name, mod_pamt_size, vanilla_pamt_size,
                         mod_paz_size, vanilla_paz_size)

        if is_standalone:
            # Each standalone mod gets its own directory number so multiple
            # mods targeting the same directory can coexist.
            rel_parts = d.relative_to(extracted_dir).parts
            old_prefix = "/".join(rel_parts)
            new_dir = _next_paz_directory(game_dir)
            remap[old_prefix] = new_dir
            logger.info("Standalone mod: remapping %s -> %s", old_prefix, new_dir)

    return remap if remap else None


_assigned_dirs: set[int] = set()  # track dirs assigned in current import batch


def clear_assigned_dirs() -> None:
    """Clear the assigned directory tracker. Call after Apply completes."""
    _assigned_dirs.clear()


def _next_paz_directory(game_dir: Path) -> str:
    """Find the next available PAZ directory number (0036+)."""
    existing = set()
    for d in game_dir.iterdir():
        if d.is_dir() and d.name.isdigit() and len(d.name) == 4:
            existing.add(int(d.name))
    existing |= _assigned_dirs
    # Start from 36 (base game uses 0000-0035)
    for n in range(36, 9999):
        if n not in existing:
            _assigned_dirs.add(n)
            return f"{n:04d}"
    raise RuntimeError("No available PAZ directory numbers (36-9999 all used)")


def import_from_7z(
    archive_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a 7z archive by extracting and treating as folder."""
    mod_name = archive_path.stem
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            import py7zr
            with py7zr.SevenZipFile(archive_path, 'r') as z:
                z.extractall(tmp_path)
        except Exception as e:
            result.error = f"Failed to extract 7z: {e}"
            return result

        # Unpack any nested archives so format detectors see folders.
        _extract_nested_archives(tmp_path)

        # Delegate to import_from_zip's internal logic (same flow)
        return _import_from_extracted(tmp_path, game_dir, db, snapshot, deltas_dir,
                                      mod_name, existing_mod_id)


_NESTED_ARCHIVE_EXTS = (".zip", ".7z", ".rar")
_NESTED_EXTRACT_MAX_DEPTH = 5  # zip-bomb / runaway-recursion guard


def _extract_nested_archives(extracted_dir: Path,
                              _depth: int = 0) -> None:
    """Walk `extracted_dir` and unpack any inner .zip / .7z / .rar
    archives into same-stem sibling directories. Recurses up to
    _NESTED_EXTRACT_MAX_DEPTH levels.

    Behavior:
      * Inner archive `english.zip` becomes directory `english/`,
        and `english.zip` is removed.
      * If `english/` already exists, the unpacked dir is named
        `english_1/`, `english_2/`, etc. (no clobbering).
      * Corrupt archives are skipped with a warning, not raised —
        a single bad inner zip must not abort the whole import.
      * 7z and rar inner archives are unpacked via the same
        py7zr / 7-Zip executable paths the top-level importers
        use.
    """
    if _depth >= _NESTED_EXTRACT_MAX_DEPTH:
        logger.warning("Nested-archive extraction depth limit (%d) reached at %s",
                       _NESTED_EXTRACT_MAX_DEPTH, extracted_dir)
        return

    inner_archives: list[Path] = []
    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() in _NESTED_ARCHIVE_EXTS:
            inner_archives.append(f)

    if not inner_archives:
        return

    logger.info("Nested archives found at depth %d: %d file(s)",
                _depth, len(inner_archives))

    any_extracted = False
    for archive in inner_archives:
        if not archive.exists():
            # A previous iteration may have removed it (shouldn't
            # happen given the gather-first-then-extract loop, but
            # cheap guard against future refactors).
            continue
        target = archive.parent / archive.stem
        if target.exists():
            i = 1
            while (archive.parent / f"{archive.stem}_{i}").exists():
                i += 1
            target = archive.parent / f"{archive.stem}_{i}"
        try:
            target.mkdir(parents=True)
        except OSError as e:
            logger.warning("Could not create nested-extract dir %s: %s", target, e)
            continue

        ext = archive.suffix.lower()
        try:
            if ext == ".zip":
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(target)
            elif ext == ".7z":
                import py7zr
                with py7zr.SevenZipFile(archive, 'r') as z:
                    z.extractall(target)
            elif ext == ".rar":
                seven_z = _find_7z()
                if not seven_z:
                    logger.warning("Skipping nested .rar (no 7-Zip found): %s",
                                   archive.name)
                    target.rmdir()
                    continue
                _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.run(
                    [seven_z, "x", str(archive), f"-o{target}", "-y"],
                    capture_output=True, timeout=120,
                    creationflags=_no_window,
                )
                if proc.returncode != 0:
                    logger.warning("Nested .rar extraction failed for %s",
                                   archive.name)
                    shutil.rmtree(target, ignore_errors=True)
                    continue
        except (zipfile.BadZipFile, Exception) as e:
            logger.warning("Skipping corrupt nested archive %s: %s",
                           archive.name, e)
            shutil.rmtree(target, ignore_errors=True)
            continue

        archive.unlink()
        any_extracted = True

    if any_extracted:
        # Recurse: an inner archive may itself have contained
        # archives (zip-of-zip-of-zip). Bounded by _depth guard.
        _extract_nested_archives(extracted_dir, _depth=_depth + 1)


_FIND_7Z_DEFAULT_PATHS: list[str] = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]

# macOS / Linux 7-Zip install conventions. The modern Homebrew formula
# is ``sevenzip`` (binary name ``7zz``); the legacy ``p7zip`` formula
# (binary ``7z``) is deprecated but still around on older systems.
# Homebrew's ``/opt/homebrew/opt/<formula>/bin`` symlink is created
# whether or not the user has run ``brew link`` — checking it directly
# means CDUMM finds 7-Zip even when ``which 7zz`` returns nothing.
_FIND_7Z_UNIX_PATHS: list[str] = [
    # Apple Silicon Homebrew (modern sevenzip formula)
    "/opt/homebrew/opt/sevenzip/bin/7zz",
    "/opt/homebrew/bin/7zz",
    "/opt/homebrew/bin/7z",
    # Intel macOS Homebrew + most Linux distros
    "/usr/local/opt/sevenzip/bin/7zz",
    "/usr/local/bin/7zz",
    "/usr/local/bin/7z",
    # System-package-manager paths (apt / dnf / pacman / Linuxbrew)
    "/usr/bin/7zz",
    "/usr/bin/7z",
    "/home/linuxbrew/.linuxbrew/bin/7zz",
]

# Binary names to try via ``shutil.which`` in PATH order. ``7zz`` first
# so modern Homebrew wins when both are installed; ``7za`` covers the
# old Linux ``p7zip-full-rar`` packaging where the RAR-capable binary
# is renamed.
_FIND_7Z_CANDIDATE_NAMES: tuple[str, ...] = ("7zz", "7z", "7za")

# Documented registry locations the official 7-Zip installer writes:
#   HKLM\SOFTWARE\7-Zip\Path                 (admin / system install)
#   HKCU\Software\7-Zip\Path                 (user install)
#   HKLM\SOFTWARE\WOW6432Node\7-Zip\Path     (32-bit on 64-bit Windows)
# Value is the install directory. The exe is `<dir>\7z.exe`.
_FIND_7Z_REGISTRY_KEYS: list[tuple[str, str]] = [
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\7-Zip"),
    ("HKEY_CURRENT_USER", r"Software\7-Zip"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\7-Zip"),
]


def _find_7z_in_registry() -> str | None:
    """Read the 7-Zip install path from the Windows registry.

    Returns the absolute path to `7z.exe` if any documented key
    points at a directory containing the executable, else None.
    Silently returns None on non-Windows platforms — ``import winreg``
    raises ``ModuleNotFoundError`` there, which the bare ``except
    ImportError`` below catches.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    hives = {
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    }
    for hive_name, sub in _FIND_7Z_REGISTRY_KEYS:
        hive = hives.get(hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except (FileNotFoundError, OSError):
            continue
        if not value:
            continue
        candidate = Path(value) / "7z.exe"
        if candidate.exists():
            return str(candidate)
    return None


def _find_7z() -> str | None:
    """Locate the 7-Zip executable. Cross-platform.

    Windows search order:
      1. Default install paths (Program Files / Program Files (x86))
      2. Windows registry — covers Scoop, custom installs, NanaZip,
         and any case where 7-Zip isn't on PATH (the installer does
         not add itself to PATH by default).
      3. PATH lookup via shutil.which.

    macOS / Linux search order:
      1. Hard-coded Homebrew + system-package paths (``/opt/homebrew/...``,
         ``/usr/local/bin/...``, ``/usr/bin/...``). The Homebrew
         ``sevenzip`` formula sometimes doesn't get linked into
         ``/opt/homebrew/bin``, so we check the ``opt`` cellar path
         too — that one is always created.
      2. PATH lookup via shutil.which, trying ``7zz`` (modern
         Homebrew), ``7z`` (legacy p7zip), then ``7za``.

    Returns the absolute path to the 7-Zip executable, or None when
    nothing usable was found. ``import_from_rar`` surfaces a clear
    install-instruction error message in the latter case.
    """
    if sys.platform == "win32":
        for candidate in _FIND_7Z_DEFAULT_PATHS:
            if Path(candidate).exists():
                return candidate
        from_registry = _find_7z_in_registry()
        if from_registry:
            return from_registry
        for name in _FIND_7Z_CANDIDATE_NAMES:
            found = shutil.which(name)
            if found:
                return found
        return None

    # POSIX (macOS / Linux): cellar / opt paths first, then PATH.
    for candidate in _FIND_7Z_UNIX_PATHS:
        if Path(candidate).exists():
            return candidate
    for name in _FIND_7Z_CANDIDATE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _extract_rar(archive_path: Path, dest: Path) -> str | None:
    """Extract a RAR archive into ``dest``. Returns None on success,
    error string on failure.

    Tries every RAR extractor that's plausibly installed, in order of
    preference, and falls through on failure. The order accounts for
    real-world RAR support gaps:

    - **7z / 7zz** (Windows 7-Zip, modern Homebrew sevenzip): works
      for classic RAR4 and basic RAR5 archives. Critical caveat:
      open-source 7zz on macOS / Linux does NOT include RARLAB's
      proprietary RAR5 codec, so newer RAR5 archives that use the v6
      compression methods (``Method = v6:128K:m5`` and similar)
      partially extract — directory structure created, file contents
      report ``Unsupported Method`` errors and exit code 2. The
      Windows 7-Zip ships ``Codecs/Rar.dll`` from RARLAB and handles
      everything; that's why this same RAR works in the Windows VM
      but not under Homebrew sevenzip on macOS.

    - **unar** (The Unarchiver, BSD-licensed, ``brew install unar``
      / ``apt install unar``): handles RAR5 v6 archives that 7zz
      can't, including the ``character_underwear`` mod that exposed
      this gap. The CLI uses different flags from 7-Zip.

    - **bsdtar** (libarchive, ships with macOS by default at
      ``/usr/bin/bsdtar``, optional on Linux): handles RAR5 via
      libarchive 3.7+. Extracts cleanly when neither 7z nor unar are
      installed — important because it means CDUMM works on a fresh
      macOS install with no extra Homebrew packages.

    Each attempt extracts into a fresh subdirectory so partial output
    from a failed attempt doesn't contaminate the next one.
    """
    from cdumm.platform import subprocess_no_window_kwargs
    sub_kwargs = subprocess_no_window_kwargs()

    attempts: list[tuple[str, list[str]]] = []
    seven_z = _find_7z()
    if seven_z:
        attempts.append(
            (seven_z,
             [seven_z, "x", str(archive_path), f"-o{dest}", "-y"]))
    unar = shutil.which("unar") or (
        "/opt/homebrew/bin/unar" if Path("/opt/homebrew/bin/unar").exists()
        else None)
    if unar:
        attempts.append(
            ("unar",
             [unar, "-quiet", "-force-overwrite",
              "-output-directory", str(dest), str(archive_path)]))
    bsdtar = shutil.which("bsdtar") or (
        "/usr/bin/bsdtar" if Path("/usr/bin/bsdtar").exists() else None)
    if bsdtar:
        attempts.append(
            ("bsdtar",
             [bsdtar, "-xf", str(archive_path), "-C", str(dest)]))

    if not attempts:
        return None  # caller surfaces the "no extractor installed" message

    last_err: str = ""
    for tool_name, cmd in attempts:
        # Reset the destination between tries so a partial extraction
        # from a previous tool doesn't get treated as a successful
        # extract by the format-detection downstream.
        try:
            for child in dest.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=120, **sub_kwargs)
        except Exception as e:
            last_err = f"{tool_name}: {e}"
            continue

        if proc.returncode == 0 and any(dest.iterdir()):
            return None

        stderr_msg = proc.stderr.decode(errors="replace").strip()
        last_err = (
            f"{tool_name} exit={proc.returncode}"
            + (f": {stderr_msg.splitlines()[-1]}" if stderr_msg else ""))

    return last_err or "RAR extraction failed"


def import_from_rar(
    archive_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a RAR archive by extracting via 7-Zip / unar / bsdtar."""
    mod_name = archive_path.stem
    result = ModImportResult(mod_name)

    have_extractor = (
        _find_7z() is not None
        or shutil.which("unar") is not None
        or Path("/opt/homebrew/bin/unar").exists()
        or shutil.which("bsdtar") is not None
        or Path("/usr/bin/bsdtar").exists()
    )
    if not have_extractor:
        if sys.platform == "darwin":
            install_hint = (
                "Install via Homebrew (`brew install sevenzip` or "
                "`brew install unar`) — bsdtar is normally pre-installed "
                "but seems missing on this system. Or extract the .rar "
                "manually and drop the folder.")
        elif sys.platform.startswith("linux"):
            install_hint = (
                "Install your distro's 7-Zip package (apt install p7zip-full, "
                "dnf install p7zip-plugins, pacman -S p7zip) or `unar` / "
                "`bsdtar`. Or extract the .rar manually and drop the folder.")
        else:
            install_hint = (
                "Install from https://7-zip.org or extract manually and drop "
                "the folder.")
        result.error = "RAR import requires a RAR-capable extractor.\n" + install_hint
        return result

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        err = _extract_rar(archive_path, tmp_path)
        if err is not None:
            result.error = f"Failed to extract RAR: {err}"
            return result

        # Unpack any nested archives so format detectors see folders.
        _extract_nested_archives(tmp_path)

        return _import_from_extracted(tmp_path, game_dir, db, snapshot, deltas_dir,
                                      mod_name, existing_mod_id)


_LOOSE_GAME_EXTENSIONS = {".json", ".xml", ".css", ".html", ".thtml",
                          ".dds", ".ttf", ".otf", ".wem", ".bnk", ".mp4"}
_SKIP_LOOSE_FILES = {"mod.json", "manifest.json", "modinfo.json"}


def _import_remaining_loose_files(
    extracted_dir: Path, game_dir: Path, db: Database,
    snapshot: SnapshotManager, deltas_dir: Path,
    result: ModImportResult,
) -> None:
    """Import loose game files that weren't matched by _match_game_files.

    For mixed-format mods (standalone PAZ + loose .json/.xml files), the
    main import handles the PAZ but drops the loose files. This function
    collects those files and routes them through the CB handler which can
    resolve filenames to PAZ directories via PAMT lookup, then imports the
    converted output into the same mod entry.
    """
    from cdumm.engine.crimson_browser_handler import convert_to_paz_mod

    # Collect loose files not inside numbered directories
    loose_files: list[Path] = []
    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(extracted_dir)
        parts = rel.parts
        # Skip files inside numbered directories (already handled as PAZ)
        if parts and parts[0].isdigit() and len(parts[0]) == 4:
            continue
        # Skip meta/ directory (PAPGT handled by CDUMM)
        if parts and parts[0].lower() == "meta":
            continue
        # Skip known non-game files
        if f.name.lower() in _SKIP_LOOSE_FILES:
            continue
        # Only include known game data extensions
        if f.suffix.lower() in _LOOSE_GAME_EXTENSIONS:
            loose_files.append(f)

    if not loose_files:
        return

    logger.info("Mixed-format mod: found %d loose files after PAZ import: %s",
                len(loose_files), [f.name for f in loose_files])

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        files_dir = work_dir / "_loose_files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for f in loose_files:
            rel = f.relative_to(extracted_dir)
            dst = files_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

        # Create synthetic CB manifest for the loose files
        manifest = {
            "id": result.name,
            "files_dir": ".",
            "_base_dir": files_dir,
        }

        cb_output = work_dir / "_cb_output"
        converted = convert_to_paz_mod(manifest, game_dir, cb_output)
        if converted is None:
            logger.warning("CB handler could not resolve loose files for %s", result.name)
            return

        # Import the converted files into the same mod entry
        loose_result = _process_extracted_files(
            converted, game_dir, db, snapshot, deltas_dir, result.name,
            existing_mod_id=result.mod_id)

        if loose_result.changed_files:
            result.changed_files.extend(loose_result.changed_files)
            logger.info("Mixed-format mod: imported %d additional files from loose pass",
                        len(loose_result.changed_files))
        else:
            logger.warning("Mixed-format mod: loose pass matched no files for %s", result.name)


def _import_from_extracted(
    tmp_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    mod_name: str, existing_mod_id: int | None = None,
) -> ModImportResult:
    """Common import logic for extracted archives (zip/7z)."""
    result = ModImportResult(mod_name)

    # Check for Crimson Browser format and convert if needed
    cb_manifest = detect_crimson_browser(tmp_path)
    if cb_manifest is not None:
        cb_work = tmp_path.parent / "_cb_converted"
        converted = convert_to_paz_mod(cb_manifest, game_dir, cb_work)
        if converted is not None:
            cb_name = _pick_cb_display_name(cb_manifest.get("id"), mod_name)
            modinfo = _read_modinfo(tmp_path)
            if modinfo and modinfo.get("name"):
                cb_name = modinfo["name"]
            cb_result = _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, cb_name,
                existing_mod_id=existing_mod_id, modinfo=modinfo)
            # Compound archive support — see import_from_folder for context.
            cb_base_raw = cb_manifest.get("_base_dir")
            cb_base = Path(cb_base_raw) if cb_base_raw is not None else None
            if (cb_base and cb_base != tmp_path
                    and cb_result and not cb_result.error
                    and cb_result.changed_files):
                try:
                    _import_sibling_json_patches(
                        tmp_path, cb_base, game_dir, db, deltas_dir)
                except Exception as e:
                    logger.debug("Sibling JSON scan after CB failed: %s", e)
            return cb_result

    # Check for loose file mod (mod.json + files/ directory)
    lfm = detect_loose_file_mod(tmp_path)
    if lfm is not None:
        lfm_work = tmp_path.parent / "_lfm_converted"
        converted = convert_to_paz_mod(lfm, game_dir, lfm_work)
        if converted is not None:
            mi = lfm.get("_modinfo", {})
            lfm_name = mi.get("title", mod_name)
            lfm_modinfo = {
                "name": mi.get("title"), "version": mi.get("version"),
                "author": mi.get("author"), "description": mi.get("description"),
                "force_inplace": mi.get("force_inplace"),
            }
            return _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

    # Check for JSON byte-patch format — use ENTR deltas for proper composition
    jp_data = detect_json_patch(tmp_path)
    if jp_data is not None:
        from cdumm.engine.json_patch_handler import import_json_as_entr
        jp_name = _json_mod_display_name(jp_data, mod_name)
        jp_modinfo = _json_mod_modinfo(jp_data)
        entr_result = import_json_as_entr(
            jp_data, game_dir, db, deltas_dir, jp_name,
            existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
        if entr_result is not None:
            if entr_result.get("version_mismatch"):
                result = ModImportResult(jp_name)
                game_ver = entr_result.get("game_version", "unknown")
                mismatched = entr_result.get("mismatched", 0)
                result.error = (
                    f"This mod is incompatible with the current game version. "
                    f"{mismatched} byte patches don't match. The game data has "
                    f"changed since this mod was created (mod targets version "
                    f"{game_ver}). The mod author needs to update it.")
                return result
            if not entr_result["changed_files"]:
                result = ModImportResult(jp_name)
                result.error = (
                    "This mod's changes are already present in your game files. "
                    "Nothing to apply.")
                return result
            result = ModImportResult(jp_name)
            result.changed_files = entr_result["changed_files"]
            # Surface per-file skips as a non-fatal info banner so
            # multi-file mods (Faster NPC Animations etc.) tell the
            # user which specific files were skipped due to byte
            # drift. Bug from round-7 systematic-debugging: the
            # multi-file partial-skip fix added skipped_files to
            # the result but the GUI saw nothing.
            _skipped = entr_result.get("skipped_files") or []
            if _skipped:
                _names = ", ".join(s.get("game_file", "?")
                                   for s in _skipped[:3])
                if len(_skipped) > 3:
                    _names += f", +{len(_skipped) - 3} more"
                result.info = (
                    f"Imported, but {len(_skipped)} file(s) skipped "
                    f"due to byte mismatch — these likely need an "
                    f"update from the mod author: {_names}"
                )
            if jp_data.get("patches"):
                _store_json_patches(db, result, jp_data, game_dir)
            return result
        # Fall through if ENTR import failed

    # Check for DDS texture mod
    tex_info = detect_texture_mod(tmp_path)
    if tex_info is not None:
        tex_work = tmp_path.parent / "_tex_converted"
        converted = convert_texture_mod(tex_info, game_dir, tex_work)
        if converted is not None:
            tex_name = tex_info.get("name", mod_name)
            modinfo = _read_modinfo(tmp_path)
            if modinfo and modinfo.get("name"):
                tex_name = modinfo["name"]
            return _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, tex_name,
                existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Check for scripts: when the archive contains ONLY a .bat or .py
    # and no PAZ/PAMT files, treat it as a script mod (e.g. Glider
    # Stamina, which ships as glider-stamina.bat inside a zip). Copy
    # the script out of tmp_path to a standalone file and run it
    # through import_from_script, so users don't have to extract zips
    # manually.
    scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
    if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
        if len(scripts) == 1:
            # Single-script archive → route to the script-mod flow.
            logger.info(
                "Archive contains a single script mod (%s) — routing to "
                "import_from_script", scripts[0].name)
            return import_from_script(
                scripts[0], game_dir, db, snapshot, deltas_dir)
        # Multiple scripts are ambiguous; keep the explicit error so
        # the user can pick one and drop it directly.
        names = ", ".join(s.name for s in scripts)
        result.error = (
            f"Archive contains {len(scripts)} scripts ({names}). "
            "Extract and drop the one you want to run as a script mod."
        )
        return result

    # Detect multi-variant
    variant = _find_best_variant(tmp_path)
    if variant:
        logger.info("Multi-variant archive, using: %s", variant.name)
        tmp_path = variant
        mod_name = f"{mod_name} ({variant.name})"

    modinfo = _read_modinfo(tmp_path)
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]

    result = _process_extracted_files(
        tmp_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Second pass: import loose game files not matched by _match_game_files.
    # Only runs for mixed-format mods (standalone PAZ dirs + loose files).
    if result.changed_files and not result.error and result.mod_id is not None:
        has_standalone_paz = any(
            d.is_dir() and d.name.isdigit() and len(d.name) == 4
            and (d / "0.paz").exists()
            for d in tmp_path.iterdir() if d.is_dir()
        )
        if has_standalone_paz:
            _import_remaining_loose_files(
                tmp_path, game_dir, db, snapshot, deltas_dir, result)

    # Third pass: register XML XPath patch / merge files (JMM parity).
    # Runs regardless of whether the main pass found other content — some
    # mods are purely XML patches and have no other game files.
    if result.mod_id is None and _scan_xml_patches(tmp_path):
        # No prior pass created a mod row; create one now for this
        # XML-patches-only mod so the deltas have somewhere to anchor.
        author = (modinfo or {}).get("author")
        version = (modinfo or {}).get("version")
        description = (modinfo or {}).get("description")
        # Reuse 'paz' as the umbrella mod_type; the presence of deltas with
        # kind='xml_patch'/'xml_merge' is what tags this mod as XML-patch
        # style. Keeps the existing schema CHECK constraint happy.
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, "
            "description) VALUES (?, 'paz', 0, ?, ?, ?)",
            (mod_name, author, version, description),
        )
        db.connection.commit()
        result.mod_id = cur.lastrowid
        result.error = None
        logger.info("xml_patch-only mod created: id=%d name=%s",
                    result.mod_id, mod_name)
    if result.mod_id is not None:
        claimed = _register_xml_patches(
            tmp_path, result.mod_id, mod_name, db, deltas_dir)
        if claimed:
            for p in claimed:
                rel = str(p.relative_to(tmp_path)).replace("\\", "/")
                if rel not in result.changed_files:
                    result.changed_files.append(rel)

    return result


def _scan_xml_patches(root: Path) -> list[Path]:
    """Lightweight detector — returns paths to XML / CSS / HTML
    partial-patch files under ``root`` without touching the DB. Used
    to decide whether an otherwise-empty archive actually contains
    patch content. Name kept for backward compatibility; covers all
    three handler families now."""
    from cdumm.engine.xml_patch_handler import (
        detect_patch_file as detect_xml,
    )
    from cdumm.engine.css_patch_handler import (
        detect_patch_file as detect_css,
    )
    from cdumm.engine.html_patch_handler import (
        detect_patch_file as detect_html,
    )
    hits: list[Path] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if detect_xml(f) or detect_css(f) or detect_html(f):
            hits.append(f)
    return hits


def import_from_zip(
    zip_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a zip archive."""
    mod_name = zip_path.stem
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_path)
        except zipfile.BadZipFile as e:
            result.error = f"Invalid zip file: {e}"
            return result
        except Exception as e:
            result.error = f"Failed to extract zip: {e}"
            logger.error("Zip extraction failed: %s", e, exc_info=True)
            return result

        # Unpack any nested archives (language packs, multi-variant
        # bundles) so the format detectors below see folders, not
        # archives. Bug from Faisal 2026-04-27 — Display take and
        # steal price ships 5 inner ZIPs (one per language).
        _extract_nested_archives(tmp_path)

        # Stage ASI files separately for GUI-side install (mixed ZIP support).
        # Stage into a per-import subdir under deltas_dir (persistent), NOT
        # tmp_path — the tempfile context auto-deletes when this function
        # returns, which would wipe the staged files before the GUI handler
        # can copy them. The per-import UUID subdir prevents collisions when
        # two imports run back-to-back (or, in the worker model, in
        # parallel). Also: only stage `.ini` files whose stem matches a
        # sibling `.asi`, so game data .ini files (e.g. 0008/foo.ini) aren't
        # stolen.
        import uuid as _uuid
        asi_staging = deltas_dir / "_asi_staging" / _uuid.uuid4().hex
        # Pre-scan: collect basenames of all .asi files for the .ini filter.
        _all_files = [f for f in tmp_path.rglob("*") if f.is_file()]
        _asi_stems = {
            f.stem.lower() for f in _all_files if f.suffix.lower() == ".asi"
        }
        try:
            for f in _all_files:
                if "_asi_staging" in f.parts:
                    continue
                ext = f.suffix.lower()
                if ext == ".asi":
                    pass  # always stage
                elif ext == ".ini" and f.stem.lower() in _asi_stems:
                    pass  # companion INI for an ASI we're staging
                else:
                    continue
                asi_staging.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(f), str(asi_staging / f.name))
                result.asi_staged.append(str(asi_staging / f.name))
                logger.info("Staged ASI file for GUI install: %s", f.name)
        except OSError as _stage_err:
            # Permission denied, file locked, cross-device — fail the
            # import gracefully instead of letting the exception bubble
            # up uncaught and confuse the GUI. Surface to result.info
            # so the user sees a yellow banner explaining the partial
            # state, not a silent skip of expected ASI files.
            logger.warning(
                "ASI staging failed: %s — continuing with PAZ-only import",
                _stage_err)
            stage_msg = (
                f"Some ASI plugin files could not be staged ({_stage_err}). "
                f"PAZ-only import continued; copy ASI files to bin64/ "
                f"manually if needed."
            )
            result.info = (
                f"{result.info}\n{stage_msg}" if result.info else stage_msg)
        # Local copy preserved across any later `result = ...` reassignments.
        _carryover_asi_staged = list(result.asi_staged)

        # Check for OG_ XML replacement files and register them as a
        # full mod (mods row + mod_deltas rows). Falls through if none
        # of the OG_ targets resolve against the game's PAMT.
        og_xml = _detect_xml_replacements(tmp_path)
        if og_xml:
            og_modinfo = _read_modinfo(tmp_path)
            og_result = _import_og_xml_as_mod(
                og_xml, game_dir, db, deltas_dir, mod_name,
                existing_mod_id=existing_mod_id, modinfo=og_modinfo,
            )
            if og_result is not None:
                og_result.asi_staged = list(_carryover_asi_staged)
                return og_result

        # Check for Crimson Browser format and convert if needed
        cb_manifest = detect_crimson_browser(tmp_path)
        if cb_manifest is not None:
            cb_work = Path(tmp) / "_cb_converted"
            converted = convert_to_paz_mod(cb_manifest, game_dir, cb_work)
            if converted is not None:
                cb_name = _pick_cb_display_name(cb_manifest.get("id"), mod_name)
                modinfo = _read_modinfo(tmp_path)
                if modinfo and modinfo.get("name"):
                    cb_name = modinfo["name"]
                result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, cb_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)
                # Compound-mod support (Lightsaber: CB mod in Lightsaber_v1_3/
                # plus a sibling Lightsaber_shop.json at archive root). Import
                # any JSON patches outside the CB subfolder as their own mods.
                cb_base = cb_manifest.get("_base_dir")
                if (cb_base and cb_base != tmp_path
                        and result and not result.error):
                    try:
                        _import_sibling_json_patches(
                            tmp_path, cb_base, game_dir, db, deltas_dir)
                    except Exception as e:
                        logger.debug("Sibling JSON scan after CB failed: %s", e)
                return result

        # Plain XML drop: some mod authors ship gamepad / UI XML mods
        # as raw `.xml` files in a folder with a `modinfo.json`, no
        # OG_ prefix and no Crimson Browser manifest. Match by PAMT
        # basename. Only fires when there's no other format signal
        # (no PAZ NNNN/ dirs, no JSON patches at the root). Source:
        # RockNBeard report 2026-04-30, Standard Gamepad Layout
        # (Nexus mod 1489).
        plain_xml = _detect_plain_xml_replacements(tmp_path)
        if plain_xml:
            has_paz_dirs = any(
                d.is_dir() and d.name.isdigit() and len(d.name) == 4
                for d in tmp_path.rglob("*") if d.is_dir()
            )
            has_json_patches = detect_json_patch(tmp_path) is not None
            if not has_paz_dirs and not has_json_patches:
                plain_modinfo = _read_modinfo(tmp_path)
                plain_result = _import_og_xml_as_mod(
                    plain_xml, game_dir, db, deltas_dir, mod_name,
                    existing_mod_id=existing_mod_id, modinfo=plain_modinfo,
                )
                if plain_result is not None:
                    plain_result.asi_staged = list(_carryover_asi_staged)
                    return plain_result

        # Check for loose file mod (files/NNNN/ structure)
        lfm = detect_loose_file_mod(tmp_path)
        if lfm is not None:
            lfm_work = Path(tmp) / "_lfm_converted"
            converted = convert_to_paz_mod(lfm, game_dir, lfm_work)
            if converted is not None:
                mi = lfm.get("_modinfo", {})
                lfm_name = mi.get("title", mod_name)
                lfm_modinfo = {
                    "name": mi.get("title"), "version": mi.get("version"),
                    "author": mi.get("author"), "description": mi.get("description"),
                    "force_inplace": mi.get("force_inplace"),
                }
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                    existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

        # Check for JSON byte-patch format — use ENTR deltas for proper composition
        jp_data = detect_json_patch(tmp_path)
        if jp_data is not None:
            from cdumm.engine.json_patch_handler import import_json_as_entr
            jp_name = _json_mod_display_name(jp_data, mod_name)
            jp_modinfo = _json_mod_modinfo(jp_data)
            entr_result = import_json_as_entr(
                jp_data, game_dir, db, deltas_dir, jp_name,
                existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
            if entr_result is None:
                # JSON patch detected but failed — don't fall through to slow PAZ scan
                result = ModImportResult(jp_name)
                result.asi_staged = list(_carryover_asi_staged)
                target_files = [p.get("game_file", "?") for p in jp_data.get("patches", [])]
                result.error = (
                    f"JSON patch mod detected but failed to process. "
                    f"Target game file(s) not found: {', '.join(target_files)}. "
                    f"Use Inspect Mod for a detailed diagnostic report.")
                return result
            if entr_result is not None:
                if entr_result.get("version_mismatch"):
                    result = ModImportResult(jp_name)
                    result.asi_staged = list(_carryover_asi_staged)
                    game_ver = entr_result.get("game_version", "unknown")
                    mismatched = entr_result.get("mismatched", 0)
                    result.error = (
                        f"This mod is incompatible with the current game version. "
                        f"{mismatched} byte patches don't match. The game data has "
                        f"changed since this mod was created (mod targets version "
                        f"{game_ver}). The mod author needs to update it.")
                    return result
                if not entr_result["changed_files"]:
                    result.error = (
                        "This mod's changes are already present in your game files. "
                        "Nothing to apply.")
                    return result
                result = ModImportResult(jp_name)
                result.asi_staged = list(_carryover_asi_staged)
                result.changed_files = entr_result["changed_files"]
                _skipped = entr_result.get("skipped_files") or []
                if _skipped:
                    _names = ", ".join(s.get("game_file", "?")
                                       for s in _skipped[:3])
                    if len(_skipped) > 3:
                        _names += f", +{len(_skipped) - 3} more"
                    result.info = (
                        f"Imported, but {len(_skipped)} file(s) "
                        f"skipped due to byte mismatch — these "
                        f"likely need an update from the mod "
                        f"author: {_names}"
                    )
                if jp_data.get("patches"):
                    _store_json_patches(db, result, jp_data, game_dir)
                return result

        # NattKh's Format 3 mods (field-names + intents) ship as a
        # single .json. detect_json_patch above checks for a
        # 'patches' array — Format 3 doesn't have one, so it falls
        # through here. Catch it before the rest of the detectors so
        # the user gets the Format 3 importer's specific error
        # ("no schema for X table") instead of the generic
        # "no recognized format" diagnostic.
        from cdumm.engine.json_patch_handler import is_natt_format_3
        f3_jsons = [p for p in tmp_path.rglob("*.json")
                    if "_asi_staging" not in p.parts
                    and is_natt_format_3(p)]
        if len(f3_jsons) == 1:
            return import_from_natt_format_3(
                json_path=f3_jsons[0], game_dir=game_dir, db=db,
                snapshot=snapshot, deltas_dir=deltas_dir)
        if len(f3_jsons) > 1:
            # Variant pack (CrimsonWings: 10pct/25pct/50pct/75pct/
            # infinite of one mod) is detected via stem prefix +
            # same target + similar intent counts. The GUI's variant
            # picker handles those before the worker runs, so reaching
            # here with a variant-pack shape means the user dropped
            # the ZIP through a non-picker path. Give a specific
            # message naming the variants. True multi-mod packs
            # (different targets / unrelated mods) get the original
            # "import one at a time" guidance.
            f3_pack = _scan_format3_variant_pack(tmp_path)
            if f3_pack:
                ids = ", ".join(vid for _, vid in f3_pack)
                result.error = (
                    f"This zip contains {len(f3_pack)} variants of "
                    f"one Format 3 mod ({ids}). Drop the zip on the "
                    f"main window to pick one variant, or extract the "
                    f"zip and import only the variant JSON you want."
                )
                return result
            names = ", ".join(p.name for p in f3_jsons)
            result.error = (
                f"This zip contains {len(f3_jsons)} Format 3 mods "
                f"({names}). Please import them one at a time so each "
                f"gets its own row in the mod list."
            )
            return result

        # Check for DDS texture mod (folder of .dds files, no PAZ/PAMT)
        tex_info = detect_texture_mod(tmp_path)
        if tex_info is not None:
            tex_work = Path(tmp) / "_tex_converted"
            converted = convert_texture_mod(tex_info, game_dir, tex_work)
            if converted is not None:
                tex_name = tex_info.get("name", mod_name)
                modinfo = _read_modinfo(tmp_path)
                if modinfo and modinfo.get("name"):
                    tex_name = modinfo["name"]
                result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, tex_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)
                return result

        # Check if zip contains a script. When it's a single-script
        # zip (like Glider Stamina's glider-stamina.bat), route it to
        # the script-mod flow so the user doesn't have to unzip first.
        scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
        if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
            if len(scripts) == 1:
                logger.info(
                    "Zip '%s' is a single-script mod (%s) — routing to "
                    "import_from_script", archive_path.name, scripts[0].name)
                return import_from_script(
                    scripts[0], game_dir, db, snapshot, deltas_dir)
            names = ", ".join(s.name for s in scripts)
            result.error = (
                f"'{archive_path.name}' contains {len(scripts)} scripts "
                f"({names}). Extract and drop the specific script you "
                "want to run."
            )
            return result

        # Detect multi-variant zips
        variant = _find_best_variant(tmp_path)
        if variant:
            logger.info("Multi-variant zip, using: %s", variant.name)
            tmp_path = variant
            mod_name = f"{mod_name} ({variant.name})"
        else:
            wrapped = _first_numbered_parent(tmp_path)
            if wrapped is not None and wrapped != tmp_path:
                logger.info("Descending wrapper: %s -> %s",
                            tmp_path.name, wrapped.relative_to(tmp_path))
                tmp_path = wrapped

        # Read mod metadata from modinfo.json if present
        modinfo = _read_modinfo(tmp_path)
        if modinfo and modinfo.get("name"):
            mod_name = modinfo["name"]

        result = _process_extracted_files(
            tmp_path, game_dir, db, snapshot, deltas_dir, mod_name,
            existing_mod_id=existing_mod_id, modinfo=modinfo)
        # _process_extracted_files builds a fresh ModImportResult — re-attach
        # any ASI files we staged at the top of import_from_zip so the GUI
        # handler still gets the install list.
        if _carryover_asi_staged:
            result.asi_staged = list(_carryover_asi_staged)

    return result


def _import_sibling_json_patches(
    root: Path, exclude_subdir: Path, game_dir: Path,
    db: Database, deltas_dir: Path,
    failures_out: list[str] | None = None,
) -> None:
    """Import JSON byte-patch siblings that live alongside a CB/loose-file mod.

    Used when a compound archive contains multiple independent mod
    components: e.g. a CB mod in Lightsaber_v1_3/ plus a standalone
    Lightsaber_shop.json at the archive root. The JSONs outside
    ``exclude_subdir`` are imported as their own separate mods so the
    user can toggle stat changes vs shop changes independently.
    """
    from cdumm.engine.json_patch_handler import (
        detect_json_patches_all, import_json_as_entr,
    )
    import re as _re_sib
    _NNNN = _re_sib.compile(r"^\d{4}$")
    try:
        exclude_resolved = exclude_subdir.resolve()
    except OSError:
        exclude_resolved = exclude_subdir

    candidates: list[Path] = []
    for p in root.rglob("*.json"):
        try:
            presolved = p.resolve()
            if exclude_resolved in presolved.parents or presolved == exclude_resolved:
                continue
        except OSError:
            continue
        # Mirror the NNNN/meta guard from detect_json_patches_all so we
        # don't try to parse extracted-vanilla JSON-looking blobs as mod
        # patches when the archive happens to ship alongside those dirs.
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(_NNNN.match(part) or part.lower() == "meta"
               for part in rel.parts[:-1]):
            continue
        candidates.append(p)

    for cand in candidates:
        jp_list = detect_json_patches_all(cand)
        for jp_data in jp_list:
            jp_name = _json_mod_display_name(
                jp_data, jp_data["_json_path"].stem)
            jp_modinfo = _json_mod_modinfo(jp_data)
            # NOTE: we intentionally do NOT reuse an existing_mod_id by
            # name here. A prior attempt matched `SELECT id FROM mods
            # WHERE name = ?`, but that's too broad — an unrelated mod
            # with the same display name ("Shop", "Dark Mode" etc.)
            # would get its deltas/source silently overwritten. The
            # better trade-off is to accept a potential duplicate row
            # on compound-archive re-import and let the user remove the
            # stale sibling manually; we will address this properly
            # with a compound-archive ID once the import worker can
            # thread that lineage through.
            # Snapshot the current max mod_id BEFORE calling the
            # importer. If it raises mid-insert, the only orphans to
            # clean up are rows with id > this watermark. Matching
            # strictly by name was too broad — any pre-existing mod
            # with the same display name and zero deltas (a legitimate
            # in-progress import, or an earlier failed one) got
            # silently wiped too. GDS + BMAD finding C-H5.
            try:
                pre_max_row = db.connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM mods").fetchone()
                pre_max_id = int(pre_max_row[0]) if pre_max_row else 0
            except Exception:
                pre_max_id = 0
            try:
                entr_result = import_json_as_entr(
                    jp_data, game_dir, db, deltas_dir, jp_name,
                    existing_mod_id=None, modinfo=jp_modinfo)
            except Exception as e:
                logger.warning(
                    "Sibling JSON '%s' import failed: %s",
                    cand.name, e)
                # Surface to caller so the GUI can show an InfoBar
                # instead of the user seeing the primary succeed with
                # no visible indication a sibling failed. GDS #7.
                if failures_out is not None:
                    failures_out.append(
                        f"sibling '{cand.name}' failed: {e}")
                # Scope the orphan scan to mods inserted AFTER the
                # watermark, with name=jp_name and zero deltas.
                try:
                    from cdumm.engine.mod_manager import ModManager
                    rows = db.connection.execute(
                        "SELECT m.id FROM mods m "
                        "LEFT JOIN mod_deltas d ON d.mod_id = m.id "
                        "WHERE m.name = ? AND m.id > ? AND d.id IS NULL",
                        (jp_name, pre_max_id)).fetchall()
                    mm = ModManager(db, deltas_dir)
                    for (gid,) in rows:
                        try:
                            mm.remove_mod(gid)
                        except Exception as e_rm:
                            logger.debug(
                                "orphan sibling cleanup remove_mod(%d) "
                                "failed: %s", gid, e_rm)
                except Exception as e_scan:
                    logger.debug(
                        "orphan sibling cleanup scan failed: %s", e_scan)
                continue
            # Surface silent failures explicitly. import_json_as_entr
            # may create a ghost DB row before deciding a mod is
            # incompatible (version_mismatch) or empty (no changed
            # files). Roll back so the user doesn't see an orphan
            # "Lightsaber_shop" row with zero deltas and no toggle.
            if entr_result is None:
                logger.warning(
                    "Sibling JSON '%s' (%s): importer returned None",
                    cand.name, jp_name)
                continue
            if entr_result.get("version_mismatch"):
                game_ver = entr_result.get("game_version", "unknown")
                mismatched = entr_result.get("mismatched", 0)
                logger.warning(
                    "Sibling JSON '%s' (%s): version mismatch, "
                    "%d patches don't match vanilla (mod targets %s). "
                    "Not importing.",
                    cand.name, jp_name, mismatched, game_ver)
                # Roll back any ghost mod row the importer may have
                # created before failing. Use ModManager so mod_deltas /
                # conflicts / sources folder get cleaned up too — the
                # raw DELETE here was orphaning delta rows and the
                # archived sources dir on every rejection.
                gid = entr_result.get("mod_id")
                if gid is not None:
                    try:
                        from cdumm.engine.mod_manager import ModManager
                        ModManager(db, deltas_dir).remove_mod(gid)
                    except Exception as e_rb:
                        logger.debug(
                            "Sibling rollback via ModManager failed (%s), "
                            "falling back to raw DELETE", e_rb)
                        try:
                            db.connection.execute(
                                "DELETE FROM mods WHERE id = ?", (gid,))
                            db.connection.commit()
                        except Exception:
                            pass
                continue
            if not entr_result.get("changed_files"):
                logger.info(
                    "Sibling JSON '%s' (%s): no file changes, "
                    "already applied or noop — rolling back",
                    cand.name, jp_name)
                # Symmetry with the version_mismatch branch: the importer
                # may have created a ghost mod row before realising no
                # bytes needed changing. Remove it so the user doesn't see
                # an orphan sibling entry with zero deltas.
                gid = entr_result.get("mod_id")
                if gid is not None:
                    try:
                        from cdumm.engine.mod_manager import ModManager
                        ModManager(db, deltas_dir).remove_mod(gid)
                    except Exception as e_rb:
                        logger.debug(
                            "Sibling no-changes rollback failed (%s)", e_rb)
                        try:
                            db.connection.execute(
                                "DELETE FROM mods WHERE id = ?", (gid,))
                            db.connection.commit()
                        except Exception:
                            pass
                continue
            logger.info(
                "Compound mod: sibling JSON '%s' imported as separate mod "
                "'%s' (%d files changed)",
                cand.name, jp_name, len(entr_result["changed_files"]))


def import_from_folder(
    folder_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a folder of modified files."""
    mod_name = folder_path.name

    # Check for Crimson Browser format and convert if needed
    manifest = detect_crimson_browser(folder_path)
    if manifest is not None:
        with tempfile.TemporaryDirectory() as cb_tmp:
            cb_work = Path(cb_tmp) / "_cb_converted"
            converted = convert_to_paz_mod(manifest, game_dir, cb_work)
            if converted is not None:
                cb_name = _pick_cb_display_name(manifest.get("id"), mod_name)
                modinfo = _read_modinfo(folder_path)
                if modinfo and modinfo.get("name"):
                    cb_name = modinfo["name"]
                cb_result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, cb_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)
                # Compound-mod support: when the CB manifest came from a
                # SUBFOLDER (Lightsaber_v1_3/ inside a zip that also has a
                # sibling Lightsaber_shop.json at the root), the sibling
                # JSON patches are their own independent mod components and
                # should be imported as separate mods so the user can toggle
                # stat-buff vs shop-addition independently.
                cb_base_raw = manifest.get("_base_dir")
                cb_base = Path(cb_base_raw) if cb_base_raw is not None else None
                if (cb_base and cb_base != folder_path
                        and cb_result and not cb_result.error
                        and cb_result.changed_files):
                    try:
                        _import_sibling_json_patches(
                            folder_path, cb_base, game_dir, db, deltas_dir)
                    except Exception as e:
                        logger.debug("Sibling JSON scan after CB failed: %s", e)
                return cb_result

    # Check for loose file mod (mod.json + files/ directory)
    lfm = detect_loose_file_mod(folder_path)
    if lfm is not None:
        with tempfile.TemporaryDirectory() as lfm_tmp:
            lfm_work = Path(lfm_tmp) / "_lfm_converted"
            converted = convert_to_paz_mod(lfm, game_dir, lfm_work)
            if converted is not None:
                mi = lfm.get("_modinfo", {})
                lfm_name = mi.get("title", mod_name)
                lfm_modinfo = {
                    "name": mi.get("title"), "version": mi.get("version"),
                    "author": mi.get("author"), "description": mi.get("description"),
                    "force_inplace": mi.get("force_inplace"),
                }
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                    existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

    # C1: compound layout detection. Issue #34 (kori228, Character
    # Creator - Female and Male) — a bodytype preset folder shipped
    # NNNN/0.paz mesh data AND a sibling FemaleAnimations.json. The
    # old flow short-circuited on JSON presence and silently dropped
    # the preset data. If we see NNNN/0.paz siblings alongside the
    # .json, import the PAZ-dir mod as primary and defer the JSONs
    # to _import_sibling_json_patches (same pattern CB-mode uses).
    def _has_paz_dirs(root: Path) -> bool:
        try:
            for d in root.iterdir():
                if (d.is_dir() and d.name.isdigit() and len(d.name) == 4
                        and (d / "0.paz").exists()):
                    return True
        except OSError:
            pass
        return False

    _is_compound_layout = _has_paz_dirs(folder_path)

    # Check for JSON byte-patch format in folder — use ENTR deltas.
    # When the folder has MULTIPLE valid JSONs (Trust Me main + Pet
    # Abyss Gear, bundled packs), import each as its own mod so the
    # user can toggle them independently. Only the first is returned
    # to the caller as the primary result; the rest are logged and
    # appear in the mod list after this function returns.
    from cdumm.engine.json_patch_handler import (
        detect_json_patches_all, import_json_as_entr,
    )
    jp_list = detect_json_patches_all(folder_path)
    if jp_list and _is_compound_layout:
        logger.info(
            "Compound layout: %d JSON patch(es) + NNNN/0.paz siblings "
            "in %s. Importing PAZ-dir mod as primary; JSON siblings "
            "will be deferred to sibling-import after.",
            len(jp_list), folder_path.name)
        # Fall through to the PAZ-dir flow below. After that
        # completes we invoke _import_sibling_json_patches so the
        # JSON patches land as their own separate mods.
        jp_list = []  # prevent JSON-only branch below from firing
    if jp_list:
        if len(jp_list) > 1:
            logger.info(
                "Folder '%s' contains %d JSON patches — importing each "
                "as a separate mod", folder_path.name, len(jp_list))
        # Import the first as the primary result we return
        primary_result = None
        for idx, jp_data in enumerate(jp_list):
            jp_name = _json_mod_display_name(
                jp_data, jp_data["_json_path"].stem)
            jp_modinfo = _json_mod_modinfo(jp_data)
            # Only the first JSON reuses the existing_mod_id (when
            # re-importing). Extras always create new mod rows.
            target_id = existing_mod_id if idx == 0 else None
            entr_result = import_json_as_entr(
                jp_data, game_dir, db, deltas_dir, jp_name,
                existing_mod_id=target_id, modinfo=jp_modinfo)
            if entr_result is None:
                continue
            if entr_result.get("version_mismatch"):
                if idx == 0:
                    result = ModImportResult(jp_name)
                    game_ver = entr_result.get("game_version", "unknown")
                    mismatched = entr_result.get("mismatched", 0)
                    result.error = (
                        f"This mod is incompatible with the current game version. "
                        f"{mismatched} byte patches don't match. The game data has "
                        f"changed since this mod was created (mod targets version "
                        f"{game_ver}). The mod author needs to update it.")
                    primary_result = result
                else:
                    logger.warning(
                        "Skipping bundled JSON '%s' — version mismatch",
                        jp_data["_json_path"].name)
                continue
            if not entr_result["changed_files"]:
                if idx == 0:
                    result = ModImportResult(jp_name)
                    result.error = (
                        "This mod's changes are already present in your game files. "
                        "Nothing to apply.")
                    primary_result = result
                continue
            result = ModImportResult(jp_name)
            result.changed_files = entr_result["changed_files"]
            if jp_data.get("patches"):
                _store_json_patches(db, result, jp_data, game_dir)
            if idx == 0:
                primary_result = result
            else:
                logger.info(
                    "Bundled JSON '%s' imported as separate mod '%s' "
                    "(%d files changed)",
                    jp_data["_json_path"].name, jp_name,
                    len(result.changed_files))
        if primary_result is not None:
            return primary_result

    # Format 3 (NattKh field-names) detection — same fix as
    # import_from_zip. detect_json_patches_all only matches files
    # with a 'patches' array; Format 3 uses 'intents' and falls
    # through. Catch it here before texture/PAZ scans so the user
    # gets the Format 3 importer's specific error.
    from cdumm.engine.json_patch_handler import is_natt_format_3
    f3_jsons = [p for p in folder_path.rglob("*.json")
                if is_natt_format_3(p)]
    if len(f3_jsons) == 1:
        return import_from_natt_format_3(
            json_path=f3_jsons[0], game_dir=game_dir, db=db,
            snapshot=snapshot, deltas_dir=deltas_dir)
    if len(f3_jsons) > 1:
        result = ModImportResult(mod_name)
        f3_pack = _scan_format3_variant_pack(folder_path)
        if f3_pack:
            ids = ", ".join(vid for _, vid in f3_pack)
            result.error = (
                f"This folder contains {len(f3_pack)} variants of "
                f"one Format 3 mod ({ids}). Drop the folder on the "
                f"main window to pick a variant, or import only the "
                f"specific variant JSON you want."
            )
            return result
        names = ", ".join(p.name for p in f3_jsons)
        result.error = (
            f"This folder contains {len(f3_jsons)} Format 3 mods "
            f"({names}). Please import them one at a time so each "
            f"gets its own row in the mod list."
        )
        return result

    # Check for DDS texture mod (folder of .dds files, no PAZ/PAMT)
    tex_info = detect_texture_mod(folder_path)
    if tex_info is not None:
        with tempfile.TemporaryDirectory() as tex_tmp:
            tex_work = Path(tex_tmp) / "_tex_converted"
            converted = convert_texture_mod(tex_info, game_dir, tex_work)
            if converted is not None:
                tex_name = tex_info.get("name", mod_name)
                modinfo = _read_modinfo(folder_path)
                if modinfo and modinfo.get("name"):
                    tex_name = modinfo["name"]
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, tex_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Check if folder contains scripts instead of game files
    scripts = list(folder_path.glob("*.bat")) + list(folder_path.glob("*.py"))
    if scripts and not _match_game_files(folder_path, game_dir, snapshot):
        result = ModImportResult(folder_path.name)
        result.error = (
            f"'{folder_path.name}' is a standalone script/tool, not a CDUMM mod. "
            "Run it directly if you need the tool, or drop the .py/.bat file "
            "individually if it's a CDUMM script mod."
        )
        return result

    # Detect variant folders: parent folder has multiple subdirectories each
    # containing their own 0.paz + 0.pamt (e.g., FatStacks2x/, FatStacks10x/).
    # Find the best single variant to import.
    variant = _find_best_variant(folder_path)
    if variant:
        logger.info("Multi-variant mod detected, using variant: %s", variant.name)
        folder_path = variant
        mod_name = f"{folder_path.parent.name} ({variant.name})"
    else:
        # Single-variant mods may wrap content in '<ModName>/files/NNNN/' or
        # similar. Descend through unambiguous single-subdir wrappers until we
        # find the folder that directly contains the NNNN game directories.
        wrapped = _first_numbered_parent(folder_path)
        if wrapped is not None and wrapped != folder_path:
            logger.info("Descending wrapper folder: %s -> %s",
                        folder_path.name, wrapped.relative_to(folder_path))
            folder_path = wrapped

    # Read mod metadata from modinfo.json if present
    modinfo = _read_modinfo(folder_path)
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]

    result = _process_extracted_files(
        folder_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Second pass: import loose game files not matched by _match_game_files.
    # Only runs for mixed-format mods (standalone PAZ dirs + loose files).
    if result.changed_files and not result.error and result.mod_id is not None:
        has_standalone_paz = any(
            d.is_dir() and d.name.isdigit() and len(d.name) == 4
            and (d / "0.paz").exists()
            for d in folder_path.iterdir() if d.is_dir()
        )
        if has_standalone_paz:
            _import_remaining_loose_files(
                folder_path, game_dir, db, snapshot, deltas_dir, result)

    # C1: compound layout — the PAZ-dir primary has landed; now
    # import any sibling .json patches as their own separate mods so
    # users can toggle them independently (issue #34).
    if (_is_compound_layout and result.changed_files and not result.error
            and result.mod_id is not None):
        try:
            # exclude_subdir is used by the helper to skip a CB/LFM
            # converted subdir. We don't have one here — the NNNN
            # and meta/ filters inside the helper already skip
            # game-data JSONs, so pass a non-existent sentinel so
            # nothing real gets excluded.
            _sentinel = folder_path / "__cdumm_nonexistent__"
            _import_sibling_json_patches(
                folder_path, _sentinel, game_dir, db, deltas_dir)
        except Exception as e:
            logger.warning(
                "Compound-layout sibling JSON scan failed: %s", e)

    return result


def _first_numbered_parent(root: Path, max_depth: int = 4) -> Path | None:
    """Return the directory that directly contains an NNNN 4-digit subfolder.

    Walks down from ``root`` through single-subdir wrappers (e.g. ``files/``)
    up to ``max_depth`` levels. Stops as soon as any NNNN child is found.
    Returns None if no numbered dir is reachable without branching.
    """
    import re as _re
    current = root
    for _ in range(max_depth):
        if not current.is_dir():
            return None
        subdirs = [c for c in current.iterdir() if c.is_dir()]
        for c in subdirs:
            if _re.match(r"^\d{4}$", c.name):
                return current
        # Only descend through unambiguous single-subdir wrappers
        if len(subdirs) == 1:
            current = subdirs[0]
            continue
        return None
    return None


def _find_best_variant(folder_path: Path) -> Path | None:
    """Detect if a folder contains multiple mod variants.

    Returns the best variant root, or None if not a multi-variant mod.
    Variants are either:
      - subdirectories each containing 0.paz + 0.pamt
      - subdirectories each containing at least one NNNN/ game directory
        (may be nested under e.g. ``<variant>/files/0000/``)

    When the variant wraps its content under an extra directory, the returned
    path is the inner folder that directly contains the NNNN/ subdirs — so the
    caller can treat it as a mod root without additional descent.
    """
    # Tier 1: variants with 0.paz + 0.pamt (original JSON MM style bundles)
    variants: list[Path] = []
    for sub in folder_path.iterdir():
        if not sub.is_dir():
            continue
        has_paz = list(sub.rglob("0.paz"))
        has_pamt = list(sub.rglob("0.pamt"))
        if has_paz and has_pamt:
            variants.append(sub)

    if len(variants) >= 2:
        variants.sort(key=lambda p: p.name)
        chosen = variants[-1]
        logger.info("Found %d PAZ variants: %s. Picking: %s",
                    len(variants), [v.name for v in variants], chosen.name)
        return chosen

    # Tier 2: variants by NNNN directory presence (Crimson Browser-style nests)
    nnnn_variants: list[tuple[Path, Path]] = []  # (variant_subdir, content_root)
    for sub in folder_path.iterdir():
        if not sub.is_dir():
            continue
        content_root = _first_numbered_parent(sub)
        if content_root is not None:
            nnnn_variants.append((sub, content_root))

    if len(nnnn_variants) >= 2:
        nnnn_variants.sort(key=lambda t: t[0].name)
        chosen_sub, chosen_root = nnnn_variants[-1]
        logger.info("Found %d NNNN variants: %s. Picking: %s (content root: %s)",
                    len(nnnn_variants),
                    [v.name for v, _ in nnnn_variants],
                    chosen_sub.name,
                    chosen_root.relative_to(folder_path))
        return chosen_root

    return None


def import_from_script(
    script_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Import a mod by running a script in a sandbox and capturing the diff."""
    mod_name = script_path.stem
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as sandbox:
        sandbox_path = Path(sandbox)

        # Copy game files the script might modify into sandbox
        # Copy all PAZ/PAMT files (script might target any of them)
        for dir_name in [f"{i:04d}" for i in range(33)]:
            src_dir = game_dir / dir_name
            if src_dir.exists():
                dst_dir = sandbox_path / dir_name
                dst_dir.mkdir(exist_ok=True)
                for f in src_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in (".paz", ".pamt"):
                        shutil.copy2(f, dst_dir / f.name)

        # Copy meta directory
        meta_src = game_dir / "meta"
        if meta_src.exists():
            meta_dst = sandbox_path / "meta"
            shutil.copytree(meta_src, meta_dst)

        # Copy the script into sandbox
        shutil.copy2(script_path, sandbox_path / script_path.name)

        # Execute the script
        suffix = script_path.suffix.lower()
        if suffix == ".bat":
            cmd = ["cmd.exe", "/c", script_path.name]
        elif suffix == ".py":
            cmd = ["py", "-3", script_path.name]
        else:
            result.error = f"Unsupported script type: {suffix}"
            return result

        try:
            _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                cmd,
                cwd=str(sandbox_path),
                timeout=SCRIPT_TIMEOUT,
                capture_output=True,
                text=True,
                creationflags=_no_window,
            )
            if proc.returncode != 0:
                logger.warning("Script exited with code %d: %s", proc.returncode, proc.stderr[:500])
        except subprocess.TimeoutExpired:
            result.error = f"Script timed out after {SCRIPT_TIMEOUT} seconds"
            return result
        except Exception as e:
            result.error = f"Script execution failed: {e}"
            return result

        # Now diff the sandbox against vanilla
        result = _process_sandbox_diff(sandbox_path, game_dir, db, snapshot, deltas_dir, mod_name)

    return result


def import_from_bsdiff(
    patch_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Import a mod distributed as a bsdiff patch file.

    Auto-detects which game file the patch targets by trying to apply it
    against each file in the snapshot. Uses the bsdiff output size to
    narrow candidates first (fast), then tries actual patching.
    """
    import struct
    import bsdiff4

    mod_name = patch_path.stem
    result = ModImportResult(mod_name)

    delta_bytes = patch_path.read_bytes()

    # Validate it's actually a bsdiff
    if not delta_bytes[:8] == b"BSDIFF40":
        result.error = "Not a valid bsdiff4 patch file."
        return result

    # Read expected output size from bsdiff header (offset 16, 8 bytes LE)
    new_size = struct.unpack("<q", delta_bytes[16:24])[0]
    logger.info("bsdiff patch '%s': expected output size = %d bytes", mod_name, new_size)

    # Find the target game file by trying to apply the patch.
    # First, narrow candidates by checking which files exist in the snapshot.
    # Then try applying the patch — only the correct source file will succeed.
    cursor = db.connection.execute("SELECT file_path, file_size FROM snapshots")
    candidates = cursor.fetchall()

    target_path = None
    patched_bytes = None

    # Try filename-encoded path first (e.g., "0035_0.paz.bsdiff" → "0035/0.paz")
    stem = patch_path.stem
    # Handle double extension like "0035_0.paz.bsdiff" where stem is "0035_0.paz"
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    encoded_path = stem.replace("_", "/")
    for file_path, file_size in candidates:
        if file_path == encoded_path:
            game_file = game_dir / file_path.replace("/", os.sep)
            if game_file.exists():
                try:
                    source = game_file.read_bytes()
                    patched_bytes = bsdiff4.patch(source, delta_bytes)
                    target_path = file_path
                    logger.info("bsdiff target found by filename: %s", target_path)
                    break
                except Exception:
                    pass

    # If filename didn't work, try all snapshot files (filter by output size)
    if target_path is None:
        logger.info("Filename match failed, trying %d snapshot files...", len(candidates))
        for file_path, file_size in candidates:
            # Skip files that can't possibly match — bsdiff patches typically
            # produce output close to the original size
            if file_size is not None and abs(file_size - new_size) > file_size * 0.5:
                continue

            game_file = game_dir / file_path.replace("/", os.sep)
            if not game_file.exists():
                continue

            try:
                source = game_file.read_bytes()
                patched_bytes = bsdiff4.patch(source, delta_bytes)
                target_path = file_path
                logger.info("bsdiff target found by brute-force: %s", target_path)
                break
            except Exception:
                continue

    if target_path is None:
        result.error = (
            "Could not find which game file this patch targets.\n\n"
            "The bsdiff patch didn't match any game file in the snapshot.\n"
            "Make sure your game files are verified through Steam."
        )
        return result

    # Generate our own delta (vanilla → patched) so it goes through the
    # standard apply pipeline with proper byte-range tracking
    vanilla_file = game_dir / "CDMods" / "vanilla" / target_path.replace("/", os.sep)
    if vanilla_file.exists():
        vanilla_bytes = vanilla_file.read_bytes()
    else:
        vanilla_bytes = (game_dir / target_path.replace("/", os.sep)).read_bytes()

    our_delta = generate_delta(vanilla_bytes, patched_bytes)
    byte_ranges = get_changed_byte_ranges(vanilla_bytes, patched_bytes)

    # Store mod in database
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority, source_path) VALUES (?, ?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority, str(patch_path)),
    )
    mod_id = cursor.lastrowid

    safe_name = target_path.replace("/", "_") + ".delta"
    delta_dest = deltas_dir / str(mod_id) / safe_name
    save_delta(our_delta, delta_dest)

    for bs, be in byte_ranges:
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
            "VALUES (?, ?, ?, ?, ?)",
            (mod_id, target_path, str(delta_dest), bs, be),
        )

    db.connection.execute(
        "INSERT OR IGNORE INTO mod_vanilla_sizes (mod_id, file_path, vanilla_size) "
        "VALUES (?, ?, ?)",
        (mod_id, target_path, len(vanilla_bytes)),
    )
    db.connection.commit()

    result.changed_files.append({
        "file_path": target_path,
        "delta_path": str(delta_dest),
        "byte_ranges": byte_ranges,
    })
    logger.info("bsdiff import: %s targets %s (%d byte ranges)",
                mod_name, target_path, len(byte_ranges))
    return result


def import_from_natt_format_3(
    json_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Importer for NattKh's Format 3 (field-names + intents).

    Parses the file, validates each intent against the PABGB schema
    + community-curated field_schema, and surfaces a precise summary
    of what we can and can't apply. Wires through to the existing
    semantic-merge pipeline only when there's at least one supported
    intent — otherwise surfaces the validator's grouped skip reasons
    so users know exactly why the mod wasn't applied.

    Variable-length array intents (e.g., dropsetinfo._list /
    "drops") and Pearl Abyss tagged-primitive fields without a
    field_schema entry can't be applied yet — same gap JMM v9.9.3
    has on dropsetinfo (JMM ships zero code for that table beyond a
    directory mapping). The validator surfaces the gap clearly so
    users can decide whether to wait or use an alternate format.
    """
    name = json_path.stem
    title = name
    try:
        from cdumm.engine.format3_handler import (
            parse_format3_mod, validate_intents,
        )
        target, intents = parse_format3_mod(json_path)
        try:
            import json as _json
            with open(json_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            title = (data.get("modinfo") or {}).get("title") or name
        except Exception:
            pass
    except ValueError as e:
        # Malformed Format 3 file — give the user the loader's
        # message verbatim, it already names what's wrong.
        result = ModImportResult(name)
        result.error = f"Invalid Format 3 mod: {e}"
        return result
    except Exception as e:
        result = ModImportResult(name)
        result.error = f"Failed to parse Format 3 mod: {e}"
        return result

    validation = validate_intents(target, intents)
    result = ModImportResult(title)

    # No supported intents → surface the skip reasons, no DB row.
    # Creating a row would put the mod in CDUMM's list as 'imported'
    # but Apply would do nothing — worse UX than not importing.
    if not validation.supported:
        result.error = (
            f"NattKh Format 3 mod targeting {target}: none of the "
            f"{len(intents)} intent(s) can be applied yet.\n\n"
            f"{validation.summary()}\n\n"
            f"Workaround: drop NattKh's offset-based JSON variant "
            f"if they ship one. That format works in CDUMM today."
        )
        return result

    # Some or all intents are applicable → persist the mod so the
    # apply pipeline can process it. Mirrors import_json_fast
    # (json_patch_handler.py) but for the Format 3 file shape.
    persist_outcome = _persist_format3_mod(
        json_path=json_path, target=target, mod_name=title,
        modinfo=(data.get("modinfo") or {}),
        game_dir=game_dir, db=db, existing_mod_id=existing_mod_id,
    )
    if persist_outcome is None:
        # Persistence failed — couldn't resolve target file in PAMTs
        result.error = (
            f"NattKh Format 3 mod targeting {target}: "
            f"validated {len(validation.supported)} applicable "
            f"intent(s), but the target file '{target}' couldn't be "
            f"located in your game's PAZ archives. Make sure the "
            f"target name matches a real game data file."
        )
        return result

    result.mod_id = persist_outcome["mod_id"]
    result.changed_files = persist_outcome["changed_files"]

    if validation.skipped:
        result.info = (
            f"Format 3 mod imported: {len(validation.supported)} "
            f"intent(s) ready to apply, {len(validation.skipped)} "
            f"can't yet:\n\n{validation.summary()}"
        )
    else:
        result.info = (
            f"Format 3 mod imported: all {len(intents)} intent(s) "
            f"on {target} ready to apply."
        )
    return result


def _persist_format3_mod(
    json_path: Path, target: str, mod_name: str,
    modinfo: dict, game_dir: Path, db: Database,
    existing_mod_id: int | None,
) -> dict | None:
    """Persist a Format 3 mod: store JSON + create mods row + lightweight
    mod_deltas rows. Returns ``{mod_id, changed_files}`` or None when the
    target file can't be resolved in the game's PAMT index."""
    from cdumm.engine.json_patch_handler import (
        _derive_pamt_dir, _find_pamt_entry,
    )

    # Resolve target into a PAMT entry so we know which PAZ it lives in.
    # Required for mod_deltas (file_path / byte_start / byte_end) and so
    # the apply pipeline's vanilla extractor can find it.
    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir
    entry = _find_pamt_entry(target, vanilla_dir)
    if entry is None:
        entry = _find_pamt_entry(target, game_dir)
    if entry is None:
        return None
    pamt_dir = _derive_pamt_dir(entry.paz_file)
    if not pamt_dir:
        return None
    paz_filename = Path(entry.paz_file).name
    paz_file_path = f"{pamt_dir}/{paz_filename}"

    # Store the Format 3 JSON in CDMods/mods/. Reuse the same name-
    # sanitization import_json_fast uses (Windows reserved chars + a
    # short hash to disambiguate post-sanitize collisions).
    mods_dir = game_dir / "CDMods" / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    import re as _re_fn
    import hashlib as _hash
    safe = _re_fn.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", mod_name).strip()
    if not safe:
        safe = "mod"
    if safe != mod_name:
        suffix = _hash.sha1(
            mod_name.encode("utf-8", errors="replace")).hexdigest()[:8]
        safe = f"{safe}_{suffix}"
    json_dest = mods_dir / f"{safe}.json"
    import shutil as _shutil
    # Skip the copy when source already lives at the destination —
    # this happens during Reimport-from-source for Format 3 mods,
    # where the stored source_path IS the previously-archived copy
    # in CDMods/mods/. Without this guard, shutil.copy2 attempts a
    # copy-to-self and fails with WinError 32 on Windows whenever
    # any process (CDUMM's apply worker, Defender) holds the file.
    # Bug from Matrixz on Nexus 2026-04-28.
    try:
        is_same = (json_path.resolve() == json_dest.resolve())
    except OSError:
        is_same = False
    if not is_same:
        _shutil.copy2(json_path, json_dest)

    # Game version stamp — matches import_json_fast convention so the
    # outdated-mod guard works for Format 3 mods too.
    game_ver_hash = None
    try:
        from cdumm.engine.version_detector import detect_game_version
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        pass

    author = modinfo.get("author")
    version = modinfo.get("version")
    description = modinfo.get("description")

    if existing_mod_id:
        mod_id = existing_mod_id
        db.connection.execute(
            "DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        db.connection.execute(
            "UPDATE mods SET json_source = ?, "
            "game_version_hash = ?, disabled_patches = NULL "
            "WHERE id = ?",
            (str(json_dest), game_ver_hash, mod_id),
        )
    else:
        priority = db.connection.execute(
            "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods"
        ).fetchone()[0]
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, "
            "version, description, game_version_hash, json_source) "
            "VALUES (?, 'paz', ?, ?, ?, ?, ?, ?)",
            (prettify_mod_name(mod_name), priority, author, version,
             description, game_ver_hash, str(json_dest)),
        )
        mod_id = cursor.lastrowid

    # One lightweight mod_deltas row per target file. delta_path is
    # empty (no actual delta on disk — apply phase derives bytes from
    # vanilla via the Format 3 expansion in apply_engine).
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, entry_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mod_id, paz_file_path, "",
         entry.offset, entry.offset + entry.comp_size, target),
    )
    db.connection.commit()

    return {
        "mod_id": mod_id,
        "changed_files": [{
            "file_path": paz_file_path,
            "delta_path": "",
            "byte_start": entry.offset,
            "byte_end": entry.offset + entry.comp_size,
        }],
    }


def import_from_json_patch(
    json_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a JSON byte-patch file.

    Uses mount-time patching: stores the JSON file and applies patches
    from vanilla at Apply time. No PAZ extraction at import = instant import.

    Falls back to ENTR delta import if mount-time setup fails.
    """
    patch_data = detect_json_patch(json_path if json_path.is_file() else json_path)
    if patch_data is None:
        result = ModImportResult(json_path.stem)
        result.error = "Not a valid JSON patch mod."
        return result

    modinfo = _json_mod_modinfo(patch_data)
    mod_name = _json_mod_display_name(patch_data, json_path.stem)

    # Mount-time fast import: just store JSON, patches applied at Apply time
    mods_dir = game_dir / "CDMods" / "mods"
    fast_result = import_json_fast(
        patch_data, game_dir, db, mods_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    if fast_result is not None and fast_result.get("changed_files"):
        result = ModImportResult(mod_name)
        result.changed_files = fast_result["changed_files"]
        result.mod_id = fast_result.get("mod_id")
        if patch_data.get("patches"):
            _store_json_patches(db, result, patch_data, game_dir)
        return result

    # Fallback: ENTR delta import (if fast import failed, e.g. game file not found)
    if fast_result is None:
        logger.info("Fast import failed for '%s', falling back to ENTR import", mod_name)
        from cdumm.engine.json_patch_handler import import_json_as_entr
        entr_result = import_json_as_entr(
            patch_data, game_dir, db, deltas_dir, mod_name,
            existing_mod_id=existing_mod_id, modinfo=modinfo)

        if entr_result is None:
            result = ModImportResult(mod_name)
            result.error = "Failed to apply JSON patches to game files."
            return result

        if entr_result.get("version_mismatch"):
            result = ModImportResult(mod_name)
            game_ver = entr_result.get("game_version", "unknown")
            mismatched = entr_result.get("mismatched", 0)
            result.error = (
                f"This mod is incompatible with the current game version. "
                f"{mismatched} byte patches don't match. The game data has "
                f"changed since this mod was created (mod targets version "
                f"{game_ver}). The mod author needs to update it.")
            return result

        if not entr_result["changed_files"]:
            result = ModImportResult(mod_name)
            result.error = (
                "This mod's changes are already present in your game files. "
                "Nothing to apply.")
            return result

        result = ModImportResult(mod_name)
        result.changed_files = entr_result["changed_files"]
        if patch_data.get("patches"):
            _store_json_patches(db, result, patch_data, game_dir)
        return result

    # fast_result returned but empty changed_files
    result = ModImportResult(mod_name)
    result.error = (
        "This mod's changes are already present in your game files. "
        "Nothing to apply.")
    return result


def _store_json_patches(db: Database, result, patch_data: dict, game_dir: Path) -> None:
    """Store original JSON patch data in mod_deltas for three-way merge.

    Maps each game_file in the JSON to its mod_deltas row via PAMT lookup,
    then stores the changes array as json_patches.
    """
    import json
    from cdumm.engine.json_patch_handler import _find_pamt_entry

    # Get the mod_id from the result's changed files
    if not result.changed_files:
        return

    # Find mod_id from the first delta
    first_delta = result.changed_files[0].get("delta_path")
    if not first_delta:
        return
    row = db.connection.execute(
        "SELECT mod_id FROM mod_deltas WHERE delta_path = ? LIMIT 1",
        (first_delta,)).fetchone()
    if not row:
        return
    mod_id = row[0]

    vanilla_dir = game_dir / "CDMods" / "vanilla"
    base_dir = vanilla_dir if vanilla_dir.exists() else game_dir

    for patch in patch_data.get("patches", []):
        game_file = patch.get("game_file")
        changes = patch.get("changes")
        if not game_file or not changes:
            continue

        # Find which PAZ file contains this game file
        entry = _find_pamt_entry(game_file, base_dir)
        if not entry:
            continue

        # The PAZ file path in mod_deltas
        pamt_dir = os.path.basename(os.path.dirname(entry.paz_file))
        paz_file_path = f"{pamt_dir}/{entry.paz_index}.paz"

        # Store the patches JSON on the matching mod_deltas row
        patches_json = json.dumps({
            "game_file": game_file,
            "entry_path": entry.path,
            "changes": changes,
        })

        # Update the specific mod_deltas row for this entry
        # (scoped to entry_path to avoid overwriting other entries in same PAZ)
        updated = db.connection.execute(
            "UPDATE mod_deltas SET json_patches = ? "
            "WHERE mod_id = ? AND entry_path = ?",
            (patches_json, mod_id, entry.path),
        ).rowcount
        if not updated:
            # Fallback for mods without entry_path (old SPRS deltas)
            db.connection.execute(
                "UPDATE mod_deltas SET json_patches = ? "
                "WHERE mod_id = ? AND file_path LIKE ? AND json_patches IS NULL",
                (patches_json, mod_id, f"{pamt_dir}/%"),
            )

    db.connection.commit()
    logger.info("Stored JSON patch data for mod %d (%d patches)",
                mod_id, len(patch_data.get("patches", [])))


def _process_extracted_files(
    extracted_dir: Path,
    game_dir: Path,
    db: Database,
    snapshot: SnapshotManager,
    deltas_dir: Path,
    mod_name: str,
    existing_mod_id: int | None = None,
    modinfo: dict | None = None,
) -> ModImportResult:
    """Common logic for zip and folder imports: match files, generate deltas, store.

    If existing_mod_id is provided, reuses that mod entry (for updates).
    """
    import time as _time
    _stage_t0 = _time.perf_counter()
    _stage_log: list[tuple[str, float]] = []  # (stage, ms)

    def _stage(name: str) -> None:
        nonlocal _stage_t0
        now = _time.perf_counter()
        _stage_log.append((name, (now - _stage_t0) * 1000))
        _stage_t0 = now

    result = ModImportResult(mod_name)

    _emit_progress(2, f"Matching files for {mod_name}...")
    matches = _match_game_files(extracted_dir, game_dir, snapshot)
    _stage("match_game_files")
    if not matches:
        # Build a short inventory of what we DID see so the user (and
        # we, when they paste the error) can tell what's wrong. Most
        # "unrecognized folder" reports have the mod in a non-standard
        # layout CDUMM can't auto-detect — listing the extensions we
        # found makes it obvious whether the drop was a readme, a
        # loose DDS, a weird JSON schema, etc.
        counts: dict[str, int] = {}
        total = 0
        for p in extracted_dir.rglob("*"):
            if p.is_file():
                total += 1
                ext = p.suffix.lower() or "(no-ext)"
                counts[ext] = counts.get(ext, 0) + 1
                if total > 500:  # cap so huge folders don't stall
                    break
        summary = ", ".join(f"{n} {ext}" for ext, n in sorted(
            counts.items(), key=lambda kv: -kv[1])[:6])
        result.error = (
            "This doesn't look like a CDUMM-supported mod. No PAZ/PAMT "
            "files, no valid JSON patch, no recognised Crimson Browser "
            f"manifest, and no DDS texture pack.\nFound {total} file(s): "
            f"{summary}\nIf this is a new format, paste the mod's "
            "Nexus page in the Bug Report card so we can add support."
        )
        return result

    new_count = sum(1 for _, _, is_new in matches if is_new)
    mod_count = sum(1 for _, _, is_new in matches if not is_new)
    _emit_progress(5, f"Matched {len(matches)} files ({mod_count} existing, {new_count} new)")
    logger.info("Matched %d files (%d existing, %d new)", len(matches), mod_count, new_count)

    # Run health check on mod files before importing
    try:
        from cdumm.engine.mod_health_check import check_mod_health, auto_fix_matches
        mod_file_map = {rel: abs_path for rel, abs_path, _ in matches}
        result.health_issues = check_mod_health(mod_file_map, game_dir)
        if result.health_issues:
            critical = [i for i in result.health_issues if i.severity == "critical"]
            logger.info("Health check: %d issues (%d critical)",
                        len(result.health_issues), len(critical))
            # Auto-fix: filter out broken files from import
            fixed = auto_fix_matches(
                [(rel, p) for rel, p, _ in matches],
                result.health_issues, game_dir)
            # Rebuild matches with is_new flags preserved
            fixed_set = {rel for rel, _ in fixed}
            matches = [(rel, p, is_new) for rel, p, is_new in matches if rel in fixed_set]
            logger.info("After auto-fix: %d files to import", len(matches))
    except Exception as e:
        logger.warning("Health check failed (non-fatal): %s", e)
    _stage("health_check")

    force_inplace = 1 if (modinfo and modinfo.get("force_inplace")) else 0
    # Use prettified name for DB storage
    mod_name = result.name
    # Override with modinfo name if provided
    if modinfo and modinfo.get("name"):
        mod_name = prettify_mod_name(modinfo["name"])

    raw_cm = modinfo.get("conflict_mode", "normal") if modinfo else "normal"
    conflict_mode = (raw_cm or "").strip().lower() if isinstance(raw_cm, str) else "normal"
    if conflict_mode not in ("normal", "override"):
        if raw_cm and raw_cm != "normal":
            logger.warning(
                "Invalid conflict_mode %r in modinfo.json, defaulting to 'normal'",
                raw_cm)
        conflict_mode = "normal"
    # Quick win F6: normalise language code to a short lowercase string
    # so EN / en don't bucket separately and "english" doesn't overflow
    # the 55px badge.
    raw_lang = modinfo.get("target_language") if modinfo else None
    if isinstance(raw_lang, str):
        raw_lang = raw_lang.strip().lower()
        if not raw_lang:
            target_language = None
        elif len(raw_lang) > 12:
            # 12 chars covers extended BCP47 tags like `zh-Hant-TW`
            # (10) and `sr-Latn-RS` (10) without truncation.
            logger.warning(
                "target_language %r too long, truncating to 12 chars",
                raw_lang)
            target_language = raw_lang[:12]
        else:
            target_language = raw_lang
    else:
        target_language = None

    # Stamp with current game version so we can detect outdated mods later
    game_ver_hash = None
    try:
        from cdumm.engine.version_detector import detect_game_version
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        pass

    if existing_mod_id is not None:
        mod_id = existing_mod_id
        # Update metadata if modinfo provided
        if modinfo:
            db.connection.execute(
                "UPDATE mods SET author=?, version=?, description=?, force_inplace=?, "
                "game_version_hash=?, conflict_mode=?, target_language=? WHERE id=?",
                (modinfo.get("author"), modinfo.get("version"), modinfo.get("description"),
                 force_inplace, game_ver_hash, conflict_mode, target_language, mod_id),
            )
        elif game_ver_hash:
            db.connection.execute(
                "UPDATE mods SET game_version_hash=? WHERE id=?",
                (game_ver_hash, mod_id))
    else:
        # Store mod in database
        priority = _next_priority(db)
        author = modinfo.get("author") if modinfo else None
        version = modinfo.get("version") if modinfo else None
        description = modinfo.get("description") if modinfo else None
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, description, "
            "force_inplace, game_version_hash, conflict_mode, target_language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mod_name, "paz", priority, author, version, description, force_inplace,
             game_ver_hash, conflict_mode, target_language),
        )
        mod_id = cursor.lastrowid

    result.mod_id = mod_id

    # Archive mod source files for auto-reimport after game updates.
    # Copy the extracted files to CDMods/sources/<mod_id>/ so the mod
    # can be re-imported without the user providing the original files.
    # Skip when reusing an existing mod (e.g., second pass of mixed-format
    # import) — sources were already archived from the first pass.
    if existing_mod_id is None:
        sources_dir = deltas_dir.parent / "sources" / str(mod_id)
        try:
            if sources_dir.exists():
                shutil.rmtree(sources_dir)
            shutil.copytree(extracted_dir, sources_dir, dirs_exist_ok=True)
            db.connection.execute(
                "UPDATE mods SET source_path = ? WHERE id = ?",
                (str(sources_dir), mod_id))
            logger.info("Archived mod source: %s -> %s", mod_name, sources_dir)
        except Exception as e:
            logger.warning("Failed to archive mod source: %s", e)

    _stage("db_setup")
    total_matches = len(matches)
    _paz_entr_handled: set[str] = set()  # PAZ/PAMT files handled by entry-level import
    _fp_sub_timings: dict[str, float] = {
        "is_new_file": 0.0,       # new-file copy path
        "paz_entr_import": 0.0,   # large PAZ via ENTR decomposition
        "fast_track_copy": 0.0,   # FULL_COPY for large different-size files
        "streaming_sparse": 0.0,  # streaming sparse delta (Rust sparse-diff)
        "small_bsdiff": 0.0,      # standard bsdiff for files <10MB
        "skipped_entr": 0.0,      # PAMT/PAZ already handled by ENTR
        "other": 0.0,
    }
    for match_idx, (rel_path, extracted_path, is_new) in enumerate(matches):
        pct = int((match_idx / max(total_matches, 1)) * 90) + 5
        size_mb = extracted_path.stat().st_size / 1048576
        _emit_progress(pct, f"Processing {rel_path} ({size_mb:.0f} MB)...")
        time.sleep(0)  # yield GIL so GUI stays responsive
        _iter_start = time.perf_counter()
        _iter_branch = "other"
        try:
            # Skip files already handled by entry-level PAZ decomposition
            if rel_path in _paz_entr_handled:
                _iter_branch = "skipped_entr"
                logger.info("Skipping %s — handled by entry-level PAZ import", rel_path)
                continue

            if is_new:
                _iter_branch = "is_new_file"
                # New file — store full copy, no delta needed
                safe_name = rel_path.replace("/", "_") + ".newfile"
                delta_path = deltas_dir / str(mod_id) / safe_name
                delta_path.parent.mkdir(parents=True, exist_ok=True)
                # Auto-fix PAMT CRC for new files too
                if rel_path.endswith(".pamt"):
                    raw = extracted_path.read_bytes()
                    if len(raw) >= 12:
                        raw = _verify_and_fix_pamt_crc(raw, rel_path)
                        delta_path.parent.mkdir(parents=True, exist_ok=True)
                        delta_path.write_bytes(raw)
                    else:
                        shutil.copy2(extracted_path, delta_path)
                else:
                    shutil.copy2(extracted_path, delta_path)

                file_size = extracted_path.stat().st_size
                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (mod_id, rel_path, str(delta_path), 0, file_size),
                )

                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "is_new": True,
                })
                logger.info("Stored new file: %s (%d bytes)", rel_path, file_size)
                continue

            # Use vanilla backup if available (accurate base for delta),
            # fall back to current game file
            vanilla_backup = game_dir / "CDMods" / "vanilla" / rel_path.replace("/", os.sep)
            vanilla_path = game_dir / rel_path.replace("/", os.sep)
            vanilla_source = vanilla_backup if vanilla_backup.exists() else vanilla_path
            if not vanilla_source.exists():
                logger.warning("Vanilla file not found for %s, skipping", rel_path)
                continue

            mod_size = extracted_path.stat().st_size
            van_size = vanilla_source.stat().st_size

            # ── Entry-level decomposition for PAZ files ──────────────
            # Instead of storing byte-level diffs of the entire PAZ, decompose
            # into ENTR deltas per PAMT entry. This way two mods modifying
            # different entries in the same PAZ compose correctly.
            if rel_path.endswith(".paz") and mod_size > 10 * 1024 * 1024:
                _iter_branch = "paz_entr_import"
                entr_ok = _try_paz_entry_import(
                    extracted_path, vanilla_source, rel_path,
                    extracted_dir, game_dir, mod_id, db, deltas_dir, result)
                if entr_ok:
                    # Also mark the corresponding PAMT as handled — the apply
                    # engine rebuilds it from ENTR delta updates
                    _paz_entr_handled.add(rel_path)
                    pamt_rel = rel_path.rsplit("/", 1)[0] + "/0.pamt"
                    _paz_entr_handled.add(pamt_rel)
                    continue
                # For large PAZ files (>100MB), don't fall back to FULL_COPY.
                # A 900MB full copy is the old broken format. Skip the file —
                # the mod will show "outdated" and need reimporting from a
                # fresh download, or the mod author needs to update it.
                if mod_size > 100 * 1024 * 1024:
                    logger.warning(
                        "Skipping %s (%.0f MB) — ENTR decomposition failed, "
                        "refusing to store full PAZ copy. Mod may be outdated.",
                        rel_path, mod_size / 1048576)
                    continue

            # ── Fast-track for different-size large files ─────────────
            # When the mod file is a different size from vanilla, it's a true
            # full replacement — store as FULL_COPY with streaming I/O.
            # Same-size files use the standard sparse delta path so multiple
            # mods can compose their changes at different byte ranges.
            FAST_TRACK_THRESHOLD = 10 * 1024 * 1024  # 10MB

            if mod_size > FAST_TRACK_THRESHOLD and mod_size != van_size:
                _iter_branch = "fast_track_copy"
                from cdumm.engine.delta_engine import FULL_COPY_MAGIC
                safe_name = rel_path.replace("/", "_") + ".bsdiff"
                delta_path = deltas_dir / str(mod_id) / safe_name
                delta_path.parent.mkdir(parents=True, exist_ok=True)

                with open(delta_path, "wb") as out:
                    out.write(FULL_COPY_MAGIC)
                    with open(extracted_path, "rb") as inp:
                        shutil.copyfileobj(inp, out, length=1024 * 1024)

                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                    "byte_start, byte_end, is_new) VALUES (?, ?, ?, 0, ?, 0)",
                    (mod_id, rel_path, str(delta_path), mod_size),
                )
                db.connection.execute(
                    "INSERT OR IGNORE INTO mod_vanilla_sizes "
                    "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                    (mod_id, rel_path, van_size),
                )
                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "byte_ranges": [(0, mod_size)],
                })
                logger.info("Fast-track import: %s (%.1f MB, different size)",
                            rel_path, mod_size / 1048576)
                continue

            # ── Streaming sparse delta for large same-size files ─────
            # For files >10MB with same size, generate SPRS delta by streaming
            # both files in 1MB chunks. Never loads the full files into memory.
            # This handles 912MB PAZ files in ~2 seconds with ~2MB RAM.
            if mod_size > FAST_TRACK_THRESHOLD and mod_size == van_size:
                _iter_branch = "streaming_sparse"
                import struct
                from cdumm.engine.delta_engine import SPARSE_MAGIC

                # Use Rust-native byte-diff scanner when available — replaces
                # a Python byte-by-byte loop that ran 1M iterations per 1MB
                # chunk. Measured 34x faster on 10MB with scattered diffs.
                try:
                    from cdumm_native import find_sparse_diffs as _native_diffs
                except ImportError:
                    _native_diffs = None

                CHUNK = 1024 * 1024
                patches: list[tuple[int, bytes]] = []
                identical = True

                with open(vanilla_source, "rb") as fv, open(extracted_path, "rb") as fm:
                    offset = 0
                    while True:
                        cv = fv.read(CHUNK)
                        cm = fm.read(CHUNK)
                        if not cv:
                            break
                        if cv != cm:
                            identical = False
                            if _native_diffs is not None:
                                patches.extend(_native_diffs(cv, cm, offset))
                            else:
                                # Python fallback — byte-by-byte scan.
                                in_diff = False
                                diff_start = 0
                                for i in range(len(cv)):
                                    if cv[i] != cm[i]:
                                        if not in_diff:
                                            diff_start = offset + i
                                            in_diff = True
                                    else:
                                        if in_diff:
                                            patches.append((diff_start, cm[diff_start - offset:i]))
                                            in_diff = False
                                if in_diff:
                                    patches.append((diff_start, cm[diff_start - offset:]))
                        offset += len(cv)

                if identical:
                    logger.debug("File %s identical to vanilla, skipping", rel_path)
                    continue

                # Build SPRS delta
                buf = bytearray(SPARSE_MAGIC)
                buf += struct.pack("<I", len(patches))
                for off, data in patches:
                    buf += struct.pack("<QI", off, len(data))
                    buf += data
                delta_bytes = bytes(buf)

                safe_name = rel_path.replace("/", "_") + ".bsdiff"
                delta_path = deltas_dir / str(mod_id) / safe_name
                save_delta(delta_bytes, delta_path)

                import hashlib
                # Use streaming to get byte ranges without re-reading
                for off, data in patches:
                    bs, be = off, off + len(data)
                    db.connection.execute(
                        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                        "byte_start, byte_end, is_new) VALUES (?, ?, ?, ?, ?, 0)",
                        (mod_id, rel_path, str(delta_path), bs, be),
                    )

                db.connection.execute(
                    "INSERT OR IGNORE INTO mod_vanilla_sizes "
                    "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                    (mod_id, rel_path, van_size),
                )
                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "byte_ranges": [(off, off + len(d)) for off, d in patches],
                })
                logger.info("Streaming delta: %s (%.1f MB, %d patches, %d bytes changed)",
                            rel_path, mod_size / 1048576, len(patches),
                            sum(len(d) for _, d in patches))
                continue

            # ── Standard delta path for small files (<10MB) ───────────
            _iter_branch = "small_bsdiff"
            vanilla_bytes = vanilla_source.read_bytes()
            modified_bytes = extracted_path.read_bytes()

            # Auto-fix PAMT CRC if it's wrong (common mod authoring mistake)
            if rel_path.endswith(".pamt") and len(modified_bytes) >= 12:
                modified_bytes = _verify_and_fix_pamt_crc(modified_bytes, rel_path)
                # B1: validate the PAMT can actually be parsed before
                # saving a delta. A corrupt pamt here will break apply
                # forever for this mod — surface it at import time so
                # the user sees the failure during import, not during
                # a 7-minute apply stall. See _validate_modified_pamt
                # docstring for the v3.1.7.1 hotfix history.
                _validate_modified_pamt(modified_bytes, rel_path)

            if vanilla_bytes == modified_bytes:
                logger.debug("File %s is identical to vanilla, skipping", rel_path)
                continue

            # Generate delta
            delta_bytes = generate_delta(vanilla_bytes, modified_bytes)

            # Get byte ranges
            byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

            # Save delta to disk
            safe_name = rel_path.replace("/", "_") + ".bsdiff"
            delta_path = deltas_dir / str(mod_id) / safe_name
            save_delta(delta_bytes, delta_path)

            # Store each byte range with a hash of the vanilla bytes at that range.
            import hashlib
            for byte_start, byte_end in byte_ranges:
                vanilla_chunk = vanilla_bytes[byte_start:byte_end]
                vh = hashlib.sha256(vanilla_chunk).hexdigest()[:16]
                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new, vanilla_hash) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (mod_id, rel_path, str(delta_path), byte_start, byte_end, vh),
                )

            db.connection.execute(
                "INSERT OR IGNORE INTO mod_vanilla_sizes (mod_id, file_path, vanilla_size) "
                "VALUES (?, ?, ?)",
                (mod_id, rel_path, len(vanilla_bytes)),
            )

            result.changed_files.append({
                "file_path": rel_path,
                "delta_path": str(delta_path),
                "byte_ranges": byte_ranges,
            })
        except Exception as e:
            logger.error("Failed to process %s: %s", rel_path, e, exc_info=True)
            result.error = f"Failed to process {rel_path}: {e}"
            return result
        finally:
            _fp_sub_timings[_iter_branch] = (
                _fp_sub_timings.get(_iter_branch, 0.0)
                + (time.perf_counter() - _iter_start)
            )

    _stage("file_processing_loop")
    # Per-branch breakdown of file_processing_loop — emits only when
    # the stage was >=1s and any single branch took >=200ms, so fast
    # mods don't bloat the log. Sorted by cost descending.
    _fp_total = sum(_fp_sub_timings.values())
    if _fp_total >= 1.0:
        import sys as _fp_sys
        _hot = sorted(_fp_sub_timings.items(), key=lambda kv: kv[1], reverse=True)
        _parts = " ".join(
            f"{n}={int(dt * 1000)}ms" for n, dt in _hot if dt >= 0.2
        )
        if _parts:
            print(
                f"[FILE-PROC-TIMING] {mod_name}: total={int(_fp_total * 1000)}ms "
                f"{_parts}",
                file=_fp_sys.stderr,
            )
    # Clean up PAMT byte-range deltas that were created before the corresponding
    # PAZ was decomposed into ENTR deltas. PAMT files sort before PAZ files
    # alphabetically (0.pamt before 4.paz), so the PAMT delta may already exist
    # by the time ENTR decomposition adds it to _paz_entr_handled.
    for handled_path in _paz_entr_handled:
        if handled_path.endswith(".pamt"):
            cursor = db.connection.execute(
                "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ? AND file_path = ? "
                "AND entry_path IS NULL",
                (mod_id, handled_path))
            count = cursor.fetchone()[0]
            if count > 0:
                db.connection.execute(
                    "DELETE FROM mod_deltas WHERE mod_id = ? AND file_path = ? "
                    "AND entry_path IS NULL",
                    (mod_id, handled_path))
                logger.info("Cleaned up %d PAMT byte-range deltas for %s "
                            "(handled by ENTR import)", count, handled_path)
                # Remove from changed_files too
                result.changed_files = [
                    cf for cf in result.changed_files
                    if cf.get("file_path") != handled_path or cf.get("entry_path")]

    db.connection.commit()
    _stage("delta_save_and_db")
    logger.info("Imported mod '%s': %d files changed", mod_name, len(result.changed_files))
    # Per-stage timing breakdown via stderr — surfaces in batch mode
    # so the GUI's _on_stderr handler logs it. Only emit when total
    # mod time was >= 1s (sub-second imports don't need diagnosis).
    _total_ms = sum(ms for _, ms in _stage_log)
    if _total_ms >= 1000:
        breakdown = " ".join(f"{n}={ms:.0f}ms" for n, ms in _stage_log)
        import sys as _s
        print(f"[STAGE-TIMING] {mod_name}: total={_total_ms:.0f}ms {breakdown}",
              file=_s.stderr, flush=True)
    return result


def _process_sandbox_diff(
    sandbox_dir: Path,
    game_dir: Path,
    db: Database,
    snapshot: SnapshotManager,
    deltas_dir: Path,
    mod_name: str,
) -> ModImportResult:
    """Diff sandbox output against vanilla game files and create deltas."""
    result = ModImportResult(mod_name)

    # Store mod in database
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority),
    )
    mod_id = cursor.lastrowid

    # Walk sandbox and compare each file against vanilla
    for f in sandbox_dir.rglob("*"):
        if not f.is_file():
            continue
        # Skip the script itself
        if f.suffix.lower() in (".bat", ".py") and f.parent == sandbox_dir:
            continue

        rel = f.relative_to(sandbox_dir)
        rel_posix = rel.as_posix()

        # Check if this is a known game file
        if snapshot.get_file_hash(rel_posix) is None:
            continue

        vanilla_path = game_dir / str(rel)
        if not vanilla_path.exists():
            continue

        vanilla_bytes = vanilla_path.read_bytes()
        modified_bytes = f.read_bytes()

        if vanilla_bytes == modified_bytes:
            continue

        delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
        byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

        safe_name = rel_posix.replace("/", "_") + ".bsdiff"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_delta(delta_bytes, delta_path)

        for byte_start, byte_end in byte_ranges:
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (mod_id, rel_posix, str(delta_path), byte_start, byte_end),
            )

        result.changed_files.append({
            "file_path": rel_posix,
            "delta_path": str(delta_path),
            "byte_ranges": byte_ranges,
        })

    db.connection.commit()
    logger.info("Script import '%s': %d files changed", mod_name, len(result.changed_files))
    return result


def import_script_live(
    script_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Run a mod script against the real game files, then capture changes.

    Opens a visible cmd/python window so the user can interact with the script
    (e.g., pick options from a menu). After the script finishes, diffs game files
    against the vanilla snapshot and stores the changes as a managed mod.
    """
    mod_name = script_path.stem
    result = ModImportResult(mod_name)

    suffix = script_path.suffix.lower()
    if suffix == ".bat":
        cmd = ["cmd", "/c", f'"{script_path}" & pause']
    elif suffix == ".py":
        cmd = ["py", "-3", str(script_path)]
    else:
        result.error = f"Unsupported script type: {suffix}"
        return result

    vanilla_dir = deltas_dir.parent / "vanilla"
    vanilla_dir.mkdir(parents=True, exist_ok=True)
    from cdumm.engine.snapshot_manager import hash_file as _hash_file

    # Figure out which game files the script might touch by reading its source
    targeted_files = _detect_script_targets(script_path, game_dir)
    logger.info("Script likely targets: %s", targeted_files if targeted_files else "unknown")

    # Back up targeted files BEFORE the script modifies them
    if targeted_files:
        for rel_path in targeted_files:
            _ensure_vanilla_backup(game_dir, vanilla_dir, rel_path)
    else:
        # Can't determine targets — back up all PAMT and PAPGT (small files)
        for dir_name in [f"{i:04d}" for i in range(33)]:
            pamt = f"{dir_name}/0.pamt"
            if (game_dir / dir_name / "0.pamt").exists():
                _ensure_vanilla_backup(game_dir, vanilla_dir, pamt)
        _ensure_vanilla_backup(game_dir, vanilla_dir, "meta/0.papgt")

    # Record pre-script hashes ONLY for files that have backups
    pre_hashes: dict[str, str] = {}
    for f in vanilla_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(vanilla_dir).as_posix()
        game_file = game_dir / rel.replace("/", os.sep)
        if game_file.exists():
            h, _ = _hash_file(game_file)
            pre_hashes[rel] = h

    logger.info("Running script live: %s", script_path)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(script_path.parent),
            shell=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        proc.wait()
        logger.info("Script finished with exit code: %d", proc.returncode)
    except Exception as e:
        result.error = f"Failed to run script: {e}"
        return result

    # Scan for changes — compare current hashes against pre-script state
    logger.info("Scanning for changes after script...")
    changed_files: list[str] = []
    for rel_path, old_hash in pre_hashes.items():
        abs_path = game_dir / rel_path.replace("/", os.sep)
        if not abs_path.exists():
            continue
        new_hash, _ = _hash_file(abs_path)
        if new_hash != old_hash:
            changed_files.append(rel_path)
            # Back up the vanilla version (from pre-script state) if needed
            _ensure_vanilla_backup(game_dir, vanilla_dir, rel_path)

    if not changed_files:
        result.error = "Script ran but no game file changes were detected."
        return result

    logger.info("Script changed %d files: %s", len(changed_files), changed_files)

    # Generate deltas for changed files
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority))
    mod_id = cursor.lastrowid

    for rel_path in changed_files:
        vanilla_path = vanilla_dir / rel_path.replace("/", os.sep)
        current_path = game_dir / rel_path.replace("/", os.sep)

        if not vanilla_path.exists():
            logger.warning("No vanilla backup for %s, skipping", rel_path)
            continue

        vanilla_bytes = vanilla_path.read_bytes()
        modified_bytes = current_path.read_bytes()

        delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
        byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

        safe_name = rel_path.replace("/", "_") + ".bsdiff"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_delta(delta_bytes, delta_path)

        for byte_start, byte_end in byte_ranges:
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (mod_id, rel_path, str(delta_path), byte_start, byte_end),
            )

        result.changed_files.append({
            "file_path": rel_path,
            "delta_path": str(delta_path),
            "byte_ranges": byte_ranges,
        })

    db.connection.commit()
    logger.info("Live script import '%s': %d files changed", mod_name, len(result.changed_files))
    return result


def _detect_script_targets(script_path: Path, game_dir: Path) -> list[str]:
    """Read a script's source code to detect which game files it targets.

    Looks for PAZ directory patterns (0000-0099) and file references,
    including os.path.join style references like ("0009") and bare
    directory name strings.
    """
    import re
    targets: list[str] = []

    try:
        content = script_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return targets

    dirs_found: set[str] = set()

    # Look for PAZ directory references like "0008\0.paz" or "0008/0.paz"
    for match in re.finditer(r'(\d{4})[/\\]+(\d+\.(?:paz|pamt))', content, re.IGNORECASE):
        dir_name = match.group(1)
        file_name = match.group(2)
        rel = f"{dir_name}/{file_name}"
        if (game_dir / dir_name / file_name).exists() and rel not in targets:
            targets.append(rel)
            dirs_found.add(dir_name)

    # Look for bare PAZ directory references like "0009" in quotes
    # (catches os.path.join(game_dir, "0009") style)
    for match in re.finditer(r'["\'](\d{4})["\']', content):
        dir_name = match.group(1)
        dir_path = game_dir / dir_name
        if dir_path.exists() and dir_path.is_dir():
            dirs_found.add(dir_name)

    # Look for meta/0.papgt references
    if re.search(r'meta[/\\]+0\.papgt', content, re.IGNORECASE):
        if (game_dir / "meta" / "0.papgt").exists():
            targets.append("meta/0.papgt")

    # For every directory found, include all PAZ and PAMT files
    for d in sorted(dirs_found):
        dir_path = game_dir / d
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.iterdir()):
            if f.is_file() and f.suffix.lower() in ('.paz', '.pamt'):
                rel = f"{d}/{f.name}"
                if rel not in targets:
                    targets.append(rel)

    if dirs_found and "meta/0.papgt" not in targets:
        if (game_dir / "meta" / "0.papgt").exists():
            targets.append("meta/0.papgt")

    return targets


def _ensure_vanilla_backup(game_dir: Path, vanilla_dir: Path, rel_path: str) -> None:
    """Back up a single game file if not already backed up.

    Always a real copy — hard links are unsafe because script mods can
    modify the game file directly, which would corrupt a hard-linked backup.
    """
    src = game_dir / rel_path.replace("/", os.sep)
    dst = vanilla_dir / rel_path.replace("/", os.sep)
    if not dst.exists() and src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.debug("Backed up vanilla: %s", rel_path)


def import_from_game_scan(
    mod_name: str, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Import a mod by scanning current game files against the vanilla snapshot.

    Use this after the user has manually run a script/installer that modified
    game files directly. Detects all changes and captures them as deltas.
    """
    result = ModImportResult(mod_name)
    changes = snapshot.detect_changes(game_dir)

    if not changes:
        result.error = "No changes detected. Game files match the vanilla snapshot."
        return result

    # Only process modified files (not deleted)
    modified = [(path, change) for path, change in changes if change == "modified"]
    if not modified:
        result.error = "No modified files found (some files may have been deleted)."
        return result

    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority),
    )
    mod_id = cursor.lastrowid

    for rel_path, _ in modified:
        vanilla_path = game_dir / rel_path.replace("/", os.sep)
        # We need the vanilla version — check the vanilla backup dir first
        vanilla_backup = deltas_dir.parent / "vanilla" / rel_path.replace("/", os.sep)

        if vanilla_backup.exists():
            vanilla_bytes = vanilla_backup.read_bytes()
        else:
            # No backup exists — we can't diff without the original
            # Store the snapshot hash so we know what changed
            logger.warning("No vanilla backup for %s, skipping delta generation", rel_path)
            continue

        modified_bytes = vanilla_path.read_bytes()
        if vanilla_bytes == modified_bytes:
            continue

        delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
        byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

        safe_name = rel_path.replace("/", "_") + ".bsdiff"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_delta(delta_bytes, delta_path)

        for byte_start, byte_end in byte_ranges:
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (mod_id, rel_path, str(delta_path), byte_start, byte_end),
            )

        result.changed_files.append({
            "file_path": rel_path,
            "delta_path": str(delta_path),
            "byte_ranges": byte_ranges,
        })

    db.connection.commit()
    logger.info("Game scan import '%s': %d files changed", mod_name, len(result.changed_files))
    return result
