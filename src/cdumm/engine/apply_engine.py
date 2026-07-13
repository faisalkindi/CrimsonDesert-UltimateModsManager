"""Apply engine — composes enabled mod deltas into a valid game state.

Pipeline:
  1. Ensure vanilla range backups exist for all mod-affected files
  2. Read game files, restore vanilla at mod byte ranges
  3. Apply each enabled mod's delta in sequence
  4. Rebuild PAPGT from scratch
  5. Stage all modified files
  6. Atomic commit (transactional I/O)

Vanilla backups are byte-range level (not full file copies) for files with
sparse deltas. Only the specific byte ranges that mods modify are backed up.
Bsdiff deltas use full file backups (but those files are always small).
"""
import logging
import os
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from cdumm.engine.cdmods_paths import get_cdmods_root

if TYPE_CHECKING:
    from cdumm.storage.config import Config


def persist_skip_summary(
    db_connection,
    patch_skips: list[dict],
    participating_mod_ids: set,
) -> None:
    """Write per-mod skip counts to the mods table after Apply.

    For each mod in ``participating_mod_ids``, set:
    - ``last_apply_skipped_count`` = number of patch_skips with that
      mod's id
    - ``last_apply_skip_summary`` = JSON list of {label, reason, file}
      for the badge tooltip, or NULL when count is 0

    Mods NOT in ``participating_mod_ids`` (e.g. disabled this apply)
    are left untouched so their last-known skip state persists in the
    badge until they participate again. Lets the user see the badge
    after disabling a broken mod without it disappearing.

    The skipped-mod-badge work (chunk 2A): without this persistence,
    the apply pipeline emitted a transient warning toast and
    `stamp_enabled_mods_as_current` then cleared the only adjacent
    badge. Mods looked fine afterward even though half their patches
    silently failed.
    """
    import json as _json
    # Tally skips per mod_id. A skip entry can carry either:
    #   _source_mod_id  (single int)  , the v2 / per-mod Format 3 path
    #   _source_mod_ids (list[int])   , whole-table Format 3 merged
    # H3 fix: fan _source_mod_ids out so all contributors get credited.
    by_mod: dict[int, list[dict]] = {}
    for s in patch_skips:
        ids: list[int] = []
        single = s.get("_source_mod_id")
        if single is not None:
            ids.append(int(single))
        plural = s.get("_source_mod_ids")
        if plural:
            ids.extend(int(i) for i in plural)
        if not ids:
            continue
        for mid in ids:
            by_mod.setdefault(mid, []).append(s)

    for mod_id in participating_mod_ids:
        skips = by_mod.get(int(mod_id), [])
        if skips:
            summary = [
                {"label": s.get("label", ""),
                 "reason": s.get("reason", ""),
                 "file": s.get("_target_file", "")}
                for s in skips
            ]
            db_connection.execute(
                "UPDATE mods SET last_apply_skipped_count = ?, "
                "last_apply_skip_summary = ? WHERE id = ?",
                (len(skips), _json.dumps(summary), int(mod_id))
            )
        else:
            db_connection.execute(
                "UPDATE mods SET last_apply_skipped_count = 0, "
                "last_apply_skip_summary = NULL WHERE id = ?",
                (int(mod_id),)
            )
    db_connection.commit()


def log_patch_skips(
    patch_skips: list[dict], limit: int = 15,
) -> tuple[list[str], int]:
    """Format skipped byte-patches, log them at WARNING, and return
    ``(lines, overflow)`` so the caller can reuse the same lines for the
    post-apply InfoBar.

    ``lines`` holds at most ``limit`` ``"  - <label> (expected <hex>,
    got <hex>, <reason>)"`` strings; ``overflow`` is the count beyond it.
    Hex fields are truncated to 32 chars (+ a byte count) so whole-table
    changes — whose expected/actual can be multiple MB of hex — don't
    blow up the message (falobos76, #191 retest).

    Issue #222 (falobos76): the skip detail previously reached only the
    transient InfoBar, so a saved bug report — which tails cdumm.log —
    had no record of WHICH patches were skipped and couldn't be
    diagnosed without the user's screenshot. Logging the same lines the
    InfoBar shows closes that gap and keeps the two sinks in lock-step.
    """
    def _short_hex(h: str) -> str:
        h = h or ""
        if len(h) <= 32:
            return h
        return f"{h[:32]}... ({len(h) // 2:,} bytes)"

    lines = [
        f"  - {s.get('label') or '(unnamed)'}"
        f" (expected {_short_hex(s.get('expected'))}, "
        f"got {_short_hex(s.get('actual'))}, "
        f"{s.get('reason')})"
        for s in patch_skips[:limit]
    ]
    overflow = max(0, len(patch_skips) - limit)
    logger.warning(
        "%d JSON patch(es) skipped (expected bytes don't match the "
        "current game; mod likely built for an older version):",
        len(patch_skips))
    for ln in lines:
        logger.warning("%s", ln.strip())
    if overflow:
        logger.warning("  ... and %d more", overflow)
    return lines, overflow


def invalidate_apply_fingerprint(
    game_dir: Path,
    config: "Config | None" = None,
) -> None:
    """Remove ``CDMods/.apply_fingerprint`` so the next Apply genuinely
    re-runs the pipeline.

    The fingerprint hashes mods.json_source PATH (not contents) plus
    mod_deltas. A re-import that overwrites the merged.json content but
    keeps the same path produces an identical hash, and the next Apply
    fast-paths 'Already up to date'. Re-import call sites must invoke
    this after the DB commit. Idempotent (no-op if the file is missing).
    """
    fp_path = get_cdmods_root(config, game_dir) / ".apply_fingerprint"
    try:
        if fp_path.exists():
            fp_path.unlink()
    except OSError as e:
        logger.debug(
            "invalidate_apply_fingerprint: could not remove %s: %s",
            fp_path, e)


# Set of file extensions where partial-byte-edits across mods can be
# safely merged. Anything else falls back to last-wins to avoid
# corrupting self-contained binary blobs (textures, audio, images,
# compressed data) that would crash the game when byte-merged.
# Nexus regression report (mrkillerhomer, 2026-05-03 v3.2.7→v3.2.8):
# the byte-merge fallback enabled in commit 57cfa29 for non-pabgb
# entries fired on overlapping DDS textures and produced a corrupt
# Frankenstein file that froze the game on the loading screen.
_BYTE_MERGEABLE_EXTS = frozenset({
    # PABGB tables and their headers
    "pabgb", "pabgh",
    # Pearl Abyss XML / sequencer formats
    "pac_xml", "pamb_xml", "paseq", "paac", "xml",
    # UI text formats
    "css", "html",
})


def _entry_supports_byte_merge(entry_path: str) -> bool:
    """Return True iff ``entry_path``'s extension is one where a
    multi-mod byte-merge is meaningful. Self-contained binary blobs
    (textures, audio) and unknown extensions return False so the
    apply pipeline falls through to last-wins for them.
    """
    name = entry_path.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1]
    return ext in _BYTE_MERGEABLE_EXTS


def _yield_gil() -> None:
    """Release the GIL momentarily so the GUI thread can process events.

    Python's GIL means QThread workers starve the main thread of CPU time
    during pure-Python loops.  time.sleep(0) is the standard way to yield
    the GIL without any real delay.
    """
    time.sleep(0)


def _dirs_losing_pamt(deferred_file_deletions: "list[Path]") -> "set[str]":
    """Directory names (e.g. ``'0037'``) whose ``0.pamt`` index file is
    queued for post-commit deletion.

    GitHub #225: a disabled mod's new files (``0037/0.pamt`` +
    ``0037/0.paz``) go into ``deferred_file_deletions``, deleted only
    AFTER the transaction commits. The Phase 4 PAPGT rebuild runs before
    the commit, while those files are still on disk, so without this the
    rebuilt index keeps an entry for ``0037`` — then the now-empty dir is
    removed post-commit and Post-Apply Verification reports
    "Missing directory 0037". Feeding these dirs into the rebuild's
    ``exclude_dirs`` keeps the index consistent with the post-commit
    on-disk state.

    Keyed on the ``0.pamt`` index file specifically (the file that makes
    a directory a real PAPGT entry, per the rebuild's disk-discovery and
    the post-apply verify): a dir losing only its ``.paz`` keeps a valid
    index and is not a "missing directory" case.
    """
    out: set[str] = set()
    for fp in deferred_file_deletions:
        if fp.name.lower() == "0.pamt":
            out.add(fp.parent.name)
    return out

from cdumm.archive.papgt_manager import PapgtManager
from cdumm.archive.transactional_io import TransactionalIO
from cdumm.engine.delta_engine import (
    SPARSE_MAGIC, apply_delta, apply_delta_from_file, load_delta,
)
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


def _build_silent_apply_failure_message(
    mod_summary: list[dict],
) -> str:
    """Build the user-facing warning shown when every enabled JSON
    mod produced 0 overlay entries at apply time.

    `mod_summary` comes from `aggregate_json_mods_into_synthetic_patches`
    and lists per-mod contributions: name, priority, targets, change
    count. The warning names each contributing mod (skipping mods that
    contributed 0 changes already) plus their target files, and points
    at Fix Everything as the next step.

    Bug from Robhood19 (Nexus, 2026-04-29): the previous warning just
    said "X JSON mods produced no game changes" without naming which
    mods or files; users had no way to act.
    """
    contributors = [
        m for m in mod_summary
        if m.get("change_count", 0) > 0 and m.get("targets")
    ]
    if not contributors:
        return (
            "Enabled JSON mods produced no game changes at apply. "
            "Run Settings > Fix Everything to rebuild vanilla backups, "
            "then re-Apply. If the warning persists, the mods are "
            "likely outdated for your current game version."
        )
    lines = []
    for m in contributors:
        targets = ", ".join(m["targets"])
        lines.append(f"  - '{m['mod_name']}' targets {targets}")
    detail = "\n".join(lines)
    return (
        f"{len(contributors)} JSON mod(s) were enabled but produced "
        f"no game changes:\n{detail}\n\n"
        f"Most likely cause: the patch bytes don't match your current "
        f"game version (the game was updated after the mod was made), "
        f"so every patch was skipped. Try Settings > Fix Everything "
        f"to rebuild vanilla backups, then re-Apply. If the same "
        f"warning shows up again, the mods are outdated and need a "
        f"fresh release from their authors."
    )


def _compose_merged_mod_name(
    mod_names: list[str], merge_kind: str,
) -> str:
    """Build a user-facing mod_name for a merged delta produced by
    semantic-merge or byte-merge.

    Used to keep real contributing mod names visible in downstream
    warnings (size-merge fallback, conflict viewer, etc.) instead of
    leaking the synthetic merge-kind label. The merge-kind tag still
    lives on `_merged_metadata` for internal routing.

    Deduplicates and skips empty / "unknown" entries. Caps the inline
    list at 3 names plus a "+ N more" tail so banner layouts don't
    blow up on big multi-mod merges.
    """
    seen: list[str] = []
    for n in mod_names:
        if not n or n == "unknown":
            continue
        if n not in seen:
            seen.append(n)
    if not seen:
        return f"({merge_kind} of unidentified mods)"
    if len(seen) <= 3:
        return " + ".join(seen)
    return " + ".join(seen[:3]) + f" + {len(seen) - 3} more"


def _rewrite_mount_error_with_mod_names(
    raw_error: str, targets_to_mods: dict[str, list[str]]
) -> str:
    """Replace the synth-file error with real mod names + an action.

    The mount-time patcher emits errors of the form::

        "<json_source_stem>: all N patches mismatched against vanilla
         <game_file> -- the mod was built for a different game version."

    When the caller pre-aggregates JSON mods, ``json_source_stem`` is
    the synth filename (e.g. ``aggregated``) and the message tells the
    user nothing useful. We re-derive the contributing mod names from
    ``targets_to_mods`` (built upstream from per_mod_summary) and emit
    a message that names the actual mods AND tells the user what to
    do about it.

    Falls back to the original ``raw_error`` if the regex doesn't match
    so we never silently drop diagnostic information.
    """
    import re
    # Match BOTH the "aborting -- N of M patches mismatched" data-table
    # form AND the "all N patches mismatched" generic form. game_file
    # capture group is what we need for the lookup.
    m = re.search(
        r"(?:all|of)\s+\d+\s+patches\s+(?:mismatched|don['’]t\s+match)\s+"
        r"(?:against\s+)?(?:vanilla\s+)?([^\s\n]+\.\w+)",
        raw_error)
    if not m:
        return raw_error
    game_file = m.group(1).rstrip(".,:;")
    contributors = targets_to_mods.get(game_file, [])
    if not contributors:
        # Lookup miss -- preserve original error rather than mislead.
        return raw_error
    if len(contributors) == 1:
        who = f"'{contributors[0]}'"
    elif len(contributors) <= 3:
        who = ", ".join(f"'{n}'" for n in contributors)
    else:
        head = ", ".join(f"'{n}'" for n in contributors[:3])
        who = f"{head} (+{len(contributors) - 3} more)"
    plural = "mod" if len(contributors) == 1 else "mods"
    return (
        f"Skipped {who}: the {plural} cannot patch the current "
        f"{game_file} -- they were built for an older version of "
        "the game. After a Steam update, click 'Start Recovery' on "
        "the banner (or right-click each mod -> Reimport from source) "
        "to regenerate them against your current game files."
    )


def _normalize_target(name: str) -> str:
    """Compare a Format 3 `target` with a Format 2 `game_file` fairly.

    One ships ``gamedata/binary__/client/bin/iteminfo.pabgb`` and the other
    ``gamedata/iteminfo.pabgb``. Comparing them raw silently never matches --
    the exact path-vs-bare-name trap that made `match` select zero records
    (#275) and array_append no-op (#278). Compare on the bare table name.
    """
    return (str(name or "").replace("\\", "/").rsplit("/", 1)[-1].lower())


def aggregate_json_mods_into_synthetic_patches(
    db, overlay_priority_tiebreak: bool = True,
) -> tuple[dict, list[dict]]:
    """Collect enabled JSON mods' patches, group by game_file, return
    a combined patch list suitable for ONE pass through
    ``process_json_patches_for_overlay``.

    Option Y: when two mods patch the same `.pabgb` but one of them
    inserts bytes, byte-merging their independent outputs is impossible
    (offset drift). The fix is to stop processing mods independently
    and instead feed ALL their patches to ``_apply_byte_patches`` in a
    single pass — its cumulative-delta tracking then handles inserts
    across mod boundaries correctly.

    Returns ``(synth_patch_data, per_mod_summary)`` where:
      * ``synth_patch_data`` is a dict shaped like a JSON-mod source
        (`{"patches": [...]}`) with one patch entry per game_file and
        all contributing mods' changes concatenated in priority order.
      * ``per_mod_summary`` is a list of dicts (one per contributing
        mod) used for logging / UI — never fed back into the patcher.

    Priority ordering: CDUMM's convention is "lowest priority number
    wins". To mirror `_apply_byte_patches`'s stable-sort-by-offset
    behaviour (on offset ties, later-in-list wins), we emit the lowest-
    priority-number mod's changes LAST so they overwrite on tie.
    """
    import json as _json

    rows = db.connection.execute(
        "SELECT m.id, m.name, m.json_source, m.disabled_patches, "
        "       m.priority, mc.custom_values "
        "FROM mods m LEFT JOIN mod_config mc ON mc.mod_id = m.id "
        "WHERE m.enabled = 1 AND m.json_source IS NOT NULL "
        "AND m.json_source != '' "
        "ORDER BY m.priority DESC, m.id ASC"
    ).fetchall()

    # {game_file: list[change]} — changes concatenated in priority DESC
    # order (highest priority number first, lowest last).
    aggregated: dict[str, list[dict]] = {}
    # {game_file: signature_hex} — first non-empty signature wins
    # (two mods targeting the same file should ship compatible anchors).
    signatures: dict[str, str] = {}
    per_mod_summary: list[dict] = []

    from pathlib import Path as _Path

    # ── Byte offsets vs a rebuilt table (GitHub #293) ──────────────────
    #
    # A Format 3 mod does not patch bytes: CDUMM parses the whole table,
    # edits records, and RE-SERIALIZES it. Records change size, so every
    # byte offset after the first edited record MOVES.
    #
    # A Format 2 mod patches fixed offsets. Applied against a table that a
    # Format 3 mod has rebuilt, those offsets no longer point where the
    # author measured them -- the write lands in the middle of some other
    # record. The result is a structurally invalid table and the game will
    # not start. falobos76 hit exactly this (GitHub #191): pinapana's
    # socket mods (Format 3, iteminfo) plus any of three offset mods that
    # also patch iteminfo -> crash on startup. Each works alone.
    #
    # CDUMM already knew which tables get rebuilt -- `f3_target_files` was
    # collected and then used ONLY for a display label. Nothing guarded on
    # it. So: find them first, and refuse the unsafe combination rather
    # than corrupt the file. An honest refusal beats a broken install the
    # user only discovers when the game won't launch.
    f3_rebuilt: dict[str, str] = {}      # {game_file: mod that rebuilds it}
    for _mid, _mname, _src, _dis, _pri, _cv in rows:
        _p = _Path(_src)
        if not _p.exists():
            continue
        try:
            _d = _json.loads(_p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if _d.get("format") != 3:
            continue
        try:
            from cdumm.engine.format3_handler import (
                parse_format3_mod_targets as _pf3)
            for _tgt, _ints in _pf3(_p):
                if _ints:
                    f3_rebuilt.setdefault(
                        _normalize_target(_tgt), _mname or f"mod {_mid}")
        except Exception:  # noqa: BLE001 - never break apply over the guard
            logger.exception(
                "aggregate: could not read Format 3 targets for mod %s; "
                "the byte-offset guard cannot protect its tables", _mid)

    refused_offset_mods: list[dict] = []

    for mod_id, mod_name, json_source, disabled_raw, priority, cv_raw in rows:
        jp_path = _Path(json_source)
        if not jp_path.exists():
            logger.debug(
                "aggregate: mod %d json_source missing, skipping: %s",
                mod_id, json_source)
            continue
        try:
            data = _json.loads(jp_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            logger.warning(
                "aggregate: mod %d json parse failed (%s), skipping",
                mod_id, e)
            continue

        try:
            disabled = set(_json.loads(disabled_raw) if disabled_raw else [])
        except (ValueError, TypeError):
            disabled = set()
        try:
            custom_vals = _json.loads(cv_raw) if cv_raw else None
        except (ValueError, TypeError):
            custom_vals = None

        # Format 3 mods carry their target + intent count in a different
        # shape than Format 1 / 2 patch-list mods. They are routed
        # through cdumm.engine.format3_apply which produces its own
        # summary line, but the per-mod display log here used to render
        # them as "0 change(s) → []" because data.get("patches", [])
        # returned an empty list. That misled pitonpp GitHub #105 into
        # thinking his Format 3 mods were not applying when the real
        # problem (the iteminfo intent silently dropping on macOS) was
        # separate. Detect Format 3 here so the display log can label
        # the entry honestly. Parsing failures fall through to the
        # legacy 0-change record (which matches the old behaviour).
        is_format3 = False
        f3_target_files: list[str] = []
        f3_intent_count = 0
        if data.get("format") == 3:
            try:
                from cdumm.engine.format3_handler import (
                    parse_format3_mod_targets)
                target_pairs = parse_format3_mod_targets(jp_path)
                is_format3 = True
                for tgt, ints in target_pairs:
                    f3_target_files.append(tgt)
                    f3_intent_count += len(ints)
            except (ValueError, OSError, ImportError) as _e_f3:
                logger.debug(
                    "aggregate: mod %d parse_format3_mod_targets failed "
                    "(%s); falling back to legacy summary row",
                    mod_id, _e_f3)

        targets_this_mod: set[str] = set()
        flat_idx = 0
        for patch in data.get("patches", []):
            game_file = patch.get("game_file")
            if not game_file:
                continue

            # GitHub #293: this mod patches fixed byte offsets in a table
            # that a Format 3 mod rebuilds. Those offsets are stale the
            # moment the table is re-serialized.
            #
            # #294 refused the mod outright here. #296 can do better --
            # work out where those bytes MOVED to and rewrite the offsets --
            # but that needs the rebuilt table, which doesn't exist yet:
            # expand_format3_into_aggregated() runs later. So tag the
            # changes now and re-anchor them once the rebuild is in hand
            # (_reanchor_offsets_onto_rebuilds, below). Anything that can't
            # be re-anchored is still refused, which is the whole point.
            rebuilder = f3_rebuilt.get(_normalize_target(game_file))
            all_changes = patch.get("changes", [])
            if rebuilder:
                logger.info(
                    "%r patches %s at fixed byte offsets and %r rebuilds "
                    "that table (Format 3) — the offsets will be re-anchored "
                    "onto the rebuilt table (#296).",
                    mod_name, game_file, rebuilder)
                all_changes = [
                    {**c, "_needs_reanchor": rebuilder} for c in all_changes]
            # Apply custom values BEFORE the disabled filter so both
            # operations key by ORIGINAL patch index. Earlier this
            # ran custom_values on the already-filtered list, which
            # used enumerate-by-filtered-position keys; if any patch
            # before an editable was disabled, the editable's stored
            # value (keyed by original index per FIX 4) silently
            # missed and the patch fell back to the mod author's
            # default. Round 4 mount-time audit MEDIUM-2.
            if custom_vals:
                from cdumm.engine.json_patch_handler import (
                    apply_custom_values)
                all_changes = apply_custom_values(all_changes, custom_vals)
            # Per-mod disabled_patches filter (flat indexed across all
            # of THIS mod's changes, matching how the picker records it).
            # Also tag each surviving change with `_source_mod_id` so
            # downstream skip-recording can attribute byte-mismatch
            # failures back to the mod that supplied the change.
            # Without this tag, a partial-skip apply knows "N patches
            # skipped total" but can't badge the responsible mod card.
            filtered = []
            for c in all_changes:
                if flat_idx not in disabled:
                    tagged = dict(c)
                    tagged["_source_mod_id"] = mod_id
                    tagged["_target_file"] = game_file
                    filtered.append(tagged)
                flat_idx += 1
            if not filtered:
                continue

            aggregated.setdefault(game_file, []).extend(filtered)
            new_sig = patch.get("signature")
            if new_sig:
                if game_file not in signatures:
                    signatures[game_file] = new_sig
                elif signatures[game_file] != new_sig:
                    # Two mods ship different signatures for the same
                    # target file. The aggregator picks first-wins,
                    # so the second mod's changes get applied with the
                    # WRONG anchor. The byte-mismatch + vanilla-remnant
                    # paths usually catch this, but a silent loss of
                    # one mod's changes is still possible. Surface the
                    # conflict so a bug report includes it. Round 4
                    # mount-time audit MEDIUM-3.
                    logger.warning(
                        "Signature conflict on %s: mod %r ships "
                        "signature %r but earlier mod's signature "
                        "(%r) is already in use. Second mod's changes "
                        "will apply against the first mod's anchor; "
                        "byte mismatches expected if the signatures "
                        "anchor at different vanilla offsets.",
                        game_file, mod_name, new_sig,
                        signatures[game_file])
            targets_this_mod.add(game_file)

        if is_format3 and not targets_this_mod:
            # Pure Format 3 mod (no legacy patches[] entries): report
            # the parsed intent count + targets so the display log
            # reflects what the mod actually ships. The Format 3 apply
            # pipeline runs separately and writes its own summary line.
            per_mod_summary.append({
                "mod_id": mod_id, "mod_name": mod_name,
                "priority": priority,
                "targets": sorted(set(f3_target_files)),
                "change_count": f3_intent_count,
                "is_format3": True,
            })
        else:
            per_mod_summary.append({
                "mod_id": mod_id, "mod_name": mod_name,
                "priority": priority,
                "targets": sorted(targets_this_mod),
                "change_count": flat_idx - len(disabled),
                "is_format3": is_format3,
            })

    synth_patch_data = {
        "modinfo": {
            "title": "CDUMM aggregated JSON mods",
            "description": (
                "Virtual combined patch produced by Option Y — all "
                "enabled JSON mods' changes folded into a single "
                "pass so cumulative-offset tracking works across "
                "mod boundaries."),
        },
        "patches": [
            {
                "game_file": gf,
                "signature": signatures.get(gf),
                "changes": changes,
            }
            for gf, changes in aggregated.items()
        ],
    }
    if refused_offset_mods:
        # Carried so the apply path / bug report can name the combination
        # instead of the user discovering it when the game won't launch.
        # Consumers read "patches"; an extra key is inert to them.
        synth_patch_data["_refused_offset_mods"] = refused_offset_mods
    return synth_patch_data, per_mod_summary


def _make_format3_vanilla_extractor(
    *, vanilla_dir, game_dir, snapshot_mgr,
    get_vanilla_entry_content, extract_sibling_entry,
):
    """Build the ``vanilla_extractor`` callable used by
    ``expand_format3_into_aggregated``.

    Resolution order mirrors the v2 ``resolve_vanilla_source`` path:
      1. ``vanilla_dir`` backup PAMT entry — return its bytes.
      2. ``game_dir`` live PAMT entry — return its bytes ONLY when
         the live PAZ's hash matches the snapshot fingerprint. If the
         live file is modded, return None so the caller surfaces a
         clean "vanilla bytes unavailable" warning instead of feeding
         modded bytes to a downstream parser (GitHub #62/#68).

    Without the hash check, a Format 3 iteminfo mod applied on top of
    a previously-applied v2 iteminfo mod hands the writer the modded
    bytes; the writer hits "CArray count exceeds remaining bytes" and
    crashes the apply.
    """
    from cdumm.engine.json_patch_handler import (
        _derive_pamt_dir, _find_pamt_entry,
    )
    from cdumm.engine.snapshot_manager import hash_file

    def _extractor(target):
        try:
            backup_entry = _find_pamt_entry(target, vanilla_dir)
            chosen_entry = None
            if backup_entry is not None and Path(
                    backup_entry.paz_file).exists():
                chosen_entry = backup_entry
            else:
                live_entry = _find_pamt_entry(target, game_dir)
                if live_entry is None:
                    return None
                paz_path = Path(live_entry.paz_file)
                if not paz_path.exists():
                    return None
                # Hash-verify before trusting live bytes.
                try:
                    paz_rel = str(paz_path.relative_to(
                        game_dir)).replace("\\", "/")
                except ValueError:
                    paz_rel = paz_path.name
                snap_hash = snapshot_mgr.get_file_hash(paz_rel)
                if snap_hash is None:
                    logger.warning(
                        "Format 3 vanilla extraction refused for %s: "
                        "no snapshot hash for %s. Run Settings -> "
                        "Fix Everything to refresh.",
                        target, paz_rel)
                    return None
                try:
                    live_hash, _size = hash_file(paz_path)
                except FileNotFoundError:
                    return None
                if live_hash != snap_hash:
                    logger.warning(
                        "Format 3 vanilla extraction refused for %s: "
                        "live PAZ %s diverged from snapshot (already "
                        "modded). Run Settings -> Fix Everything or "
                        "revert before re-applying.",
                        target, paz_rel)
                    return None
                chosen_entry = live_entry
                # Lazy backup so the next Format 3 apply finds the
                # backup directly. Mirrors the behavior in
                # ``resolve_vanilla_source`` for the v2 path.
                # GitHub #68. Skip when paz_rel is just a bare
                # filename (relative_to fallback) — would write the
                # backup to the wrong location otherwise.
                if "/" in paz_rel:
                    try:
                        backup_paz = vanilla_dir / paz_rel
                        if not backup_paz.exists():
                            backup_paz.parent.mkdir(
                                parents=True, exist_ok=True)
                            _backup_copy(paz_path, backup_paz)
                            sibling_pamt = paz_path.with_suffix(".pamt")
                            if sibling_pamt.exists():
                                backup_pamt = backup_paz.with_suffix(".pamt")
                                if not backup_pamt.exists():
                                    _backup_copy(sibling_pamt, backup_pamt)
                    except Exception as e:
                        logger.debug(
                            "Lazy vanilla backup failed for %s: %s",
                            paz_rel, e)
            pamt_dir = _derive_pamt_dir(chosen_entry.paz_file)
            if not pamt_dir:
                return None
            file_path = f"{pamt_dir}/{Path(chosen_entry.paz_file).name}"
            body = get_vanilla_entry_content(file_path, target)
            if body is None:
                return None
            header_path = target
            if header_path.endswith(".pabgb"):
                header_path = header_path[:-len(".pabgb")] + ".pabgh"
            header = extract_sibling_entry(pamt_dir, header_path)
            if header is None:
                return None
            return body, header
        except Exception:
            logger.debug(
                "Format 3 vanilla extraction failed for %s",
                target, exc_info=True)
            return None

    return _extractor


def _expand_format3_into_synth_data(
    synth_data: dict, db, vanilla_dir, game_dir,
    get_vanilla_entry_content, extract_sibling_entry,
    warnings_out: list[str] | None = None,
    participating_mod_ids: set | None = None,
) -> None:
    """Wire-up helper: decompose synth_data, run Format 3 expansion,
    repack synth_data["patches"] with the extended set.

    Lives here (not inside aggregate_json_mods_into_synthetic_patches)
    so the v2 aggregator function stays load-bearing-stable. This
    helper is the ONE place apply_engine knows about Format 3
    expansion; the rest of the apply pipeline sees v2-shaped changes.

    ``warnings_out`` collects user-facing warnings (zero-change mods,
    extraction failures) that the caller routes through the
    ``warning`` Qt signal so on_apply_done renders them in the
    InfoBar.
    """
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from cdumm.engine.snapshot_manager import SnapshotManager

    snapshot_mgr = SnapshotManager(db)
    _vanilla_extractor = _make_format3_vanilla_extractor(
        vanilla_dir=vanilla_dir,
        game_dir=game_dir,
        snapshot_mgr=snapshot_mgr,
        get_vanilla_entry_content=get_vanilla_entry_content,
        extract_sibling_entry=extract_sibling_entry,
    )

    # Decompose synth_data → mutable dicts the expansion mutates
    aggregated = {p["game_file"]: list(p.get("changes", []))
                  for p in synth_data.get("patches", [])}
    signatures = {p["game_file"]: p["signature"]
                  for p in synth_data.get("patches", [])
                  if p.get("signature")}

    pre_keys = set(aggregated.keys())
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=_vanilla_extractor,
        warnings_out=warnings_out,
        participating_mod_ids=participating_mod_ids,
    )

    # GitHub #121 BloodGozilla: Format 3 mods use the bare basename
    # form for their `target` field (e.g. "iteminfo.pabgb"), while
    # legacy Format 1 / 2 patch mods use the full PAZ-internal path
    # (e.g. "gamedata/iteminfo.pabgb"). Both resolve to the same
    # physical PAZ entry at mount time, but `aggregated` keys them
    # separately so process_json_patches_for_overlay produces two
    # overlay entries with the same destination path. The PAMT
    # lookup at game launch then returns only one, silently dropping
    # the other mod's changes. Normalising bare basenames to their
    # canonical full path here merges Format 3 + legacy contributors
    # for the same target into a single overlay entry.
    try:
        from cdumm.engine.json_patch_handler import _find_pamt_entry
        normalized: dict[str, list] = {}
        normalized_sigs: dict[str, str] = {}
        for key in list(aggregated.keys()):
            canonical = key
            if "/" not in key:
                entry = _find_pamt_entry(key, game_dir)
                if entry is None and vanilla_dir is not None:
                    entry = _find_pamt_entry(key, vanilla_dir)
                if entry is not None and entry.path and entry.path != key:
                    canonical = entry.path
            normalized.setdefault(canonical, []).extend(aggregated[key])
            if key in signatures:
                normalized_sigs.setdefault(canonical, signatures[key])
        # Log any merges so a bundle shows the normalisation worked.
        merges = {
            k: v for k, v in normalized.items()
            if k not in aggregated and len(v) > 1
        }
        if merges:
            for canonical, changes in merges.items():
                logger.info(
                    "Format 3 aggregator: merged %d change(s) under "
                    "canonical path %r (was split across bare-name + "
                    "full-path keys)", len(changes), canonical)
        aggregated.clear()
        aggregated.update(normalized)
        signatures.clear()
        signatures.update(normalized_sigs)
    except Exception as _e_norm:
        logger.warning(
            "Format 3 aggregator: bare-basename normalisation failed "
            "(%s); falling through with original keys", _e_norm)

    # The Format 3 rebuild now exists in `aggregated`, so the byte-offset
    # changes tagged during aggregation can finally be moved onto it (#296).
    _reanchor_offsets_onto_rebuilds(aggregated, synth_data)

    new_keys = set(aggregated.keys()) - pre_keys
    if new_keys or any(len(aggregated[k]) != len(
            next((p["changes"] for p in synth_data.get("patches", [])
                  if p["game_file"] == k), []))
            for k in pre_keys):
        # Format 3 contributed something — repack
        synth_data["patches"] = [
            {"game_file": gf,
             "signature": signatures.get(gf),
             "changes": aggregated[gf]}
            for gf in aggregated
        ]


def _reanchor_offsets_onto_rebuilds(
    aggregated: dict[str, list[dict]], synth_data: dict,
) -> None:
    """Move byte-offset changes onto the table a Format 3 mod rebuilt.

    GitHub #293. Without this, #296's re-anchor module is dead code: it was
    shipped with its tests but never called, so falobos76's mods would still
    be REFUSED by #294 rather than made to work. (Same mistake as #288 —
    a translator nothing called. Wiring is not a detail.)

    Refuses, rather than guesses, in the two cases that matter:
      * the rebuild didn't materialise (Format 3 expansion failed) — the
        offsets are still stale, so they must not be written;
      * a change can't be re-anchored because the Format 3 mod changed the
        very bytes it patches — the two mods genuinely disagree.
    """
    from cdumm.engine.offset_reanchor import (
        reanchor_changes, whole_table_change,
    )

    refused: list[dict] = list(synth_data.get("_refused_offset_mods") or [])

    def _refuse(change: dict, game_file: str, why: str) -> None:
        refused.append({
            "mod_id": change.get("_source_mod_id"),
            "mod_name": change.get("_source_mod_name") or "a byte-offset mod",
            "game_file": game_file,
            "rebuilt_by": change.get("_needs_reanchor"),
            "reason": why,
        })

    for game_file, changes in list(aggregated.items()):
        tagged = [c for c in changes if c.get("_needs_reanchor")]
        if not tagged:
            continue

        if whole_table_change(changes) is None:
            # A Format 3 mod claims this table but produced no rebuilt body
            # (extraction failed, zero supported intents, ...). The offsets
            # are stale and there is nothing to re-anchor them onto.
            logger.warning(
                "REFUSED: %d byte-offset change(s) on %s — %r rebuilds this "
                "table but its rebuild is not present, so the offsets cannot "
                "be re-anchored and would write to the wrong bytes.",
                len(tagged), game_file, tagged[0].get("_needs_reanchor"))
            for c in tagged:
                _refuse(c, game_file, "the Format 3 rebuild is missing")
            aggregated[game_file] = [
                c for c in changes if not c.get("_needs_reanchor")]
            continue

        kept, dropped = reanchor_changes(changes)
        for c in dropped:
            _refuse(c, game_file,
                    c.get("_refuse_reason") or "could not be re-anchored")
        moved = sum(1 for c in kept if "_reanchored_from" in c)
        logger.info(
            "offset re-anchor on %s: %d change(s) moved onto the rebuilt "
            "table, %d refused", game_file, moved, len(dropped))
        aggregated[game_file] = [
            {k: v for k, v in c.items() if k != "_needs_reanchor"}
            for c in kept
        ]

    if refused:
        synth_data["_refused_offset_mods"] = refused


def collect_enabled_json_targets(db) -> set[str]:
    """Return the set of game_files every enabled JSON mod patches.

    Used by cross-layer merge to decide whether a PAZ-dir mod's entry
    should skip direct staging. If NO enabled JSON mod targets a given
    logical file, there's no one to layer on top and skipping leaves
    the game with an orphaned PAMT entry pointing at a missing paz.
    """
    import json as _json
    from pathlib import Path as _Path
    rows = db.connection.execute(
        "SELECT json_source FROM mods "
        "WHERE enabled = 1 AND json_source IS NOT NULL "
        "AND json_source != ''").fetchall()
    targets: set[str] = set()
    for (json_source,) in rows:
        # json_source holds a filesystem path to the archived JSON,
        # written by import_json_fast / import_json_as_entr. Earlier
        # code parsed the path string itself, which always failed —
        # silently masking this whole loop.
        jp = _Path(json_source)
        if not jp.exists():
            continue
        try:
            data = _json.loads(jp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for patch in data.get("patches", []):
            gf = patch.get("game_file")
            if gf:
                targets.add(gf)
    # ENTR-delta JSON mods (imported pre-v3.1) stored their patches
    # at import time as entry-level deltas rather than keeping
    # json_source. Their mod_deltas rows carry the entry_path, which
    # equals the logical game_file for single-target JSON mods.
    entr_rows = db.connection.execute(
        "SELECT DISTINCT d.entry_path FROM mod_deltas d "
        "JOIN mods m ON m.id = d.mod_id "
        "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
        "AND d.entry_path IS NOT NULL AND d.entry_path != ''"
    ).fetchall()
    for (ep,) in entr_rows:
        targets.add(ep)
    return targets


def precheck_enabled_mod_pamts(db) -> list[str]:
    """Placeholder — was the v3.1.7 apply-time PAMT precheck.

    Disabled in v3.1.7.2 because the implementation was fundamentally
    wrong: it called ``parse_pamt`` on stored delta files, which are
    BSDIFF patches — not PAMT bytes. Two failure modes:

    1. Parsing bsdiff binary as PAMT produced garbage headers that
       tripped the paz_count sanity guard, false-flagging every valid
       mod as corrupt.
    2. The bsdiff filename stem (e.g. ``0036_0.pamt``) is non-numeric,
       so ``int(pamt_stem)`` in parse_pamt blew up before any header
       parsing — same failure shape as the v3.1.7 B1 bug.

    Reported as a warning banner on every Apply in issue #38
    (LeoBodnar). A proper version of this precheck would reconstruct
    the modified PAMT bytes via ``bsdiff4.patch(vanilla, delta)`` and
    validate them — that's real work and should land in a later
    release. Meanwhile, B1 (v3.1.7.1) catches corrupt PAMTs at import
    time, and the apply flow's existing error handling catches them
    downstream if a legacy broken delta still exists.

    Kept as a callable no-op so the call site in ``_apply()`` doesn't
    need a conditional.
    """
    return []


def collect_paz_dir_overrides(
    db, enabled_only: bool = True,
    warnings_out: list | None = None,
) -> dict:
    """Scan enabled PAZ-dir mods, return a map of game_file → override.

    #145 cross-layer merge. When a mod ships ``0036/0.paz`` containing
    its own ``gamedata/iteminfo.pabgb``, ANOTHER mod's JSON patches on
    that same logical file would normally land in CDUMM's overlay
    (0037+) while the PAZ-dir mod stages 0036/0.paz separately. The
    game then loads one of the two and silently drops the other.

    This helper walks every enabled PAZ-dir mod's stored ``NNNN/0.paz``
    + ``NNNN/0.pamt`` delta pair and records which logical entries
    each mod provides, so the JSON-patch resolver can layer on top:
    use the PAZ-dir mod's bytes as the patch base and emit the
    combined result to a higher overlay dir where it wins in-game.

    Returns a dict keyed by ``game_file`` (e.g. ``gamedata/iteminfo.pabgb``).
    Each value is a dict with:

      * ``mod_id``, ``mod_name``, ``priority`` — source mod identity
      * ``pamt_dir`` — the 4-digit dir the mod claimed (e.g. ``"0036"``)
      * ``paz_delta_path`` — absolute path to the stored ``NNNN/0.paz``
      * ``pamt_delta_path`` — absolute path to the stored ``NNNN/0.pamt``
      * ``entry`` — parsed :class:`PazEntry` rebound to the delta paz

    When multiple enabled mods provide the same game_file the lowest
    priority number wins (CDUMM convention — priority=1 is top).
    """
    from cdumm.archive.paz_parse import parse_pamt

    rows = db.connection.execute(
        "SELECT d.mod_id, m.name, m.priority, d.file_path, d.delta_path "
        "FROM mod_deltas d JOIN mods m ON m.id = d.mod_id "
        "WHERE m.mod_type = 'paz' "
        + ("AND m.enabled = 1 " if enabled_only else "")
        + "AND d.file_path LIKE '____/0.paz' "
        "ORDER BY m.priority ASC, m.id ASC"
    ).fetchall()

    overrides: dict[str, dict] = {}
    # Track which mod_ids have already produced a parse-failure warning
    # in this call so a single broken mod that ships many NNNN dirs
    # doesn't flood the InfoBar with the same message N times. Bug
    # report from Faisal 2026-04-26 — Enhanced Internal Graphics fired
    # 30+ identical warnings for one mod.
    warned_mod_ids: set[int] = set()
    for mod_id, mod_name, priority, file_path, paz_delta_path in rows:
        # file_path looks like 'NNNN/0.paz'; sibling PAMT lives at
        # 'NNNN/0.pamt' in the same mod's deltas.
        pamt_dir = file_path.split("/", 1)[0]
        if not (len(pamt_dir) == 4 and pamt_dir.isdigit()):
            continue
        pamt_file_path = f"{pamt_dir}/0.pamt"
        pamt_row = db.connection.execute(
            "SELECT delta_path FROM mod_deltas "
            "WHERE mod_id = ? AND file_path = ?",
            (mod_id, pamt_file_path)).fetchone()
        if pamt_row is None:
            continue
        pamt_delta_path = pamt_row[0]

        # The cross-layer override scan only handles full-file PAZ-dir
        # replacements. Crimson Browser converted mods (e.g. r457
        # Graphics Tweaks, mod 602) store their PAMT as an SPRS sparse
        # patch and the PAZ as a BSDIFF40 binary diff against vanilla.
        # Neither can be parsed as a standalone PAMT or PAZ — feeding
        # them to parse_pamt yields the bogus 'folder_size exceeds
        # file size' rejection users were seeing in the InfoBar. Skip
        # silently when EITHER stored delta is a sparse/binary patch.
        # Bug report from Faisal 2026-04-26.
        try:
            with open(pamt_delta_path, "rb") as _f:
                if _f.read(4) in (b"SPRS", b"BSDI"):
                    continue
            with open(paz_delta_path, "rb") as _f:
                if _f.read(4) in (b"SPRS", b"BSDI"):
                    continue
        except OSError:
            continue

        # Stored deltas land on disk as ``0036_0.pamt.newfile`` /
        # ``0036_0.paz.newfile`` — parse_pamt derives the PAZ number
        # from the filename stem (expects plain ``0.pamt``). Stage
        # both into a tracked temp workspace with the canonical names
        # first. make_temp_dir registers atexit cleanup, the prefix is
        # in temp_workspace.CDUMM_PREFIXES so sweep_stale reclaims
        # leftovers from crashed runs, and ApplyWorker releases these
        # at the end of the apply (the staged copies are consumed
        # during the run).
        import shutil as _shutil
        from cdumm.engine.temp_workspace import (
            make_temp_dir, release_temp_dir)
        stage_root = make_temp_dir(f"cdumm_xlayer_{pamt_dir}_")
        canonical_pamt = stage_root / "0.pamt"
        canonical_paz = stage_root / "0.paz"
        try:
            _shutil.copyfile(pamt_delta_path, str(canonical_pamt))
            _shutil.copyfile(paz_delta_path, str(canonical_paz))
            entries = parse_pamt(
                str(canonical_pamt), paz_dir=str(stage_root))
        except Exception as e:
            logger.debug(
                "collect_paz_dir_overrides: skip mod %d — pamt parse "
                "failed: %s", mod_id, e)
            if warnings_out is not None and mod_id not in warned_mod_ids:
                # A2 fix: surface this to the GUI InfoBar. The silent
                # DEBUG log made every corrupt-archive case look like
                # "stuck at 2%" to users. Tell them what broke and
                # what to do next. Dedup per mod — see warned_mod_ids.
                warnings_out.append(
                    f"Mod '{mod_name}' has a corrupt archive "
                    f"(pamt parse failed: {e}) and was skipped. "
                    "Re-import it from the original zip.")
                warned_mod_ids.add(mod_id)
            release_temp_dir(stage_root)
            continue

        for entry in entries:
            key = entry.path
            # Lower priority number wins — skip if a higher-priority
            # mod already claimed this game_file.
            prior = overrides.get(key)
            if prior is not None and prior["priority"] <= priority:
                continue
            # entry.paz_file already points at the canonicalised
            # stage_root/0.paz so _extract_from_paz reads from there.
            overrides[key] = {
                "mod_id": mod_id,
                "mod_name": mod_name,
                "priority": priority,
                "pamt_dir": pamt_dir,
                "paz_delta_path": paz_delta_path,
                "pamt_delta_path": pamt_delta_path,
                "stage_root": stage_root,  # for later cleanup
                "entry": entry,
            }
    return overrides


def resolve_vanilla_source(
    game_file: str,
    vanilla_dir: Path,
    game_dir: Path,
    snapshot_mgr,
    warn_callback=None,
    paz_dir_overrides: dict | None = None,
):
    """Return a :class:`PazEntry` pointing at a known-clean PAZ.

    Resolution order:

    1. ``vanilla_dir`` — returned as-is when the PAMT entry exists AND
       its PAZ file is present on disk.
    2. ``game_dir`` — returned only when the live PAZ's full-file hash
       equals the snapshot fingerprint stored in the ``snapshots``
       table. Passing ``warn_callback`` is the opportunity to surface
       a one-time InfoBar so users see the self-heal happened and can
       refresh their vanilla backups.
    3. Raise :class:`cdumm.engine.json_patch_handler.VanillaSourceUnavailable`
       with a descriptive reason. Callers should log and skip.

    Extracted from :meth:`ApplyWorker._make_vanilla_source_resolver`
    for unit-test access without needing a full Qt worker fixture.
    """
    from cdumm.engine.json_patch_handler import (
        VanillaSourceUnavailable, _find_pamt_entry,
    )
    from cdumm.engine.snapshot_manager import hash_file

    # #145 cross-layer merge: if an enabled PAZ-dir mod ships this
    # logical file in its own numbered dir, use its bytes as the base
    # instead of vanilla. The mod's entry is already bound to its
    # delta-stored 0.paz so _extract_from_paz will read from there.
    if paz_dir_overrides is not None and game_file in paz_dir_overrides:
        ov = paz_dir_overrides[game_file]
        if warn_callback is not None:
            # Build the complete sentence here so the callback is a
            # thin pass-through. Earlier the callback prepended
            # "Vanilla backup missing for {arg}, using hash-verified
            # live copy ..." to whatever it received, which produced
            # a grammatically broken message when the cross-layer
            # call site passed a complete sentence as the argument
            # (scottykyzer Nexus 2026-05-09 against
            # 'Better Unique Gears' + iteminfo.pabgb on v3.2.13).
            warn_callback(
                f"Mod {ov['mod_name']!r} is providing the base "
                f"for {game_file} (priority={ov['priority']}). "
                f"CDUMM is stacking JSON patches on top of the "
                f"mod's bytes.")
        return ov["entry"]

    vanilla_entry = _find_pamt_entry(game_file, vanilla_dir)
    if vanilla_entry is not None:
        paz_path = Path(vanilla_entry.paz_file)
        if paz_path.exists():
            return vanilla_entry

    live_entry = _find_pamt_entry(game_file, game_dir)
    if live_entry is None:
        raise VanillaSourceUnavailable(
            f"no PAMT entry for '{game_file}' in vanilla or live")

    paz_path = Path(live_entry.paz_file)
    try:
        paz_rel = str(paz_path.relative_to(game_dir)).replace("\\", "/")
    except ValueError:
        paz_rel = paz_path.name

    snap_hash = snapshot_mgr.get_file_hash(paz_rel)
    if snap_hash is None:
        raise VanillaSourceUnavailable(
            f"no snapshot hash for '{paz_rel}' "
            "\u2014 run Settings \u2192 Fix Everything")

    try:
        live_hash, _size = hash_file(paz_path)
    except FileNotFoundError:
        raise VanillaSourceUnavailable(
            f"live PAZ missing at '{paz_path}'") from None

    if live_hash != snap_hash:
        raise VanillaSourceUnavailable(
            f"live PAZ '{paz_rel}' diverged from snapshot "
            "\u2014 cannot safely patch (user has modded the base install)")

    # Lazy backup: copy the verified-vanilla live PAZ + sibling PAMT
    # into vanilla_dir so the next apply finds the backup directly and
    # skips the warn path entirely. Otherwise the warning fires on
    # every apply forever, and the recommended "Run Fix Everything"
    # action doesn't actually create the missing backup (it only
    # backs up archives critical for currently-enabled JSON mods).
    # GitHub #68 (mit999sif).
    # Only attempt lazy backup when paz_rel is a real relative path
    # (contains a directory component). When relative_to(game_dir)
    # fell back to ``paz_path.name`` we'd otherwise write the backup
    # to the wrong location (vanilla_dir/0.paz instead of
    # vanilla_dir/0008/0.paz).
    if "/" in paz_rel:
        try:
            backup_paz = vanilla_dir / paz_rel
            if not backup_paz.exists():
                backup_paz.parent.mkdir(parents=True, exist_ok=True)
                _backup_copy(paz_path, backup_paz)
                sibling_pamt = paz_path.with_suffix(".pamt")
                if sibling_pamt.exists():
                    backup_pamt = backup_paz.with_suffix(".pamt")
                    if not backup_pamt.exists():
                        _backup_copy(sibling_pamt, backup_pamt)
        except Exception as e:
            # Backup failure is non-fatal \u2014 the apply itself can still
            # proceed with the verified-live bytes, the warning just
            # fires again next apply.
            logger.debug(
                "Lazy vanilla backup failed for %s: %s", paz_rel, e)

    if warn_callback is not None:
        # Build the complete sentence here so the callback is a thin
        # pass-through. The worker's deduper used to wrap the paz_rel
        # fragment with the "Vanilla backup missing for X, using hash-
        # verified live copy ..." template; moving the template here
        # lets the cross-layer call site emit its own clean message
        # without inheriting this prefix.
        warn_callback(
            f"Vanilla backup missing for {paz_rel}, using "
            f"hash-verified live copy and creating the backup "
            f"now. Subsequent applies will use the backup "
            f"directly.")
    return live_entry

RANGE_BACKUP_EXT = ".vranges"  # sparse range backup extension


def _backup_copy(src: Path, dst: Path) -> None:
    """Copy a file for vanilla backup. Always a real copy, never a hard link.

    Hard links are unsafe for backups — if a script mod writes directly to
    the game file, it corrupts the backup too (same inode).
    """
    import shutil
    shutil.copy2(src, dst)


def _delta_changes_size(delta_path: Path, vanilla_size: int) -> bool:
    """Check if a delta replaces or resizes the file.

    Returns True for:
    - FULL_COPY deltas (always replace entire file — must be applied first)
    - SPRS deltas that write past vanilla_size
    - bsdiff deltas that produce different size (checked by output size)
    """
    try:
        with open(delta_path, "rb") as f:
            magic = f.read(4)

            if magic == b"FULL":
                # FULL_COPY replaces the entire file — always "changes size"
                # conceptually, even if output happens to be same length.
                # Must be applied before SPRS patches from other mods.
                return True

            if magic == b"BSDI":  # bsdiff4 header "BSDIFF40"
                # bsdiff output size is at offset 16 (8 bytes LE)
                f.seek(16)
                new_size = struct.unpack("<q", f.read(8))[0]
                return new_size != vanilla_size

            if magic == SPARSE_MAGIC:
                count = struct.unpack("<I", f.read(4))[0]
                for _ in range(count):
                    offset = struct.unpack("<Q", f.read(8))[0]
                    length = struct.unpack("<I", f.read(4))[0]
                    if offset + length > vanilla_size:
                        return True
                    f.seek(length, 1)
    except Exception:
        pass
    return False


def _find_insertion_point(delta_path: Path) -> int:
    """Find the first offset in a sparse delta (the insertion/shift point)."""
    try:
        with open(delta_path, "rb") as f:
            f.read(4)  # skip magic
            count = struct.unpack("<I", f.read(4))[0]
            if count > 0:
                offset = struct.unpack("<Q", f.read(8))[0]
                return offset
    except Exception:
        pass
    return 0


def _apply_sparse_shifted(
    buf: bytearray, delta_path: Path, insertion_point: int, shift: int,
) -> None:
    """Apply a sparse delta with offset adjustment for shifted data.

    Entries at or after insertion_point have their offset shifted.
    """
    with open(delta_path, "rb") as f:
        magic = f.read(4)
        if magic != SPARSE_MAGIC:
            return  # can't shift bsdiff
        count = struct.unpack("<I", f.read(4))[0]

        for _ in range(count):
            offset = struct.unpack("<Q", f.read(8))[0]
            length = struct.unpack("<I", f.read(4))[0]
            data = f.read(length)

            # Adjust offset if past the insertion point
            if offset >= insertion_point:
                offset += shift

            end = offset + length
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[offset:end] = data


# ── Range backup helpers ─────────────────────────────────────────────

def _save_range_backup(game_dir: Path, vanilla_dir: Path,
                       file_path: str, byte_ranges: list[tuple[int, int]]) -> None:
    """Save vanilla bytes at specific byte ranges from the game file.

    Stored in sparse format: SPRS + count + (offset, length, data)*

    If a backup already exists, merges new ranges into it — reads any
    not-yet-backed-up positions from the current game file (which must
    still be vanilla at those positions, since backups run before apply).
    """
    game_file = game_dir / file_path.replace("/", os.sep)
    if not game_file.exists():
        return

    backup_path = vanilla_dir / (file_path.replace("/", "_") + RANGE_BACKUP_EXT)
    merged = _merge_ranges(byte_ranges)

    if backup_path.exists():
        # Load existing backup, find ranges not yet covered
        existing = _load_range_backup(vanilla_dir, file_path)
        if existing:
            covered: set[tuple[int, int]] = set()
            for offset, data in existing:
                covered.add((offset, offset + len(data)))
            # Find new ranges not covered by existing backup
            new_ranges: list[tuple[int, int]] = []
            for start, end in merged:
                # Check if this range is already fully covered
                is_covered = any(
                    cs <= start and ce >= end for cs, ce in covered)
                if not is_covered:
                    new_ranges.append((start, end))
            if not new_ranges:
                return  # all ranges already backed up

            # Read new range data from game file and rebuild backup
            all_entries: list[tuple[int, bytes]] = list(existing)
            with open(game_file, "rb") as f:
                for start, end in new_ranges:
                    f.seek(start)
                    all_entries.append((start, f.read(end - start)))

            # Rebuild backup file with all entries, deduplicating
            seen_offsets: dict[int, bytes] = {}
            for offset, data in all_entries:
                if offset not in seen_offsets or len(data) > len(seen_offsets[offset]):
                    seen_offsets[offset] = data
            sorted_entries = sorted(seen_offsets.items())

            buf = bytearray(SPARSE_MAGIC)
            buf += struct.pack("<I", len(sorted_entries))
            for offset, data in sorted_entries:
                buf += struct.pack("<QI", offset, len(data))
                buf += data
            backup_path.write_bytes(bytes(buf))
            logger.info("Range backup updated: %s (+%d new ranges)",
                        file_path, len(new_ranges))
            return

    # First backup — create from scratch
    buf = bytearray(SPARSE_MAGIC)
    buf += struct.pack("<I", len(merged))

    with open(game_file, "rb") as f:
        for start, end in merged:
            length = end - start
            f.seek(start)
            data = f.read(length)
            buf += struct.pack("<QI", start, len(data))
            buf += data

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_bytes(bytes(buf))
    total_bytes = sum(e - s for s, e in merged)
    logger.info("Range backup: %s (%d ranges, %d bytes)",
                file_path, len(merged), total_bytes)


def _load_range_backup(vanilla_dir: Path, file_path: str
                       ) -> list[tuple[int, bytes]] | None:
    """Load a range backup. Returns [(offset, data), ...] or None."""
    backup_path = vanilla_dir / (file_path.replace("/", "_") + RANGE_BACKUP_EXT)
    if not backup_path.exists():
        return None

    raw = backup_path.read_bytes()
    if raw[:4] != SPARSE_MAGIC:
        return None

    entries: list[tuple[int, bytes]] = []
    offset = 4
    count = struct.unpack_from("<I", raw, offset)[0]
    offset += 4

    for _ in range(count):
        file_offset = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        length = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        data = raw[offset:offset + length]
        offset += length
        entries.append((file_offset, data))

    return entries


def _apply_ranges_to_buf(buf: bytearray, entries: list[tuple[int, bytes]]) -> None:
    """Overwrite byte ranges in a buffer."""
    for file_offset, data in entries:
        end = file_offset + len(data)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[file_offset:end] = data


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent byte ranges."""
    if not ranges:
        return []
    sorted_r = sorted(ranges)
    merged = [sorted_r[0]]
    for start, end in sorted_r[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _apply_pamt_entry_update(data: bytearray, update: dict) -> None:
    """Update a single PAMT file record based on entry-level delta changes.

    Finds the record by matching vanilla offset/comp_size/orig_size/flags,
    then updates offset, comp_size, and optionally PAZ size in the header.
    """
    entry = update["entry"]  # PazEntry with vanilla values
    new_comp = update["new_comp_size"]
    new_offset = update["new_offset"]
    new_orig = update.get("new_orig_size", entry.orig_size)
    new_paz_size = update.get("new_paz_size")

    # Update PAZ size table in PAMT header if entry was appended
    if new_paz_size is not None:
        paz_count = struct.unpack_from("<I", data, 4)[0]
        paz_index = entry.paz_index
        if paz_index < paz_count:
            table_off = 16
            for i in range(paz_index):
                table_off += 8
                if i < paz_count - 1:
                    table_off += 4
            size_off = table_off + 4  # skip hash, point to size
            old_size = struct.unpack_from("<I", data, size_off)[0]
            # Use the larger of current and new size (multiple entries may append)
            final_size = max(old_size, new_paz_size)
            struct.pack_into("<I", data, size_off, final_size)
            logger.debug("Updated PAMT PAZ[%d] size: %d -> %d",
                         paz_index, old_size, final_size)

    # Find and update the file record (20 bytes: node_ref + offset + comp + orig + flags)
    search = struct.pack("<IIII", entry.offset, entry.comp_size,
                         entry.orig_size, entry.flags)
    pos = data.find(search)
    if pos >= 4:  # at least 4 bytes for node_ref
        struct.pack_into("<I", data, pos, new_offset)
        struct.pack_into("<I", data, pos + 4, new_comp)
        if new_orig != entry.orig_size:
            struct.pack_into("<I", data, pos + 8, new_orig)
        logger.debug("Patched PAMT record for %s: offset %d->%d, comp %d->%d",
                     entry.path, entry.offset, new_offset,
                     entry.comp_size, new_comp)
    else:
        logger.warning("Could not find PAMT record for %s (offset=0x%X, comp=%d)",
                       entry.path, entry.offset, entry.comp_size)


# ── Workers ──────────────────────────────────────────────────────────

def cdumm_to_xml_priority(cdumm_priority: int) -> int:
    """Map CDUMM priority (lower = wins) to xml_patch_handler priority
    (JMM convention: higher = wins, sorted last).

    xml_patch_handler sorts ASC and lets the last item win, so we negate
    the CDUMM value: CDUMM=1 becomes xml-priority=-1 (largest), sorting
    last and winning conflicts, matching the JSON overlay's winning
    order.
    """
    return -int(cdumm_priority or 0)


class ApplyWorker(QObject):
    """Background worker for apply operation."""

    progress_updated = Signal(int, str)
    finished = Signal()
    error_occurred = Signal(str)
    # Non-fatal messages surfaced to the GUI as InfoBar.warning so users
    # learn about mount-time extraction fallbacks or silently-empty
    # JSON mod overlays without needing to read the log.
    warning = Signal(str)

    def __init__(self, game_dir: Path, vanilla_dir: Path, db_path: Path,
                 force_outdated: bool = False) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir
        self._db_path = db_path
        self._force_outdated = force_outdated
        self._soft_warnings: list[str] = []

    def run(self) -> None:
        try:
            self._db = Database(self._db_path)
            self._db.initialize()
            self._apply()
            self._db.close()
        except FileNotFoundError as e:
            # Bug C (Nexus 2026-05-03, Jyoungy13): bare
            # [WinError 2] strings have no path. Surface
            # e.filename so users can see which vanilla file
            # vanished after a game patch.
            logger.error("Apply failed: %s", e, exc_info=True)
            path = getattr(e, "filename", None) or "(unknown path)"
            self.error_occurred.emit(
                f"Apply failed: file not found: {path} ({e})")
        except Exception as e:
            logger.error("Apply failed: %s", e, exc_info=True)
            self.error_occurred.emit(f"Apply failed: {e}")

    def _make_vanilla_source_resolver(self):
        """Wire :func:`resolve_vanilla_source` to this worker's state.

        Emits :attr:`warning` once per distinct archive that falls back
        to the hash-verified live PAZ, so users see the self-heal
        instead of silently depending on it.
        """
        from cdumm.engine.snapshot_manager import SnapshotManager
        snapshot_mgr = SnapshotManager(self._db)
        warned_once: set[str] = set()

        def _warn(msg: str) -> None:
            # resolve_vanilla_source now passes complete sentences for
            # both the self-heal path and the cross-layer base path.
            # This used to prepend "Vanilla backup missing for X, ..."
            # to whatever the caller passed, which produced grammatical
            # nonsense when the cross-layer call site passed a
            # complete sentence (scottykyzer Nexus 2026-05-09 with
            # 'Better Unique Gears' on iteminfo.pabgb v3.2.13).
            if msg in warned_once:
                return
            warned_once.add(msg)
            logger.warning("mount-time: %s", msg)
            self._soft_warnings.append(msg)
            self.warning.emit(msg)

        # #145 cross-layer override map was already built at _apply()
        # start (so Phase 1 can skip direct-staging overridden files).
        # Read it from self here; don't re-collect.
        if not hasattr(self, "_paz_dir_overrides"):
            _fallback_warn_before = len(self._soft_warnings)
            self._paz_dir_overrides = collect_paz_dir_overrides(
                self._db, warnings_out=self._soft_warnings)
            for _w in self._soft_warnings[_fallback_warn_before:]:
                self.warning.emit(_w)

        def resolver(game_file: str):
            return resolve_vanilla_source(
                game_file, self._vanilla_dir, self._game_dir,
                snapshot_mgr, warn_callback=_warn,
                paz_dir_overrides=self._paz_dir_overrides,
            )
        return resolver

    def _paz_dir_file_paths_to_skip(self) -> set[str]:
        """Return `NNNN/0.paz` + `NNNN/0.pamt` file_paths whose content
        is being layered via the cross-layer override path and should
        NOT be staged directly in Phase 1. These mods' entries flow
        into the overlay with JSON patches applied on top.
        """
        to_skip: set[str] = set()
        for ov in (getattr(self, "_paz_dir_overrides", None) or {}).values():
            to_skip.add(f"{ov['pamt_dir']}/0.paz")
            to_skip.add(f"{ov['pamt_dir']}/0.pamt")
        return to_skip

    def _warn_entr_load_failure(self, d: dict, exc: Exception) -> None:
        """Surface a user-visible warning when an entry delta can't be
        loaded (missing ENTRY_MAGIC, truncated file, decode failure).

        Bug D (Nexus 2026-05-03, Torie1985): Barber Unlocked's
        customizationcolorpalette.xml.entr lacked the 'ENTR' magic
        header so load_entry_delta raised ValueError; apply caught
        the exception, logged to logger only, and silently continued.
        The user saw zero errors and zero in-game effect.

        The fix: emit to self.warning AND log, so users see an
        actionable message in the GUI / CLI and know to re-import
        the mod (typical cause: legacy import format or file system
        corruption since import).
        """
        mod_name = d.get("mod_name") or "(unknown mod)"
        delta_path = d.get("delta_path", "?")
        msg = (
            f"Mod '{mod_name}' has a corrupt entry delta — "
            f"re-import the mod to regenerate it. "
            f"Affected file: {delta_path}. Error: {exc}"
        )
        logger.warning(
            "Entry delta load failed for %s (%s): %s",
            mod_name, delta_path, exc)
        if hasattr(self, "_soft_warnings"):
            self._soft_warnings.append(msg)
        try:
            self.warning.emit(msg)
        except Exception:
            pass

    def _stage_with_pamt_tracking(
        self, txn, file_path: str, data: bytes,
        modified_pamts: dict[str, bytes],
    ) -> None:
        """Stage a file via the transactional IO and, when the path is
        a .pamt, also record the bytes in modified_pamts so PapgtManager.
        rebuild() hashes the staged bytes instead of the stale on-disk
        copy.

        Bug A (Nexus 2026-05-03): michael2k + timelesscjing reported
        post-apply verification mismatches on 0009 / 0015 PAMT entries
        after game patch 1.05.00. Mods that ship complete PAMTs as
        is_new entries (or as FULL_COPY deltas) were going through
        stage_file call sites that didn't update modified_pamts. Result:
        PAPGT entry held the hash of the pre-apply PAMT (whatever was
        on disk), but commit then replaced it with the mod's bytes —
        hash mismatch every time.

        All stage_file calls that may target a PAMT MUST go through
        this helper so the next refactor can't accidentally diverge.
        """
        txn.stage_file(file_path, data)
        if file_path.endswith(".pamt"):
            dir_name = file_path.split("/")[0]
            modified_pamts[dir_name] = data

    def _compute_apply_fingerprint(self) -> str:
        """Compute a hash of all inputs that affect Apply output.

        If this fingerprint matches the last Apply, the game files are
        already correct and the entire Apply can be skipped.

        GitHub #59 (DoRoon, 2026-05-01): priority, conflict_mode,
        force_inplace, and mod_config.custom_values all change apply
        output — they must each be in the hash or drag-reorder /
        slot-edit / override-toggle gets silently skipped.
        """
        import hashlib
        h = hashlib.sha256()

        rows = self._db.connection.execute(
            "SELECT m.id, m.enabled, m.json_source, m.disabled_patches, "
            "       m.priority, m.conflict_mode, m.force_inplace, "
            "       mc.custom_values, "
            "       GROUP_CONCAT(md.delta_path || ':' || md.file_path, '|') "
            "FROM mods m "
            "LEFT JOIN mod_deltas md ON m.id = md.mod_id "
            "LEFT JOIN mod_config mc ON mc.mod_id = m.id "
            "GROUP BY m.id ORDER BY m.id"
        ).fetchall()
        for row in rows:
            h.update(
                f"{row[0]}:{row[1]}:{row[2]}:{row[3]}:"
                f"{row[4]}:{row[5]}:{row[6]}:{row[7]}:{row[8]}\n".encode()
            )

        return h.hexdigest()[:16]

    def _apply(self) -> None:
        _t0 = time.perf_counter()
        def _phase(name):
            elapsed = time.perf_counter() - _t0
            logger.info("APPLY PHASE [%.1fs]: %s", elapsed, name)

        _phase("Starting apply")

        # Fast-path: check if game files already match the current mod state
        import json as _json_mod
        fingerprint = self._compute_apply_fingerprint()
        from cdumm.storage.config import Config as _Config
        _cdmods = get_cdmods_root(_Config(self._db), self._game_dir)
        fp_path = _cdmods / ".apply_fingerprint"
        try:
            if fp_path.exists():
                stored = fp_path.read_text(encoding="utf-8").strip()
                if stored == fingerprint:
                    # Verify overlay PAZ is intact
                    cache_json = _cdmods / ".overlay_cache.json"
                    if cache_json.exists():
                        manifest = _json_mod.loads(cache_json.read_text(encoding="utf-8"))
                        overlay_dir = manifest.get("_overlay_dir", "")
                        overlay_paz = self._game_dir / overlay_dir / "0.paz" if overlay_dir else None
                        if overlay_paz and overlay_paz.exists():
                            _phase("SKIPPED — game files already up to date")
                            self.progress_updated.emit(100, "Already up to date!")
                            self.finished.emit()
                            return
                    # No overlay needed (all mods disabled?) — check PAPGT
                    papgt = self._game_dir / "meta" / "0.papgt"
                    if papgt.exists():
                        _phase("SKIPPED — game files already up to date (no overlay)")
                        self.progress_updated.emit(100, "Already up to date!")
                        self.finished.emit()
                        return
        except Exception as e:
            logger.debug("Fingerprint check failed, proceeding with full apply: %s", e)

        file_deltas = self._get_file_deltas()
        revert_files = self._get_files_to_revert(set(file_deltas.keys()))
        _phase(f"Loaded {len(file_deltas)} file deltas, {len(revert_files)} reverts")

        # JMM-parity localisation redirect. If a standalone PAZ mod targets
        # a language group (0019-0032) that doesn't match the user's Steam
        # language, rename the delta keys to point at the user's group and
        # rewrite the PAMT's embedded .paloc filename in one pass so the
        # game's VFS resolves the mod under the correct language slot.
        file_deltas, revert_files = self._apply_language_redirect(
            file_deltas, revert_files)

        # #145 Option Y: enabled JSON mods with json_source do their
        # work in Phase 1a via mount-time aggregation, not through
        # file_deltas. Don't early-exit just because file_deltas is
        # empty — check for enabled JSON mods too.
        has_enabled_json = self._db.connection.execute(
            "SELECT 1 FROM mods WHERE enabled = 1 "
            "AND json_source IS NOT NULL AND json_source != '' "
            "LIMIT 1").fetchone() is not None
        if not file_deltas and not revert_files and not has_enabled_json:
            self.error_occurred.emit("No mod changes to apply or revert.")
            return

        # Entry-level deltas (from script mods) require updating the PAMT
        # after PAZ composition. Track updates here for Phase 2.
        self._pamt_entry_updates: dict[str, list[dict]] = {}

        # Overlay PAZ: collect ENTR delta entries to write to an overlay
        # directory instead of modifying original game PAZ files in-place.
        self._overlay_entries: list[tuple[bytes, dict]] = []

        # Pre-load overlay cache before orphan cleanup deletes the previous overlay
        from cdumm.archive.overlay_builder import _load_overlay_cache
        from cdumm.storage.config import Config as _Config
        self._cached_overlay = _load_overlay_cache(
            self._game_dir, config=_Config(self._db))
        self._overlay_dir_name: str | None = None

        # Also ensure PAMTs are backed up for directories with entry deltas
        entry_pamt_dirs = set()
        for file_path, deltas in file_deltas.items():
            if any(d.get("entry_path") for d in deltas):
                entry_pamt_dirs.add(file_path.split("/")[0])

        all_files = set(file_deltas.keys()) | set(revert_files)
        total_files = len(all_files) + len(entry_pamt_dirs)
        self.progress_updated.emit(0, f"Applying {total_files} file(s)...")

        # Ensure vanilla backups exist BEFORE any modifications.
        # Skip if all backups already exist (common case after first apply).
        needs_backup = False
        for file_path in all_files:
            delta_infos = file_deltas.get(file_path, [])
            if all(d.get("is_new") for d in delta_infos) and delta_infos:
                continue
            full_path = self._vanilla_dir / file_path.replace("/", os.sep)
            range_path = self._vanilla_dir / (file_path.replace("/", "_") + RANGE_BACKUP_EXT)
            if not full_path.exists() and not range_path.exists():
                needs_backup = True
                break

        if needs_backup:
            self.progress_updated.emit(2, "Backing up vanilla files...")
            unbacked = self._ensure_backups(file_deltas, revert_files)
            if unbacked:
                files_str = ", ".join(unbacked[:5])
                if len(unbacked) > 5:
                    files_str += f" (+{len(unbacked) - 5} more)"
                self.error_occurred.emit(
                    f"Cannot apply — these game files don't match vanilla "
                    f"and can't be safely backed up:\n\n{files_str}\n\n"
                    f"To fix: Verify game files through Steam, then click "
                    f"Fix Everything (say Yes to Steam verify)."
                )
                return
        # Ensure PAMT backups for directories with entry-level deltas
        for pamt_dir in entry_pamt_dirs:
            pamt_path = f"{pamt_dir}/0.pamt"
            full_path = self._vanilla_dir / pamt_path.replace("/", os.sep)
            if not full_path.exists():
                game_pamt = self._game_dir / pamt_path.replace("/", os.sep)
                if game_pamt.exists():
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    _backup_copy(game_pamt, full_path)
                    logger.info("Full vanilla backup (PAMT for entries): %s", pamt_path)

        staging_dir = self._game_dir / ".cdumm_staging"
        staging_dir.mkdir(exist_ok=True)
        txn = TransactionalIO(self._game_dir, staging_dir)
        modified_pamts: dict[str, bytes] = {}
        # Destructive deletions collected during the run and executed
        # ONLY after txn.commit() succeeds; a failed commit rolls back
        # to PAMTs/PAPGT that still reference them (audit C1).
        deferred_file_deletions: list[Path] = []
        deferred_dir_deletions: list[Path] = []

        # #145 cross-layer merge: build the PAZ-dir override map once
        # upfront so Phase 1 knows which `NNNN/0.paz` file_paths to
        # skip (their content flows through the overlay instead).
        # Bug-A fix: only skip staging when an ENABLED JSON mod will
        # actually patch that file. Skipping without a replacement
        # leaves an orphaned PAMT pointing at a missing PAZ and the
        # game crashes on load.
        # B3: upfront corrupt-archive precheck. Runs BEFORE the
        # cross-layer override build so users see the warning at the
        # top of apply, not mid-run.
        for _pw in precheck_enabled_mod_pamts(self._db):
            self._soft_warnings.append(_pw)
            self.warning.emit(_pw)

        # A2 fix: forward any pamt-parse failures through
        # self._soft_warnings so the GUI InfoBar surfaces "mod X has
        # a corrupt archive" instead of silently losing the mod.
        _dedup_warn_before = len(self._soft_warnings)
        self._paz_dir_overrides = collect_paz_dir_overrides(
            self._db, warnings_out=self._soft_warnings)
        for _w in self._soft_warnings[_dedup_warn_before:]:
            self.warning.emit(_w)
        _json_targets = collect_enabled_json_targets(self._db)
        _active_overrides = {
            k: v for k, v in self._paz_dir_overrides.items()
            if k in _json_targets
        }
        # The FULL map stays on self for the resolver (it's safe to
        # return the override entry even if no JSON mod hits it —
        # that branch just never fires). But `skip_paz_dir_files`
        # restricts to ACTIVE overrides only.
        skip_paz_dir_files: set[str] = set()
        for ov in _active_overrides.values():
            skip_paz_dir_files.add(f"{ov['pamt_dir']}/0.paz")
            skip_paz_dir_files.add(f"{ov['pamt_dir']}/0.pamt")
        if self._paz_dir_overrides:
            logger.info(
                "cross-layer: %d PAZ-dir override(s) available, "
                "%d active (JSON mod patches the same file): %s",
                len(self._paz_dir_overrides),
                len(_active_overrides),
                sorted(skip_paz_dir_files) or "[none — PAZ-dir mods "
                "stage normally]")

        try:
            file_idx = 0

            _phase("Phase 1: Compose PAZ files")
            # ── Phase 1: Compose PAZ and other non-PAMT files ──────────
            for file_path, deltas in file_deltas.items():
                pct = int((file_idx / total_files) * 55)
                self.progress_updated.emit(pct, f"Processing {file_path}...")
                file_idx += 1
                _yield_gil()

                # Skip PAPGT (rebuilt at end) and PAMT (Phase 2)
                if file_path == "meta/0.papgt":
                    continue
                if file_path.endswith(".pamt"):
                    continue

                # #145 cross-layer merge: this NNNN/0.paz is being
                # layered via overlay (a JSON mod is patching its
                # content). Don't stage it directly — skip here and
                # let Phase 1a feed its bytes into the overlay as the
                # JSON-patch base.
                if file_path in skip_paz_dir_files:
                    logger.info(
                        "cross-layer: skipping direct stage of %s — "
                        "JSON patches will layer onto overlay",
                        file_path)
                    continue

                # New files: copy from stored full file (last mod wins)
                new_deltas = [d for d in deltas if d.get("is_new")]
                mod_deltas = [d for d in deltas if not d.get("is_new")]

                if new_deltas and not mod_deltas:
                    src = Path(new_deltas[-1]["delta_path"])
                    if src.exists():
                        result_bytes = src.read_bytes()
                        # Bug A: route through helper so PAMT-shipping
                        # mods don't desync modified_pamts vs disk.
                        self._stage_with_pamt_tracking(
                            txn, file_path, result_bytes, modified_pamts)
                        logger.info("Applying new file: %s from %s",
                                    file_path, new_deltas[-1]["mod_name"])
                    continue

                # Fast-track: single mod with FULL_COPY delta — stream-copy
                # directly instead of loading 900MB+ into memory
                if len(mod_deltas) == 1 and not mod_deltas[0].get("entry_path"):
                    dp = Path(mod_deltas[0]["delta_path"])
                    try:
                        with open(dp, "rb") as f:
                            magic = f.read(4)
                        if magic == b"FULL" and dp.stat().st_size > 50 * 1024 * 1024:
                            # Stream the FULL_COPY content directly to staging
                            with open(dp, "rb") as f:
                                f.seek(4)  # skip FULL magic
                                result_bytes = f.read()
                            # Bug A: PAMT-aware staging
                            self._stage_with_pamt_tracking(
                                txn, file_path, result_bytes, modified_pamts)
                            logger.info("Fast-track apply: %s (%.1f MB)",
                                        file_path, len(result_bytes) / 1048576)
                            continue
                    except OSError:
                        pass

                result_bytes = self._compose_file(file_path, mod_deltas)
                if result_bytes is None:
                    continue

                # Bug A: PAMT-aware staging
                self._stage_with_pamt_tracking(
                    txn, file_path, result_bytes, modified_pamts)


            _phase("Phase 1a: Mount-time JSON patching")
            # ── Phase 1a: Mount-time patching for JSON mods ──────────
            # JSON mods with json_source are patched fresh from vanilla
            # at Apply time (no stored ENTR deltas needed).
            # CDUMM convention (mod_manager.py:541): **lower priority
            # number wins**. merge_compiled_mod_files (compiled_merge.py:47-50)
            # expects "lowest priority first, highest last" in its input
            # list so the highest-priority mod's bytes win in any overlap.
            # Process LOW-precedence (high priority number) mods FIRST, so
            # the HIGH-precedence (priority=1) mod applies LAST and wins.
            # Use DESC so 10, 9, ..., 1 feeds merge in the correct order.
            json_mods = self._db.connection.execute(
                "SELECT id, name FROM mods "
                "WHERE enabled = 1 AND json_source IS NOT NULL "
                "AND json_source != ''"
            ).fetchall()
            if json_mods:
                from cdumm.engine.json_patch_handler import (
                    process_json_patches_for_overlay,
                )
                import json as _json
                resolver = self._make_vanilla_source_resolver()
                overlay_count_before = len(self._overlay_entries)

                # #145 Option Y — aggregate ALL enabled JSON mods'
                # patches into ONE combined pass so cumulative-offset
                # tracking inside _apply_byte_patches works correctly
                # across mod boundaries. Without this, two mods both
                # targeting iteminfo.pabgb (e.g. Fat Stacks + Extra
                # Sockets) produce separate overlay entries that can't
                # be byte-merged when one mod inserts bytes and the
                # other doesn't.
                synth_data, mod_summary = (
                    aggregate_json_mods_into_synthetic_patches(self._db))

                # Phase 4 of #208: expand Format 3 mods alongside v2
                # patches. Format 3 mods don't have "patches" keys, so
                # the v2 aggregator above didn't pick them up. Resolve
                # their intents into v2-style change dicts and append
                # to the same synth_data so the rest of the apply
                # pipeline doesn't need to know which side a change
                # came from.
                f3_warnings: list[str] = []
                # Format 3 mods report their contributing mod ids
                # here so persist_skip_summary can reset rows on a
                # clean apply (H2 fix).
                f3_participating: set[int] = set()
                _expand_format3_into_synth_data(
                    synth_data, self._db,
                    self._vanilla_dir, self._game_dir,
                    self._get_vanilla_entry_content,
                    self._extract_sibling_entry,
                    warnings_out=f3_warnings,
                    participating_mod_ids=f3_participating)
                if f3_warnings:
                    # Same surfacing pattern v3.2.1's skipped-patches
                    # feature uses — InfoBar after Apply via the
                    # warning signal.
                    msg = (
                        f"{len(f3_warnings)} Format 3 mod(s) produced "
                        f"0 byte changes:\n\n"
                        + "\n\n".join(f"- {w}" for w in f3_warnings[:5])
                    )
                    if len(f3_warnings) > 5:
                        msg += (
                            f"\n\n…and {len(f3_warnings) - 5} more "
                            f"(see Activity log for full list)."
                        )
                    self.warning.emit(msg)

                logger.info(
                    "Phase 1a: aggregated %d JSON mod(s) into %d target "
                    "file(s) for single-pass patching",
                    len(mod_summary), len(synth_data.get("patches", [])))
                for m in mod_summary:
                    # Format 3 mods route through a separate writer
                    # pipeline (cdumm.engine.format3_apply); annotate the
                    # display line so the count is read as "intents this
                    # mod ships" not "bytes written through this path".
                    # The Format 3 apply summary logs actual write counts
                    # at INFO level immediately after this loop.
                    if m.get("is_format3"):
                        logger.info(
                            "  mod %d '%s' (priority=%d): %d Format 3 "
                            "intent(s) → %s (applied via Format 3 "
                            "pipeline, see Format 3 apply summary)",
                            m["mod_id"], m["mod_name"], m["priority"],
                            m["change_count"], m["targets"])
                    else:
                        logger.info(
                            "  mod %d '%s' (priority=%d): %d change(s) → %s",
                            m["mod_id"], m["mod_name"], m["priority"],
                            m["change_count"], m["targets"])

                if synth_data.get("patches"):
                    # Write synth JSON to a temp file so
                    # process_json_patches_for_overlay (which reads
                    # json_source from disk) can consume it. Tracked
                    # via temp_workspace (cdumm_agg_ is in
                    # CDUMM_PREFIXES) and released in the apply's
                    # finally block; the JSON is consumed within this
                    # run.
                    from cdumm.engine.temp_workspace import (
                        make_temp_dir as _make_temp_dir)
                    synth_temp = _make_temp_dir("cdumm_agg_")
                    self._synth_temp = synth_temp
                    synth_path = synth_temp / "aggregated.json"
                    synth_path.write_text(
                        _json.dumps(synth_data), encoding="utf-8")

                    self.progress_updated.emit(
                        55, f"Patching {len(synth_data['patches'])} "
                        "target file(s) from vanilla...")
                    patch_errors: list[str] = []
                    patch_skips: list[dict] = []
                    from cdumm.storage.config import Config as _Config
                    entries = process_json_patches_for_overlay(
                        0,  # pseudo mod_id — no single mod owns this
                        str(synth_path), self._game_dir,
                        disabled_indices=None,
                        custom_values=None,
                        vanilla_source_resolver=resolver,
                        errors_out=patch_errors,
                        skipped_out=patch_skips,
                        config=_Config(self._db))
                    # JMM-parity UX: surface the per-patch skip details
                    # to the user. CDUMM previously logged these at
                    # debug-level only, so users with mods that
                    # partially-fail (mod built for an older game
                    # version where some byte offsets shifted) saw
                    # "Apply complete" with no warning and concluded
                    # CDUMM was broken when in fact 121/140 patches
                    # applied and 19 silently skipped. JMM v9.9.3
                    # prints "121 applied, 19 skipped" inline; this
                    # match brings parity. The skip list goes to the
                    # warning signal which on_apply_done renders in
                    # the post-apply InfoBar.
                    if patch_skips:
                        # Whole-table changes carry the FULL file as
                        # expected/actual hex (5+ MB); interpolating
                        # them raw turned the post-apply warning into
                        # an unreadable hex wall (falobos76, #191
                        # retest). Show a short prefix + total size.
                        # Shared formatter also writes these lines to the
                        # log at WARNING so a saved bug report records them
                        # (issue #222) — not just the transient InfoBar.
                        skip_lines, more = log_patch_skips(patch_skips)
                        suffix = (f"\n  ... and {more} more"
                                  if more else "")
                        msg = (
                            f"{len(patch_skips)} JSON patch(es) "
                            "skipped because the bytes they expect "
                            "don't match the current game. The mod "
                            "was probably built for an older game "
                            "version. Affected entries:\n"
                            + "\n".join(skip_lines) + suffix)
                        try:
                            self.warning.emit(msg)
                        except Exception:
                            pass
                        if hasattr(self, "_soft_warnings"):
                            self._soft_warnings.append(msg)
                    # Persist per-mod skip results so the mod card can
                    # render a yellow badge after the toast dismisses.
                    # Resets cleanly for mods that participated this
                    # apply with no skips, so the badge clears when the
                    # user fixes the underlying issue (skipped-mod
                    # badge work, chunk 2A).
                    try:
                        participating = {m["mod_id"] for m in mod_summary}
                        # Union in Format 3 contributors so their rows
                        # also clear on a clean apply (H2 fix).
                        participating |= f3_participating
                        persist_skip_summary(
                            self._db.connection, patch_skips, participating)
                    except Exception as _e:
                        logger.debug(
                            "persist_skip_summary failed: %s", _e)
                    # Tag each overlay entry with the LOWEST priority
                    # number among its contributors (lowest = highest
                    # precedence = CDUMM winner on downstream collisions
                    # like mixed JSON+XML).
                    min_priority_per_target = {}
                    for m in mod_summary:
                        for gf in m["targets"]:
                            prior = min_priority_per_target.get(
                                gf, float("inf"))
                            if m["priority"] < prior:
                                min_priority_per_target[gf] = m["priority"]
                    for _body, _meta in entries:
                        ep = _meta.get("entry_path", "")
                        # derive game_file from entry_path — JSON mods
                        # target paths that match directly.
                        for gf, prio in min_priority_per_target.items():
                            # Match on suffix so "gamedata/iteminfo.pabgb"
                            # matches entry_paths with folder prefix.
                            if ep.endswith(gf) or ep == gf:
                                _meta.setdefault("priority", prio)
                                break
                        _meta.setdefault("mod_name", "aggregated JSON")
                        _meta.setdefault("_aggregated_from",
                                          len(mod_summary))
                    self._overlay_entries.extend(entries)
                    if entries:
                        logger.info(
                            "Mount-time: aggregated %d JSON mod(s) → "
                            "%d overlay entries",
                            len(mod_summary), len(entries))
                    # Build {game_file: [mod_name, ...]} so we can replace
                    # the synth-named error with the real contributing mod
                    # names. Without this the user sees "aggregated: all
                    # N patches mismatched..." which is unhelpful — it
                    # names the temp synth file, not the mod(s) at fault.
                    targets_to_mods: dict[str, list[str]] = {}
                    for m in mod_summary:
                        for gf in m["targets"]:
                            targets_to_mods.setdefault(gf, []).append(
                                m["mod_name"])

                    for err in patch_errors:
                        logger.warning("Mount-time abort: %s", err)
                        # Try to rewrite the synth-named error with real
                        # mod names. If we can't parse out the game_file
                        # (different message shape), fall through to the
                        # original text rather than dropping context.
                        friendly = _rewrite_mount_error_with_mod_names(
                            err, targets_to_mods)
                        self._soft_warnings.append(friendly)
                        self.warning.emit(friendly)

                    # Keep the temp dir alive until apply finishes;
                    # Python's tempfile cleanup on process exit is
                    # adequate for a ~KB synth file.
                # Task 1.3: loud error when enabled JSON mods produced no
                # overlay at all — the user thought their mods applied but
                # mount-time extraction failed silently for every target.
                if len(self._overlay_entries) == overlay_count_before:
                    msg = _build_silent_apply_failure_message(mod_summary)
                    logger.error("APPLY_SILENT_FAILURE: %s", msg)
                    self._soft_warnings.append(msg)
                    self.warning.emit(msg)

            _phase("Phase 1a-xml: Mount-time XML patches")
            # Collect xml_patch / xml_merge deltas for all enabled mods and
            # feed them to xml_patch_handler.process_xml_patches_for_overlay.
            # xml_patch_handler follows JMM convention ("higher priority
            # number wins, executes last") but CDUMM uses the inverse
            # ("priority=1 is top, wins"). Transform priorities via
            # cdumm_to_xml_priority so the handler's ASC sort places
            # CDUMM's lowest-priority mods LAST.
            xml_rows = self._db.connection.execute(
                "SELECT d.mod_id, m.name, d.kind, d.delta_path, d.file_path, "
                "m.priority FROM mod_deltas d "
                "JOIN mods m ON m.id = d.mod_id "
                "WHERE m.enabled = 1 AND d.kind IN ('xml_patch', 'xml_merge') "
                "ORDER BY m.priority DESC, d.id ASC"
            ).fetchall()
            if xml_rows:
                self.progress_updated.emit(58, f"Applying XML patches ({len(xml_rows)})...")
                from cdumm.engine.xml_patch_handler import process_xml_patches_for_overlay
                items = [
                    {
                        "mod_id": mod_id, "mod_name": mod_name, "kind": kind,
                        "delta_path": dp, "file_path": fp,
                        "priority": cdumm_to_xml_priority(prio),
                    }
                    for (mod_id, mod_name, kind, dp, fp, prio) in xml_rows
                ]
                from cdumm.storage.config import Config as _Config
                xml_entries = process_xml_patches_for_overlay(
                    items, self._game_dir, config=_Config(self._db))
                # Stamp CDUMM priority onto each XML overlay entry so
                # the merge function can resolve mixed JSON+XML
                # collisions by priority instead of feed order (C-H6).
                # XML merges every item targeting one file into one
                # entry; use the MIN CDUMM priority among contributors
                # (lowest number = CDUMM winner).
                xml_min_priority_by_target: dict[str, int] = {}
                for _mod_id, _mod_name, _kind, _dp, fp, prio in xml_rows:
                    if fp not in xml_min_priority_by_target or \
                            prio < xml_min_priority_by_target[fp]:
                        xml_min_priority_by_target[fp] = prio
                for _body, _meta in xml_entries:
                    # entry_path may include the 'NNNN/' prefix or not;
                    # try both shapes for a lookup.
                    ep = _meta.get("entry_path", "")
                    pamt_dir = _meta.get("pamt_dir", "")
                    candidates = {
                        ep, f"{pamt_dir}/{ep}" if pamt_dir else ep,
                        ep.split("/", 1)[-1] if "/" in ep else ep,
                    }
                    for k in candidates:
                        if k in xml_min_priority_by_target:
                            _meta.setdefault(
                                "priority",
                                xml_min_priority_by_target[k])
                            break
                self._overlay_entries.extend(xml_entries)
                if xml_entries:
                    logger.info("xml_patch: produced %d overlay entries from %d deltas",
                                len(xml_entries), len(xml_rows))

            _phase("Phase 1a-css: Mount-time CSS patches")
            # CSS patch / merge mods (JMM v9.9.3 parity). Same priority
            # transform as XML — CDUMM low priority = highest winner,
            # handler sorts ASC and lets the last item win.
            css_rows = self._db.connection.execute(
                "SELECT d.mod_id, m.name, d.kind, d.delta_path, d.file_path, "
                "m.priority FROM mod_deltas d "
                "JOIN mods m ON m.id = d.mod_id "
                "WHERE m.enabled = 1 AND d.kind IN ('css_patch', 'css_merge') "
                "ORDER BY m.priority DESC, d.id ASC"
            ).fetchall()
            if css_rows:
                self.progress_updated.emit(
                    59, f"Applying CSS patches ({len(css_rows)})...")
                from cdumm.engine.css_patch_handler import (
                    process_css_patches_for_overlay,
                )
                items = [
                    {
                        "mod_id": mod_id, "mod_name": mod_name,
                        "kind": kind, "delta_path": dp,
                        "file_path": fp,
                        "priority": cdumm_to_xml_priority(prio),
                    }
                    for (mod_id, mod_name, kind, dp, fp, prio) in css_rows
                ]
                from cdumm.storage.config import Config as _Config
                css_entries = process_css_patches_for_overlay(
                    items, self._game_dir, config=_Config(self._db))
                self._overlay_entries.extend(css_entries)
                if css_entries:
                    logger.info(
                        "css_patch: produced %d overlay entries from "
                        "%d deltas", len(css_entries), len(css_rows))

            _phase("Phase 1a-html: Mount-time HTML patches")
            html_rows = self._db.connection.execute(
                "SELECT d.mod_id, m.name, d.kind, d.delta_path, d.file_path, "
                "m.priority FROM mod_deltas d "
                "JOIN mods m ON m.id = d.mod_id "
                "WHERE m.enabled = 1 AND d.kind IN ('html_patch', 'html_merge') "
                "ORDER BY m.priority DESC, d.id ASC"
            ).fetchall()
            if html_rows:
                self.progress_updated.emit(
                    60, f"Applying HTML patches ({len(html_rows)})...")
                from cdumm.engine.html_patch_handler import (
                    process_html_patches_for_overlay,
                )
                items = [
                    {
                        "mod_id": mod_id, "mod_name": mod_name,
                        "kind": kind, "delta_path": dp,
                        "file_path": fp,
                        "priority": cdumm_to_xml_priority(prio),
                    }
                    for (mod_id, mod_name, kind, dp, fp, prio) in html_rows
                ]
                from cdumm.storage.config import Config as _Config
                html_entries = process_html_patches_for_overlay(
                    items, self._game_dir, config=_Config(self._db))
                self._overlay_entries.extend(html_entries)
                if html_entries:
                    logger.info(
                        "html_patch: produced %d overlay entries from "
                        "%d deltas", len(html_entries), len(html_rows))

            # Merge overlay entries that target the same (pamt_dir, entry_path).
            # Without this, two JSON mods patching iteminfo.pabgb produce two
            # separate overlay entries and only one wins in PAMT — the other
            # mod silently drops. butanokaabii's "same-file mods no longer
            # combine" regression report traces to this. Mirrors the JMM-parity
            # MergeCompiledModFiles byte-merge already used for ENTR deltas.
            self._overlay_entries = self._merge_same_target_overlay_entries(
                self._overlay_entries)

            # ── Cross-layer overlay de-dup ──────────────────────────
            # When multiple apply phases contributed an entry for the
            # same (pamt_dir, entry_path) — e.g. JSON Phase 1a + an
            # ENTR rewrite both targeting the same prefab — collapse
            # them into a single byte-merged entry instead of silently
            # priority-picking one. Runs BEFORE Phase 1b so the
            # overlay builder sees one entry per target.
            if self._overlay_entries:
                from cdumm.engine.overlay_dedup import (
                    merge_duplicate_overlay_entries,
                )

                def _dedup_vanilla_resolver(pamt_dir: str,
                                             entry_path: str):
                    if not entry_path:
                        return None
                    # _get_vanilla_entry_content takes (file_path, entry_path)
                    # where file_path is a PAMT-relative archive path (e.g.
                    # '0009/0.paz'). pamt_dir alone isn't enough — but the
                    # canonical "0.paz" suffix is what vanilla stores.
                    try:
                        return self._get_vanilla_entry_content(
                            f"{pamt_dir}/0.paz", entry_path)
                    except Exception as e:
                        logger.debug(
                            "dedup resolver failed for %s/%s: %s",
                            pamt_dir, entry_path, e)
                        return None

                before_count = len(self._overlay_entries)
                merged_entries, dedup_warnings = (
                    merge_duplicate_overlay_entries(
                        self._overlay_entries, _dedup_vanilla_resolver))
                self._overlay_entries = merged_entries
                if before_count != len(merged_entries):
                    logger.info(
                        "overlay-dedup: %d entries collapsed into %d",
                        before_count, len(merged_entries))
                for w in dedup_warnings:
                    self._soft_warnings.append(w)
                    try:
                        self.warning.emit(w)
                    except Exception:
                        pass

            _phase(f"Phase 1b: Build overlay ({len(self._overlay_entries)} entries)")
            # ── Phase 1b: Build overlay PAZ for ENTR deltas ─────────
            if self._overlay_entries:
                # Collect directories claimed by standalone mods so overlay
                # doesn't collide with them
                _staged_mod_dirs = set()
                for fp in file_deltas:
                    d = fp.split("/")[0]
                    if d.isdigit() and len(d) == 4 and int(d) >= 36:
                        _staged_mod_dirs.add(d)

                # Restore vanilla PAZ/PAMT for directories moving to overlay.
                # Users upgrading from v2.1.7 (in-place) have modified PAZ/PAMT
                # files that must be reverted before overlay takes over.
                overlay_dirs_used = set()
                for _, meta in self._overlay_entries:
                    paz_file = meta.get("pamt_dir", "")
                    if paz_file:
                        overlay_dirs_used.add(paz_file)
                for od in overlay_dirs_used:
                    for suffix in ["0.pamt"]:
                        vanilla_path = self._vanilla_dir / od / suffix
                        if vanilla_path.exists():
                            game_path = self._game_dir / od / suffix
                            if game_path.exists():
                                snap = self._db.connection.execute(
                                    "SELECT file_size FROM snapshots WHERE file_path = ?",
                                    (f"{od}/{suffix}",)).fetchone()
                                if snap and game_path.stat().st_size != snap[0]:
                                    # PAMT was modified in-place, restore vanilla
                                    txn.stage_file(f"{od}/{suffix}", vanilla_path.read_bytes())
                                    modified_pamts[od] = vanilla_path.read_bytes()
                                    logger.info("Restored vanilla %s/%s (overlay migration)",
                                                od, suffix)

                from cdumm.archive.overlay_builder import build_overlay

                _last_report = [0.0]
                def _overlay_progress(idx, total, entry_name=""):
                    # Emit progress every 0.5 seconds to keep GUI responsive
                    now = time.perf_counter()
                    if now - _last_report[0] > 0.5 or idx == total - 1:
                        _last_report[0] = now
                        pct = 60 + int((idx / max(total, 1)) * 25)
                        self.progress_updated.emit(pct, f"Building overlay ({idx + 1}/{total})...")

                # DDS reserved1 / reserved2 are rewritten inside build_overlay
                # itself now, as part of JMM-parity BuildPartialDdsPayload
                # handling for `comp_type==1` entries. The builder needs the
                # backed-up vanilla PATHC to resolve each DDS's expected last4.
                vanilla_pathc_for_build = self._vanilla_dir / "meta" / "0.pathc"
                if not vanilla_pathc_for_build.exists():
                    game_pathc_fallback = self._game_dir / "meta" / "0.pathc"
                    vanilla_pathc_for_build = game_pathc_fallback if game_pathc_fallback.exists() else None

                paz_bytes, pamt_bytes, overlay_packed = build_overlay(
                    self._overlay_entries,
                    game_dir=self._game_dir,
                    progress_cb=_overlay_progress,
                    preloaded_cache=self._cached_overlay,
                    vanilla_pathc_path=vanilla_pathc_for_build,
                )
                overlay_dir = self._allocate_overlay_dir(_staged_mod_dirs)
                self._overlay_dir_name = overlay_dir
                overlay_path = self._game_dir / overlay_dir
                overlay_path.mkdir(parents=True, exist_ok=True)
                # Stage through transactional IO so overlay files are committed
                # atomically with everything else (and not overwritten by other staged files)
                self.progress_updated.emit(86, f"Staging overlay PAZ ({len(paz_bytes) // 1048576} MB)...")
                txn.stage_file(f"{overlay_dir}/0.paz", paz_bytes)
                self.progress_updated.emit(88, "Staging overlay PAMT...")
                txn.stage_file(f"{overlay_dir}/0.pamt", pamt_bytes)
                # GitHub #141 mrkillerhomerxD: write an unambiguous
                # marker file inside the overlay dir so the next apply
                # can tell CDUMM-managed overlays apart from external-
                # tool dirs (HAWT, etc.). Stale overlay dirs from prior
                # applies carry .pamt/.paz files just like external
                # dirs, so widened orphan protection (#83) was leaving
                # them on disk and the user accumulated 004X..006X dirs
                # over time. With this marker, the cleanup loop deletes
                # the previous overlay dir while still protecting any
                # 0036+ dir an external tool wrote.
                marker_body = (
                    "CDUMM overlay marker. Do not edit by hand.\n"
                    f"overlay_dir={overlay_dir}\n"
                ).encode("utf-8")
                txn.stage_file(
                    f"{overlay_dir}/_cdumm_overlay.marker", marker_body)
                modified_pamts[overlay_dir] = pamt_bytes
                logger.info("Overlay PAZ: %s (%d entries, PAZ=%d bytes, PAMT=%d bytes)",
                            overlay_dir, len(self._overlay_entries),
                            len(paz_bytes), len(pamt_bytes))

                # Save overlay cache for incremental rebuild on next Apply
                if hasattr(build_overlay, '_last_cache') and build_overlay._last_cache:
                    from cdumm.archive.overlay_builder import _save_overlay_cache
                    from cdumm.storage.config import Config as _Config
                    _save_overlay_cache(
                        self._game_dir, overlay_dir,
                        build_overlay._last_cache,
                        config=_Config(self._db))

                # Register DDS entries in PATHC (meta/0.pathc) so the game
                # can find textures via its texture path index.
                # DMM does this for all DDS overlay entries.
                self._update_pathc_for_overlay(txn, overlay_packed)

            _phase("Phase 2: Compose PAMT files")
            # ── Phase 2: Compose PAMT files (entry updates + byte deltas) ──
            # Collect all PAMTs that need processing
            pamt_paths = set()
            for fp in file_deltas:
                if fp.endswith(".pamt"):
                    pamt_paths.add(fp)
            for pamt_dir in self._pamt_entry_updates:
                pamt_paths.add(f"{pamt_dir}/0.pamt")

            for pamt_path in sorted(pamt_paths):
                pct = int((file_idx / total_files) * 80)
                self.progress_updated.emit(pct, f"Processing {pamt_path}...")
                file_idx += 1

                pamt_dir = pamt_path.split("/")[0]
                byte_deltas = file_deltas.get(pamt_path, [])
                # Filter out entry_path deltas (shouldn't be on PAMT, but be safe)
                byte_deltas = [d for d in byte_deltas
                               if not d.get("entry_path") and not d.get("is_new")]

                new_pamt_deltas = [d for d in file_deltas.get(pamt_path, [])
                                   if d.get("is_new")]

                entry_updates = self._pamt_entry_updates.get(pamt_dir, [])

                if new_pamt_deltas and not byte_deltas and not entry_updates:
                    # Purely new PAMT — use last copy. If the language-
                    # redirect pass rewrote this PAMT's .paloc filename in
                    # memory, prefer those bytes over the raw delta file.
                    last_delta = new_pamt_deltas[-1]
                    result_bytes = last_delta.get("_rewritten_bytes")
                    if result_bytes is None:
                        src = Path(last_delta["delta_path"])
                        if src.exists():
                            result_bytes = src.read_bytes()
                    if result_bytes:
                        txn.stage_file(pamt_path, result_bytes)
                        modified_pamts[pamt_dir] = result_bytes
                    continue

                result_bytes = self._compose_pamt(
                    pamt_path, pamt_dir, byte_deltas, entry_updates)
                if result_bytes is None:
                    continue

                txn.stage_file(pamt_path, result_bytes)
                modified_pamts[pamt_dir] = result_bytes

            _phase("Phase 3: Revert disabled mods")
            # ── Phase 3: Revert files from disabled mods ───────────────
            new_files_to_delete = self._get_new_files_to_delete(set(file_deltas.keys()))
            # Hash-first short-circuit: if the live game file already
            # matches the snapshot (vanilla) hash, skip the ~100 MB read
            # + stage + rename entirely. Phase 3 dominates wallclock on
            # texture-mod toggles; most reverted files are already vanilla
            # from a prior apply/revert and only need the hash to confirm.
            from cdumm.engine.snapshot_manager import hash_matches
            phase3_skipped = 0
            for file_path in revert_files:
                pct = int((file_idx / total_files) * 80)
                self.progress_updated.emit(pct, f"Reverting {file_path}...")
                file_idx += 1
                _yield_gil()

                if file_path in new_files_to_delete:
                    # Deletion DEFERRED until after txn.commit():
                    # deleting before commit meant a failed commit
                    # rolled back to PAMTs that referenced already
                    # deleted files (audit finding C1, 2026-06-10).
                    game_path = self._game_dir / file_path.replace("/", os.sep)
                    if game_path.exists():
                        deferred_file_deletions.append(game_path)
                    continue

                game_path = self._game_dir / file_path.replace("/", os.sep)
                if game_path.exists():
                    try:
                        snap_row = self._db.connection.execute(
                            "SELECT file_hash, file_size FROM snapshots "
                            "WHERE file_path = ?", (file_path,)).fetchone()
                        if snap_row:
                            snap_hash, snap_size = snap_row
                            if (game_path.stat().st_size == snap_size
                                    and hash_matches(game_path, snap_hash)):
                                phase3_skipped += 1
                                continue
                    except OSError:
                        pass

                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes is None:
                    logger.warning("Cannot revert %s — no backup", file_path)
                    continue

                txn.stage_file(file_path, vanilla_bytes)
                if file_path.endswith(".pamt"):
                    modified_pamts[file_path.split("/")[0]] = vanilla_bytes

            if phase3_skipped:
                logger.info("Phase 3: skipped %d file(s) already matching "
                            "vanilla snapshot (no read/write needed)",
                            phase3_skipped)

            # ── Phase 3b: Safety net — restore orphaned modded files ────
            # Files can be left modded if a previous CDUMM version modified
            # them without recording a delta (e.g., PAMT PAZ size updates).
            # Scan all files with vanilla backups and restore any that differ
            # from the snapshot but aren't managed by an enabled mod.
            if not file_deltas:  # only when reverting everything
                try:
                    from cdumm.engine.snapshot_manager import hash_file, hash_matches
                    snap_cursor = self._db.connection.execute(
                        "SELECT file_path, file_hash, file_size FROM snapshots")
                    already_staged = set(txn.staged_files()) if hasattr(txn, 'staged_files') else set()
                    for rel, snap_hash, snap_size in snap_cursor.fetchall():
                        if rel == "meta/0.papgt":
                            continue  # handled in Phase 4
                        if rel in already_staged:
                            continue  # already being reverted
                        game_file = self._game_dir / rel.replace("/", os.sep)
                        if not game_file.exists():
                            continue
                        try:
                            actual_size = game_file.stat().st_size
                            needs_restore = False
                            if actual_size != snap_size:
                                needs_restore = True
                            elif actual_size < 50 * 1024 * 1024:
                                # Small file — quick hash check
                                if not hash_matches(game_file, snap_hash):
                                    needs_restore = True
                            if needs_restore:
                                vanilla = self._get_vanilla_bytes(rel)
                                if vanilla:
                                    txn.stage_file(rel, vanilla)
                                    if rel.endswith(".pamt"):
                                        modified_pamts[rel.split("/")[0]] = vanilla
                                    logger.info("Safety net: restored orphan %s", rel)
                        except OSError:
                            pass
                except Exception as e:
                    logger.debug("Safety net scan failed: %s", e)

            _phase("Phase 4: PAPGT rebuild")
            # ── Phase 4: PAPGT ─────────────────────────────────────────
            self.progress_updated.emit(90, "Updating PAPGT...")

            # Check if any mod has a PAPGT delta (overlay mods that add
            # new directories ship their own PAPGT with correct entries/ordering).
            # BUT: skip mod-shipped PAPGTs from remapped mods — their PAPGT
            # references the original dir (e.g. 0036) not the remapped one
            # (e.g. 0043), so it's stale and would break other mods.
            papgt_deltas = file_deltas.get("meta/0.papgt", [])
            mod_papgt_data = None
            if papgt_deltas:
                # Check if the mod that ships PAPGT was remapped
                # by looking at whether its other files use the directory
                # referenced in its PAPGT or a different (remapped) one.
                use_mod_papgt = True
                for d in papgt_deltas:
                    dp = d.get("delta_path")
                    if not dp:
                        continue
                    # Find which mod this PAPGT belongs to
                    mod_row = self._db.connection.execute(
                        "SELECT mod_id FROM mod_deltas WHERE delta_path = ? LIMIT 1",
                        (dp,)).fetchone()
                    if not mod_row:
                        continue
                    # Get the mod's actual file paths (non-PAPGT)
                    mod_files = self._db.connection.execute(
                        "SELECT DISTINCT file_path FROM mod_deltas "
                        "WHERE mod_id = ? AND file_path != 'meta/0.papgt'",
                        (mod_row[0],)).fetchall()
                    mod_dirs = {f[0].split("/")[0] for f in mod_files}
                    # If any mod dir is >= 0036 and NOT 0036, the mod was remapped
                    has_remapped = any(
                        d.isdigit() and len(d) == 4 and int(d) >= 36 and d != "0036"
                        for d in mod_dirs
                    )
                    if has_remapped:
                        use_mod_papgt = False
                        logger.info("Skipping mod-shipped PAPGT — mod was remapped "
                                    "(dirs: %s)", mod_dirs)
                        break

                if use_mod_papgt:
                    # Don't use mod PAPGT as the full rebuild base.
                    # Mod-shipped PAPGTs often have string table formats that
                    # our parser can't handle, causing all vanilla directories
                    # to be removed. Instead, just ensure the mod's new
                    # directories exist on disk and let rebuild discover them.
                    logger.info("Mod ships PAPGT — new directories will be "
                                "discovered from disk during rebuild")

            # Clean up orphan mod directories (0036+) not used by any enabled mod.
            # Must happen before PAPGT rebuild so orphans aren't re-added.
            enabled_dirs = set()
            for fp in file_deltas:
                d = fp.split("/")[0]
                if d.isdigit() and len(d) == 4 and int(d) >= 36:
                    enabled_dirs.add(d)
            # Also include new files from enabled mods
            for fp, deltas in file_deltas.items():
                for d in deltas:
                    if d.get("is_new"):
                        mod_dir = fp.split("/")[0]
                        if mod_dir.isdigit() and len(mod_dir) == 4:
                            enabled_dirs.add(mod_dir)
            # Include the overlay directory (just created in Phase 1b)
            if hasattr(self, '_overlay_dir_name') and self._overlay_dir_name:
                enabled_dirs.add(self._overlay_dir_name)

            # External-tool protection (GitHub #83 mrkillerhomerxD): some
            # users layer a separate patcher on top (HAWT writes to 0036+
            # with its own 0.pamt + .paz). That directory is not in the
            # CDUMM vanilla snapshot, not managed by any CDUMM mod, but
            # has a fully-valid PAMT + PAZ pair on disk. Nuking it
            # silently breaks the external patch and the user's game
            # crashes at boot. Treat any 0036+ directory with a 0.pamt
            # file as externally-managed and leave it alone. Also pick
            # them up below for PAPGT rebuild discovery.
            user_protected = set()
            try:
                from cdumm.storage.config import Config as _Cfg
                _raw = _Cfg(self._db).get("protected_external_dirs") or ""
                if _raw.strip():
                    user_protected = {
                        s.strip() for s in _raw.split(",") if s.strip()}
            except Exception as _e:
                logger.debug("protected_external_dirs lookup failed: %s", _e)

            # GitHub #83 mrkillerhomerxD reported HAWT's 0036 still being
            # deleted on v3.3.6 even after the externally-managed-dir
            # protection landed. Add INFO logging at every decision branch
            # so the next bundle from a hit reveals which path fired.
            candidate_dirs = sorted(
                d for d in self._game_dir.iterdir()
                if d.is_dir() and d.name.isdigit() and len(d.name) == 4
                and int(d.name) >= 36
            )
            if candidate_dirs:
                logger.info(
                    "Orphan-cleanup scan: %d candidate dir(s) found at "
                    ">=0036; enabled_dirs=%s, user_protected=%s",
                    len(candidate_dirs),
                    sorted(enabled_dirs), sorted(user_protected))
            for d in candidate_dirs:
                if d.name in enabled_dirs:
                    logger.info(
                        "Orphan-cleanup: %s is in enabled_dirs, skip",
                        d.name)
                    continue
                if d.name in user_protected:
                    logger.info(
                        "Skipping orphan cleanup for user-protected dir: %s",
                        d.name)
                    continue
                # GitHub #141 mrkillerhomerxD: stale CDUMM-managed
                # overlay dirs from prior applies carry .pamt/.paz
                # files just like external-tool dirs, so the widened
                # protection in #83 was leaving them on disk and the
                # user accumulated dirs over time. Look for the
                # marker file CDUMM writes inside every overlay dir
                # it produces (apply_engine line ~2257). If the
                # marker is present AND this dir is not the current
                # overlay (already excluded via enabled_dirs above),
                # it is a stale CDUMM overlay and is safe to delete.
                marker_path = d / "_cdumm_overlay.marker"
                if marker_path.exists():
                    # Deletion DEFERRED until after txn.commit(); the
                    # PAPGT rebuild below excludes these via
                    # exclude_dirs so the new index never references
                    # them, while a failed commit rolls back to the
                    # OLD PAPGT with the dirs still on disk (audit
                    # finding C1, 2026-06-10: pre-commit rmtree made
                    # rollback restore an index pointing at deleted
                    # dirs, which crashes the game at boot).
                    deferred_dir_deletions.append(d)
                    logger.info(
                        "Stale CDUMM overlay dir %s queued for "
                        "post-commit deletion (carried "
                        "_cdumm_overlay.marker, not current overlay)",
                        d.name)
                    continue
                # Externally-managed dirs carry a .pamt + .paz pair on
                # disk. Originally the check was only ``0.pamt`` which
                # missed setups where the external tool numbered its
                # files differently (1.pamt, etc.) or wrote a .paz first
                # with the .pamt landing a moment later. Widen the check
                # to "any .pamt OR .paz file in the dir" — if anything
                # PAZ-shaped sits there, some other tool owns the slot
                # and we must not delete it.
                try:
                    has_paz_artifacts = any(
                        c.suffix.lower() in (".pamt", ".paz")
                        for c in d.iterdir()
                    )
                except OSError as _e:
                    logger.warning(
                        "Orphan-cleanup: could not list %s, treating "
                        "as protected to be safe: %s", d.name, _e)
                    has_paz_artifacts = True
                if has_paz_artifacts:
                    contents = []
                    try:
                        contents = sorted(c.name for c in d.iterdir())
                    except OSError:
                        pass
                    logger.info(
                        "Skipping orphan cleanup for externally-managed "
                        "dir %s (contents: %s)", d.name, contents)
                    continue
                # Check if directory is in snapshot (vanilla)
                snap_check = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",),
                ).fetchone()[0]
                if snap_check == 0:
                    deferred_dir_deletions.append(d)
                    logger.info(
                        "Orphan directory queued for post-commit "
                        "deletion: %s (not in snapshot, no PAZ "
                        "artifacts present)", d.name)
                else:
                    logger.info(
                        "Orphan-cleanup: %s is in vanilla snapshot, keep",
                        d.name)

            papgt_mgr = PapgtManager(self._game_dir, self._vanilla_dir)
            try:
                # exclude_dirs must cover BOTH whole-dir deletions and
                # dirs that lose their 0.pamt index via deferred FILE
                # deletions (disabled mods that added a whole directory).
                # Both kinds are still on disk now (deletion deferred to
                # post-commit), so without the second set the rebuilt
                # index keeps an entry for a dir that gets removed right
                # after commit -> "Missing directory NNNN" (GitHub #225).
                exclude_dirs = {d.name for d in deferred_dir_deletions}
                exclude_dirs |= _dirs_losing_pamt(deferred_file_deletions)
                papgt_bytes = papgt_mgr.rebuild(
                    modified_pamts, exclude_dirs=exclude_dirs)
                txn.stage_file("meta/0.papgt", papgt_bytes)
            except FileNotFoundError:
                logger.warning("PAPGT not found, skipping rebuild")

            _phase("Phase 5: Committing changes")
            self.progress_updated.emit(95, "Committing changes...")
            txn.commit()

            # Destructive deletions run ONLY after the commit landed:
            # a failed commit rolls back to the old PAMTs/PAPGT, which
            # still reference these files/dirs, so they must still be
            # on disk at that point (audit finding C1, 2026-06-10).
            import shutil as _shutil
            for fp in deferred_file_deletions:
                try:
                    fp.unlink()
                    logger.info(
                        "Deleted new file from disabled mod: %s", fp)
                    parent = fp.parent
                    if parent != self._game_dir and parent.exists():
                        if not any(parent.iterdir()):
                            parent.rmdir()
                            logger.info(
                                "Removed empty mod directory: %s",
                                parent.name)
                except OSError as _e:
                    logger.warning(
                        "Post-commit file deletion failed for %s: %s",
                        fp, _e)
            for d in deferred_dir_deletions:
                _shutil.rmtree(d, ignore_errors=True)
                logger.info("Post-commit cleanup removed %s", d.name)

            # Save fingerprint so next Apply can skip if nothing changed
            try:
                from cdumm.storage.config import Config as _Config
                fp_path = (
                    get_cdmods_root(_Config(self._db), self._game_dir)
                    / ".apply_fingerprint"
                )
                fp_path.write_text(fingerprint, encoding="utf-8")
            except Exception:
                pass

            _phase("DONE — Apply complete")
            self.progress_updated.emit(100, "Apply complete!")
            self.finished.emit()

        except Exception:
            txn.cleanup_staging()
            raise
        finally:
            txn.cleanup_staging()
            # End-of-run temp reclamation: the cross-layer stage roots
            # and the aggregated-JSON dir were consumed during this
            # apply; delete them now instead of leaking one dir per run
            # into %TEMP% (audit finding, 2026-06-11).
            self._cleanup_apply_temp_dirs()

    def _cleanup_apply_temp_dirs(self) -> None:
        """Delete temp dirs created for this apply run.

        Covers the ``cdumm_xlayer_*`` stage roots referenced by
        ``self._paz_dir_overrides`` (their canonical 0.pamt/0.paz copies
        feed Phase 1a extraction and are not needed once the run ends)
        and the ``cdumm_agg_*`` aggregated-JSON dir. Also drops the
        override map so any later resolver build re-collects instead of
        pointing at deleted paths. Safe to call more than once.
        """
        from cdumm.engine.temp_workspace import release_temp_dir
        overrides = getattr(self, "_paz_dir_overrides", None) or {}
        released: set = set()
        for ov in overrides.values():
            root = ov.get("stage_root")
            if root is not None and root not in released:
                released.add(root)
                release_temp_dir(root)
        if hasattr(self, "_paz_dir_overrides"):
            try:
                del self._paz_dir_overrides
            except AttributeError:
                pass
        synth = getattr(self, "_synth_temp", None)
        if synth is not None:
            release_temp_dir(synth)
            self._synth_temp = None

    def _ensure_backups(self, file_deltas: dict, revert_files: list[str]) -> list[str]:
        """Create vanilla backups for all files about to be modified.

        Validates each backup against the snapshot hash to ensure we're
        backing up actual vanilla files, not modded ones. A dirty backup
        poisons the entire restore chain.

        Returns list of file paths that couldn't be backed up (game file
        doesn't match vanilla snapshot). Caller should abort if non-empty.
        """
        unbacked_files: list[str] = []
        self._vanilla_dir.mkdir(parents=True, exist_ok=True)

        # Always back up PAPGT — it's rebuilt on every Apply and the rebuild
        # produces different bytes from vanilla. Need the original for Revert.
        papgt_backup = self._vanilla_dir / "meta" / "0.papgt"
        if not papgt_backup.exists():
            game_papgt = self._game_dir / "meta" / "0.papgt"
            if game_papgt.exists():
                # Validate against snapshot before backing up
                snap = self._db.connection.execute(
                    "SELECT file_hash, file_size FROM snapshots WHERE file_path = ?",
                    ("meta/0.papgt",)).fetchone()
                if snap:
                    try:
                        actual_size = game_papgt.stat().st_size
                        if actual_size == snap[1]:
                            papgt_backup.parent.mkdir(parents=True, exist_ok=True)
                            _backup_copy(game_papgt, papgt_backup)
                            logger.info("Full vanilla backup: meta/0.papgt")
                    except OSError:
                        pass

        # Load snapshot hashes for validation
        snap_hashes: dict[str, tuple[str, int]] = {}
        try:
            cursor = self._db.connection.execute(
                "SELECT file_path, file_hash, file_size FROM snapshots")
            for rel, h, s in cursor.fetchall():
                snap_hashes[rel] = (h, s)
        except Exception:
            pass

        all_files = set(file_deltas.keys()) | set(revert_files)

        # ENTR deltas modify PAMTs during apply (Phase 2) without having
        # delta records. Back up PAMTs for any directory with ENTR deltas.
        # Also back up PATHC — texture mods modify it during apply.
        implicit_backups: set[str] = set()
        for file_path, deltas in file_deltas.items():
            if any(d.get("entry_path") for d in deltas) and "/" in file_path:
                pamt_path = file_path.rsplit("/", 1)[0] + "/0.pamt"
                implicit_backups.add(pamt_path)
        if "meta/0.pathc" in snap_hashes:
            implicit_backups.add("meta/0.pathc")

        for imp_path in implicit_backups:
            backup_path = self._vanilla_dir / imp_path.replace("/", os.sep)
            if not backup_path.exists():
                game_path = self._game_dir / imp_path.replace("/", os.sep)
                if game_path.exists() and self._verify_is_vanilla(
                        game_path, imp_path, snap_hashes):
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    _backup_copy(game_path, backup_path)
                    logger.info("Implicit vanilla backup: %s", imp_path)
        # A3 fix: emit per-file progress across the loop so the UI
        # advances from 2% through the backup phase (previously silent
        # until Phase 1 kicked in at 55% — users called this "stuck
        # at 2%"). Reserve 2-15 for backups, leaving 15+ for Phase 1.
        _total_backup_files = max(1, len(all_files))
        for backup_idx, file_path in enumerate(all_files):
            pct = 2 + int((backup_idx / _total_backup_files) * 13)
            self.progress_updated.emit(
                pct,
                f"Backing up vanilla files... "
                f"({backup_idx + 1}/{_total_backup_files}) {file_path}")
            delta_infos = file_deltas.get(file_path, [])

            # Skip revert-only files — they don't need new backups,
            # they need existing backups to restore FROM
            if not delta_infos:
                continue

            # Skip new files — no vanilla version to back up
            if all(d.get("is_new") for d in delta_infos) and delta_infos:
                continue

            # Skip mod directories (0036+) — these aren't vanilla files
            dir_num = file_path.split("/")[0]
            if dir_num.isdigit() and len(dir_num) == 4 and int(dir_num) >= 36:
                continue

            # PAMT files always get full backups — they're small (<14MB)
            # and range backups are unreliable when the PAMT structure changes.
            # ENTR deltas also need full backups (entry-level composition).
            has_bsdiff = self._has_bsdiff_delta(file_path)
            needs_full = has_bsdiff or file_path.endswith(".pamt")

            if needs_full:
                full_path = self._vanilla_dir / file_path.replace("/", os.sep)
                game_path = self._game_dir / file_path.replace("/", os.sep)
                if full_path.exists():
                    # Validate existing backup against snapshot
                    snap = snap_hashes.get(file_path)
                    if snap:
                        snap_hash, snap_size = snap
                        try:
                            backup_size = full_path.stat().st_size
                            is_contaminated = backup_size != snap_size
                            # Also verify hash for files where size matches
                            # (catches same-size but different-content contamination)
                            if not is_contaminated and snap_hash:
                                from cdumm.engine.snapshot_manager import hash_file
                                actual_hash, _ = hash_file(full_path)
                                if actual_hash != snap_hash:
                                    is_contaminated = True
                                    logger.debug("Backup hash mismatch for %s", file_path)

                            if is_contaminated:
                                # Backup is contaminated — try to refresh from game file
                                if game_path.exists() and self._verify_is_vanilla(
                                        game_path, file_path, snap_hashes):
                                    _backup_copy(game_path, full_path)
                                    logger.info(
                                        "Refreshed contaminated backup: %s", file_path)
                                else:
                                    # Both the backup and the live game
                                    # file diverge from the snapshot, so
                                    # the restore chain is poisoned.
                                    # Report it so the caller aborts
                                    # with the Steam-verify guidance
                                    # instead of silently proceeding.
                                    logger.warning(
                                        "Backup for %s is contaminated and game file "
                                        "is modded; cannot safely back up.",
                                        file_path)
                                    unbacked_files.append(file_path)
                        except OSError:
                            pass
                else:
                    if game_path.exists():
                        is_vanilla = self._verify_is_vanilla(game_path, file_path, snap_hashes)
                        if is_vanilla:
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            _backup_copy(game_path, full_path)
                            logger.info("Full vanilla backup: %s", file_path)
                        else:
                            # Game file is modded and no vanilla backup
                            # exists: composing from this base would bake
                            # foreign bytes into the result and revert
                            # could never restore. Report it so the
                            # caller's abort path fires with actionable
                            # advice (Steam verify + Fix Everything).
                            # This list was previously never appended to,
                            # which left the caller's abort path dead and
                            # silently proceeded without any backup.
                            logger.warning(
                                "No vanilla backup for %s and game file is modded; "
                                "cannot safely back up.",
                                file_path)
                            unbacked_files.append(file_path)
            else:
                # Byte-range backup — only the positions mods touch
                ranges = self._get_all_byte_ranges(file_path)
                if ranges:
                    _save_range_backup(
                        self._game_dir, self._vanilla_dir, file_path, ranges)

        return unbacked_files

    def _verify_is_vanilla(self, game_path: Path, file_path: str,
                           snap_hashes: dict[str, tuple[str, int]]) -> bool:
        """Check if a game file matches its snapshot hash (is truly vanilla)."""
        snap = snap_hashes.get(file_path)
        if snap is None:
            return False  # not in snapshot = not a vanilla file

        snap_hash, snap_size = snap
        # Quick size check first
        try:
            if game_path.stat().st_size != snap_size:
                return False
        except OSError:
            return False

        # Full hash check for small files (<50MB). For large files, trust
        # the size match — hashing 900MB PAZ on every apply is too slow.
        if snap_size < 50 * 1024 * 1024:
            from cdumm.engine.snapshot_manager import hash_file
            try:
                current_hash, _ = hash_file(game_path)
                return current_hash == snap_hash
            except Exception:
                return False

        return True  # large file, size matches

    def _has_bsdiff_delta(self, file_path: str) -> bool:
        """Check if any mod delta for this file is bsdiff format."""
        cursor = self._db.connection.execute(
            "SELECT md.delta_path FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE md.file_path = ? AND m.mod_type = 'paz'",
            (file_path,),
        )
        for (delta_path,) in cursor.fetchall():
            try:
                with open(delta_path, "rb") as f:
                    magic = f.read(4)
                if magic != SPARSE_MAGIC:
                    return True
            except OSError:
                continue
        return False

    def _get_all_byte_ranges(self, file_path: str) -> list[tuple[int, int]]:
        """Get union of all mod byte ranges for a file."""
        cursor = self._db.connection.execute(
            "SELECT byte_start, byte_end FROM mod_deltas "
            "WHERE file_path = ? AND byte_start IS NOT NULL",
            (file_path,),
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]

    def _compose_file(self, file_path: str, deltas: list[dict]) -> bytes | None:
        """Compose a file by starting from vanilla and applying deltas.

        Handles three delta types:
        - ENTR (entry-level): decompressed PAMT entry content, repacked per-entry
        - FULL_COPY/bsdiff: replace entire file
        - SPRS: sparse byte-level patches

        JSON patch merging: when multiple mods have json_patches data for the
        same decompressed game file, their patches are merged at the decompressed
        level (three-way merge against vanilla) instead of last-wins at PAZ level.

        ENTR deltas are applied first (different entries compose perfectly),
        then byte-level deltas on top for backward compatibility.
        """
        from cdumm.engine.delta_engine import ENTRY_MAGIC, load_entry_delta

        # Check for JSON patch merge opportunities BEFORE byte-level composition.
        # If multiple mods have json_patches for the same game file, merge them
        # into a single ENTR-style delta, then skip their byte deltas.
        merged_deltas, remaining_deltas = self._merge_json_patch_deltas(
            file_path, deltas)

        # Separate entry-level and byte-level deltas from remaining
        entry_deltas = [d for d in remaining_deltas if d.get("entry_path")]
        # Include merged deltas as entry deltas
        entry_deltas.extend(merged_deltas)
        byte_deltas = [d for d in remaining_deltas if not d.get("entry_path")]

        # ── Semantic merge: when multiple mods touch the same PABGB entry ──
        # Try field-level merge instead of last-wins. Falls back silently.
        if len(entry_deltas) > 1:
            entry_deltas = self._try_semantic_merge(file_path, entry_deltas)

        # ── Overlay routing: ENTR-only files go to overlay PAZ ──
        # If a file has ONLY entry deltas (no byte-range), route all entries
        # to the overlay builder. The original PAZ is left untouched.
        # Exception: force_inplace mods bypass overlay (for game dirs that
        # merge base+overlay entries instead of overlay-wins).
        any_force_inplace = any(d.get("force_inplace") for d in remaining_deltas)
        if entry_deltas and not byte_deltas and not any_force_inplace:
            from cdumm.engine.delta_engine import load_entry_delta
            by_entry: dict[str, dict] = {}
            for d in entry_deltas:
                ep = d.get("entry_path") or d.get("_merged_metadata", {}).get("entry_path")
                if ep:
                    by_entry[ep] = d
            for entry_path, d in by_entry.items():
                if d.get("_merged_content") is not None:
                    content = d["_merged_content"]
                    metadata = d["_merged_metadata"]
                elif d.get("delta_path"):
                    try:
                        content, metadata = load_entry_delta(Path(d["delta_path"]))
                        metadata["delta_path"] = d["delta_path"]
                    except Exception as e:
                        # Bug D: surface the failure to the user
                        # instead of silent continue.
                        self._warn_entr_load_failure(d, e)
                        continue
                else:
                    continue
                # Propagate mod_name from the delta dict so the size-merge
                # fallback warning at _merge_same_target_overlay_entries can
                # name the actual mod that got dropped, not "mod #0".
                # DerBambusbjoern report 2026-04-30: dropsetinfo conflict
                # warning showed "Dropped: 'mod #0'" because metadata lost
                # the name during routing.
                if "mod_name" not in metadata:
                    name_from_d = d.get("mod_name")
                    if name_from_d:
                        metadata["mod_name"] = name_from_d
                if "priority" not in metadata and "priority" in d:
                    metadata["priority"] = d["priority"]
                self._overlay_entries.append((content, metadata))
                logger.info("Routed to overlay: %s in %s from %s",
                            entry_path, file_path, d.get("mod_name", "?"))
            return None  # Don't modify the original PAZ

        # Get vanilla content
        full_vanilla = self._vanilla_dir / file_path.replace("/", os.sep)
        if full_vanilla.exists():
            current = full_vanilla.read_bytes()
        else:
            game_path = self._game_dir / file_path.replace("/", os.sep)
            if not game_path.exists():
                logger.warning("Game file not found: %s", file_path)
                return None

            current_buf = bytearray(game_path.read_bytes())
            range_entries = _load_range_backup(self._vanilla_dir, file_path)
            if range_entries:
                _apply_ranges_to_buf(current_buf, range_entries)
            current = bytes(current_buf)

        vanilla_size = len(current)

        # ── Entry-level deltas (script mods) ───────────────────────
        if entry_deltas:
            current = self._apply_entry_deltas(
                file_path, bytearray(current), entry_deltas)

        # ── Byte-level deltas (zip/JSON/legacy mods) ───────────────
        if not byte_deltas:
            return current

        # Classify byte deltas by type
        full_replace = []
        sprs_shifted = []
        size_preserving = []

        for d in byte_deltas:
            dp = Path(d["delta_path"])
            try:
                with open(dp, "rb") as f:
                    magic = f.read(4)
            except OSError:
                continue

            if magic == b"FULL" or (magic == b"BSDI"):
                full_replace.append(d)
            elif _delta_changes_size(dp, vanilla_size):
                sprs_shifted.append(d)
            else:
                size_preserving.append(d)

        # Step 1: Apply ONE full-replace delta (the priority winner).
        # When multiple mods ship a full-replace bsdiff for the same
        # .paz, each bsdiff was computed against vanilla. Applying
        # them sequentially feeds the second bsdiff a non-vanilla
        # input (the first mod's output), which produces a corrupted
        # paz: the bsdiff control stream issues copies/inserts that
        # don't reconcile with the actual base bytes, the result is
        # truncated or scrambled, and PAMT entries point past the
        # end of the file → game crashes on launch.
        # Bug confirmed 2026-05-08 against Graphics Mod (Nexus 651)
        # + Vaxis Water Physics Overhaul Mod (Nexus 2376) both
        # full-replacing 0003/0.paz: the staged paz was 756563 bytes
        # but PAMT expected 816735, the trailing renderpass entries
        # all failed LZ4 decompression with "expected another byte,
        # found none" or "offset to copy not contained in
        # decompressed buffer".
        # Sort by priority so the highest-priority mod wins
        # deterministically. Lower priority value wins in CDUMM.
        if full_replace:
            full_replace_sorted = sorted(
                full_replace,
                key=lambda d: d.get("priority", 0))
            winner = full_replace_sorted[0]
            try:
                current = apply_delta_from_file(
                    current, Path(winner["delta_path"]))
                logger.info(
                    "Applied full-replace delta for %s from %s",
                    file_path, winner.get("mod_name", "?"))
            except ValueError as e:
                # Truncated "BSDI" header that is not a full BSDIFF40
                # magic: skip the winner instead of replacing the file
                # with the raw delta bytes.
                self._warn_corrupt_delta(winner, e)
            if len(full_replace_sorted) > 1:
                skipped = [
                    d.get("mod_name", "?")
                    for d in full_replace_sorted[1:]
                ]
                # Plain-English banner. Replaces the previous
                # dev-style version that confused users (Faisal
                # screenshot 2026-05-09). Matches the overlay-merge
                # branch's voice for consistency.
                winner_name = winner.get("mod_name", "?")
                shown = skipped[:5]
                more = len(skipped) - len(shown)
                names_block = ", ".join(f"'{n}'" for n in shown)
                if more > 0:
                    names_block += f" and {more} more"
                if len(skipped) == 1:
                    msg = (
                        f"'{shown[0]}' could not be applied."
                        f" '{winner_name}' is changing the same game"
                        f" data and CDUMM can only keep one of them."
                        f" To use '{shown[0]}' instead,"
                        f" move it higher in the mod list"
                        f" than '{winner_name}'."
                        f" (File: {file_path})"
                    )
                else:
                    msg = (
                        f"Some mods could not be applied:"
                        f" {names_block}. '{winner_name}' is changing"
                        f" the same game data and CDUMM can only keep"
                        f" one. To activate one of these,"
                        f" move it higher in the mod list"
                        f" than '{winner_name}'."
                        f" (File: {file_path})"
                    )
                logger.warning(msg)
                if hasattr(self, "_soft_warnings"):
                    self._soft_warnings.append(msg)
                try:
                    self.warning.emit(msg)
                except Exception:
                    pass

        # Step 2: Apply SPRS deltas that shift file size
        for d in sprs_shifted:
            try:
                current = apply_delta_from_file(
                    current, Path(d["delta_path"]))
            except ValueError as e:
                # Corrupt/unrecognized delta: skip this one mod's delta
                # instead of replacing the game file with garbage or
                # aborting the whole apply.
                self._warn_corrupt_delta(d, e)

        if not size_preserving:
            return current

        # Step 3: Apply same-size SPRS patches on top
        shift = len(current) - vanilla_size
        if shift != 0 and (full_replace or sprs_shifted):
            if sprs_shifted:
                insertion_point = _find_insertion_point(
                    Path(sprs_shifted[0]["delta_path"]))
            else:
                insertion_point = vanilla_size

            if insertion_point < vanilla_size:
                logger.info(
                    "PAZ shift detected: %+d bytes at offset %d, "
                    "adjusting %d remaining delta(s)",
                    shift, insertion_point, len(size_preserving))
                result = bytearray(current)
                for d in size_preserving:
                    _apply_sparse_shifted(
                        result, Path(d["delta_path"]), insertion_point, shift)
                return bytes(result)

        for d in size_preserving:
            try:
                current = apply_delta_from_file(
                    current, Path(d["delta_path"]))
            except ValueError as e:
                self._warn_corrupt_delta(d, e)
        return current

    def _warn_corrupt_delta(self, d: dict, exc: Exception) -> None:
        """Surface a per-delta corruption skip (unknown delta magic).

        apply_delta no longer falls back to raw replacement for
        unrecognized magics (a truncated delta would silently replace a
        game file with garbage); callers downgrade the raised ValueError
        to a per-file skip through here.
        """
        mod_name = d.get("mod_name") or "(unknown mod)"
        msg = (
            f"Mod '{mod_name}' has a corrupt stored delta and was "
            f"skipped for this file. Re-import the mod to regenerate "
            f"it. Affected file: {d.get('delta_path', '?')}. "
            f"Error: {exc}"
        )
        logger.warning(msg)
        if hasattr(self, "_soft_warnings"):
            self._soft_warnings.append(msg)
        try:
            self.warning.emit(msg)
        except Exception:
            pass

    def _try_semantic_merge(self, file_path: str,
                           entry_deltas: list[dict]) -> list[dict]:
        """Attempt semantic field-level merge for overlapping ENTR deltas.

        When multiple mods modify the same PABGB entry, semantic merge
        combines their changes at the field level instead of last-wins.
        Falls back to the original deltas if semantic merge is unavailable.
        """
        from cdumm.engine.delta_engine import load_entry_delta

        # Group deltas by entry_path
        by_entry: dict[str, list[dict]] = {}
        for d in entry_deltas:
            ep = d.get("entry_path", "")
            if ep:
                by_entry.setdefault(ep, []).append(d)

        # Only attempt merge on entries with 2+ mods
        entries_to_merge = {ep: ds for ep, ds in by_entry.items() if len(ds) > 1}
        if not entries_to_merge:
            return entry_deltas

        try:
            from cdumm.semantic.parser import identify_table_from_path
        except ImportError:
            return entry_deltas

        merged_deltas = list(entry_deltas)  # start with original list

        # Tell the user what's happening BEFORE the merge loop runs.
        # This phase can take 10-30+ seconds with many overlapping mods
        # and the percentage doesn't move during it (we're still on the
        # same file). Without this emit, progress looks frozen.
        total_entries = len(entries_to_merge)
        total_mods = sum(len(d) for d in entries_to_merge.values())
        try:
            self.progress_updated.emit(
                self._last_pct_emitted if hasattr(self, "_last_pct_emitted") else 30,
                f"Merging {total_mods} mods into {total_entries} entries "
                f"in {file_path} (this can take a while with many mods)...")
        except Exception:
            pass

        for idx, (entry_path, conflicting) in enumerate(entries_to_merge.items()):
            # Skip byte-merge entirely for self-contained binary blobs
            # (textures, audio, images, unknown formats). Merging a
            # DDS texture from two mods produces a corrupt Frankenstein
            # file the GPU chokes on. Last-wins is the right fallback
            # for these formats. Nexus regression mrkillerhomer
            # 2026-05-03 (texture mods 920/2233/2126 broke on v3.2.8).
            if not _entry_supports_byte_merge(entry_path):
                logger.debug(
                    "skipping merge for %s (extension not in byte-"
                    "merge whitelist; falling through to last-wins)",
                    entry_path)
                continue
            # Populate mod_bodies BEFORE the table_name check so the byte-
            # merge tier 2 fallback below works for entries that aren't
            # known pabgb tables. GitHub #59 (DoRoon, 2026-05-01): two
            # mods on the same non-pabgb entry (sequencer .paseq, NPC
            # interaction definition, .paac, etc.) used to hit
            # `if not table_name: continue` and skip the byte-merge
            # entirely, last-wins silently dropped one mod's changes.
            mod_bodies: dict[str, bytes] = {}
            for d in conflicting:
                dp = d.get("delta_path")
                if not dp:
                    continue
                try:
                    content, _meta = load_entry_delta(Path(dp))
                    mod_bodies[d.get("mod_name", "unknown")] = content
                except Exception as e:
                    # R2: same Bug D class — surface to user instead of
                    # logger-only silence so corrupt entry deltas don't
                    # silently skip mods during merge.
                    self._warn_entr_load_failure(d, e)

            if len(mod_bodies) < 2:
                continue

            pamt_dir = file_path.split("/")[0]
            table_name = identify_table_from_path(entry_path)

            # Tier 1: schema-aware semantic merge, only for known pabgb
            # tables. Falls through to byte-merge tier 2 (always-on)
            # below if it can't run or doesn't produce a merged body.
            if table_name:
                try:
                    from cdumm.semantic.engine import SemanticEngine
                    engine = SemanticEngine(self._db)

                    header_entry_path = entry_path.replace(".pabgb", ".pabgh")
                    header_bytes = self._extract_sibling_entry(
                        pamt_dir, header_entry_path)
                    vanilla_content = self._get_vanilla_entry_content(
                        file_path, entry_path)

                    if header_bytes and vanilla_content:
                        # Heartbeat so the progress bar text changes for huge
                        # tables. analyze_bytes can take many seconds on
                        # 6000+ record tables like iteminfo with multiple
                        # conflicting mods. Without this emit, the bar
                        # message stays stale on the same file/percentage.
                        try:
                            self.progress_updated.emit(
                                self._last_pct_emitted if hasattr(self, "_last_pct_emitted") else 30,
                                f"Merging entry {idx + 1}/{total_entries} in "
                                f"{file_path}: {entry_path} ({len(mod_bodies)} mods)...")
                        except Exception:
                            pass

                        result = engine.analyze_bytes(
                            table_name, vanilla_content, header_bytes,
                            mod_bodies)
                        if result and not result.has_conflicts:
                            merged_body = engine.build_merged_body(
                                table_name, vanilla_content, header_bytes,
                                result.table_changeset)
                            if merged_body and merged_body != vanilla_content:
                                merged_meta = {
                                    "pamt_dir": pamt_dir,
                                    "entry_path": entry_path,
                                    "_semantic_merged": True,
                                }
                                first_d = conflicting[0]
                                _, first_meta = load_entry_delta(
                                    Path(first_d["delta_path"]))
                                merged_meta.update({
                                    k: first_meta[k] for k in (
                                        "paz_index", "compression_type", "flags",
                                        "vanilla_offset", "vanilla_comp_size",
                                        "vanilla_orig_size", "encrypted")
                                    if k in first_meta
                                })
                                merged_d = dict(first_d)
                                merged_d["_merged_content"] = merged_body
                                merged_d["_merged_metadata"] = merged_meta
                                merged_d["mod_name"] = _compose_merged_mod_name(
                                    list(mod_bodies.keys()), "semantic merge")
                                ep_set = {entry_path}
                                merged_deltas = [
                                    d for d in merged_deltas
                                    if d.get("entry_path") not in ep_set
                                ]
                                merged_deltas.append(merged_d)
                                logger.info(
                                    "Semantic merge SUCCESS: %s, %s",
                                    entry_path, result.summary)
                                continue

                        if result and result.has_conflicts:
                            logger.info(
                                "Semantic merge: %d conflict(s) in %s, "
                                "trying byte-merge fallback",
                                len(result.conflicts), entry_path)
                    else:
                        logger.debug(
                            "Semantic merge skipped for %s: missing %s",
                            entry_path,
                            "PABGH header" if not header_bytes
                            else "vanilla body")
                except Exception as e:
                    logger.debug(
                        "Semantic merge failed for %s: %s", entry_path, e)

            # JMM-parity byte-level fallback (MergeCompiledModFiles). Fires
            # when the schema-aware semantic merge couldn't cleanly combine
            # the conflicting entries. We have vanilla + each mod's body in
            # memory already — walk each mod's bytes against vanilla, copy
            # differing runs into a shared buffer, log overlaps.
            try:
                vanilla_for_bytes = self._get_vanilla_entry_content(
                    file_path, entry_path)
                if vanilla_for_bytes and mod_bodies and len(mod_bodies) >= 2:
                    from cdumm.engine.compiled_merge import merge_compiled_mod_files
                    ordered = list(mod_bodies.items())  # priority order preserved
                    # merge_compiled_mod_files always emits a buffer
                    # the same length as vanilla and silently drops
                    # bytes past that length. If any mod grew or
                    # shrank the body (inserts of new XML elements,
                    # added attributes, etc.), three-way byte-merge
                    # would truncate the inserts mid-token, producing
                    # malformed XML/CSS/HTML that the engine accepts
                    # at parse but renders unusable. Fall back to the
                    # priority-winning mod's full body when ANY
                    # contributor changes the file size, mirroring the
                    # guard in _merge_same_target_overlay_entries.
                    # Bug confirmed 2026-05-08 against Faster
                    # Interactions All RAW (Nexus 146): the mod adds
                    # 789 bytes to ui/inputmap_common.xml; when stacked
                    # with Better Radial Menus + No Intro the byte-
                    # merge truncated those bytes, the resulting
                    # inputmap loaded but no input action dispatched,
                    # the user could not even ALT+F4 out of the game.
                    size_changed = [
                        (n, len(body)) for n, body in ordered
                        if len(body) != len(vanilla_for_bytes)
                    ]
                    if size_changed:
                        winner_name, winner_body = ordered[-1]
                        # Plain-English banner. Replaces the previous
                        # dev-style version that confused users
                        # (Faisal screenshot 2026-05-09). Matches the
                        # overlay-merge branch's voice for consistency.
                        dropped_names = [n for n, _ in size_changed
                                         if n != winner_name]
                        shown = dropped_names[:5]
                        more = len(dropped_names) - len(shown)
                        names_block = ", ".join(
                            f"'{n}'" for n in shown)
                        if more > 0:
                            names_block += f" and {more} more"
                        if len(dropped_names) == 1:
                            msg = (
                                f"'{shown[0]}' could not be applied."
                                f" It changes the same game data as"
                                f" '{winner_name}' but in a way that"
                                f" cannot be combined with the others."
                                f" To use '{shown[0]}' instead,"
                                f" move it higher in the mod list"
                                f" than '{winner_name}'."
                                f" (File: {entry_path})"
                            )
                        else:
                            msg = (
                                f"Some mods could not be applied:"
                                f" {names_block}. They change the same"
                                f" game data as '{winner_name}' in a"
                                f" way that cannot be combined with the"
                                f" others. To activate one,"
                                f" move it higher in the mod list"
                                f" than '{winner_name}'."
                                f" (File: {entry_path})"
                            )
                        logger.warning(msg)
                        if hasattr(self, "_soft_warnings"):
                            self._soft_warnings.append(msg)
                        try:
                            self.warning.emit(msg)
                        except Exception:
                            pass
                        merged_body, warnings = winner_body, []
                    else:
                        merged_body, warnings = merge_compiled_mod_files(
                            vanilla_for_bytes, ordered)
                    if warnings:
                        logger.info(
                            "byte-merge: %d byte-range overlap(s)",
                            len(warnings))
                        for w in warnings:
                            logger.debug("byte-merge: %s", w)
                    if merged_body and merged_body != vanilla_for_bytes:
                        from cdumm.engine.delta_engine import load_entry_delta
                        pamt_dir = file_path.split("/")[0]
                        merged_meta = {
                            "pamt_dir": pamt_dir,
                            "entry_path": entry_path,
                            "_byte_merged": True,
                        }
                        first_d = conflicting[0]
                        _, first_meta = load_entry_delta(Path(first_d["delta_path"]))
                        merged_meta.update({
                            k: first_meta[k] for k in (
                                "paz_index", "compression_type", "flags",
                                "vanilla_offset", "vanilla_comp_size",
                                "vanilla_orig_size", "encrypted")
                            if k in first_meta
                        })
                        merged_d = dict(first_d)
                        merged_d["_merged_content"] = merged_body
                        merged_d["_merged_metadata"] = merged_meta
                        merged_d["mod_name"] = _compose_merged_mod_name(
                            list(mod_bodies.keys()), "byte merge")
                        ep_set = {entry_path}
                        merged_deltas = [
                            d for d in merged_deltas
                            if d.get("entry_path") not in ep_set
                        ]
                        merged_deltas.append(merged_d)
                        logger.info(
                            "Byte-merge fallback SUCCESS: %s — %d mod(s) merged, "
                            "%d overlap warning(s)",
                            entry_path, len(ordered), len(warnings))
            except Exception as e:
                logger.debug("Byte-merge fallback failed for %s: %s",
                             entry_path, e)

        return merged_deltas

    def _extract_sibling_entry(self, pamt_dir: str, entry_path: str) -> bytes | None:
        """Extract a sibling entry (e.g., .pabgh) from the same PAZ
        directory.

        Like ``_get_vanilla_entry_content``, accepts either the
        exact PAMT path or a basename. Format 3 callers compute the
        sibling header path from the user's basename target
        ("iteminfo.pabgb" -> "iteminfo.pabgh"); without basename
        fallback, the PAMT entry stored as
        "gamedata/iteminfo.pabgh" wouldn't match.
        """
        from cdumm.archive.paz_parse import parse_pamt
        from cdumm.engine.json_patch_handler import _extract_from_paz

        if not hasattr(self, "_pamt_entries_cache"):
            self._pamt_entries_cache: dict[str, list] = {}

        entry_basename = entry_path.rsplit("/", 1)[-1]
        for base in [self._vanilla_dir, self._game_dir]:
            pamt_path = base / pamt_dir / "0.pamt"
            if not pamt_path.exists():
                continue
            cache_key = str(pamt_path)
            entries = self._pamt_entries_cache.get(cache_key)
            if entries is None:
                try:
                    entries = parse_pamt(
                        str(pamt_path), paz_dir=str(base / pamt_dir))
                except Exception as e:
                    logger.warning(
                        "Vanilla PAMT parse failed for %s: %s", pamt_path, e)
                    continue
                # R2: share cache with _get_vanilla_entry_content so
                # Format 3 mods that touch many .pabgb entries don't
                # re-parse the PAMT once per sibling-header lookup
                # (#61 fix only covered the other call site).
                self._pamt_entries_cache[cache_key] = entries
            try:
                for e in entries:
                    if e.path == entry_path:
                        return _extract_from_paz(e)
                for e in entries:
                    if e.path.rsplit("/", 1)[-1] == entry_basename:
                        return _extract_from_paz(e)
            except Exception as e:
                # R2: same #62 visibility fix — log the real cause
                # instead of silently dropping it.
                logger.warning(
                    "Sibling extraction failed for %s in %s: %s",
                    entry_path, pamt_dir, e)
        return None

    def _merge_same_target_overlay_entries(
        self, entries: list[tuple[bytes, dict]],
    ) -> list[tuple[bytes, dict]]:
        """Collapse overlay entries that hit the same (pamt_dir, entry_path).

        Multiple JSON mods can each produce an overlay entry for the same
        PABGB (e.g. ALOO PC's Mega Stacks + Abyss Gear Stacking both patch
        gamedata/iteminfo.pabgb). Without merging, the overlay PAMT ends up
        with two entries at the same path and the game resolves only one,
        silently discarding the other mod's changes. This runs a three-way
        byte merge against vanilla — non-overlapping byte ranges from all
        mods are kept; overlaps go to the later (higher-priority) entry.
        """
        if not entries or len(entries) < 2:
            return entries
        from collections import OrderedDict
        grouped: "OrderedDict[tuple[str, str], list[int]]" = OrderedDict()
        for i, (_, meta) in enumerate(entries):
            key = (meta.get("pamt_dir", ""), meta.get("entry_path", ""))
            grouped.setdefault(key, []).append(i)
        if not any(len(v) >= 2 for v in grouped.values()):
            return entries

        from cdumm.engine.compiled_merge import merge_compiled_mod_files
        # Byte-merge is safe ONLY on structured data tables where each
        # non-overlapping delta region means "one record field changed".
        # Opaque assets (.dds textures, .bnk audio, compiled shaders) and
        # text formats (XML, JSON, CSS) would get spliced into corrupt
        # output because either (a) both mods rewrite overlapping header
        # / offset tables or (b) byte-level diffs on serialised text can
        # land inside tokens. XML is further handled upstream by
        # xml_patch_handler.process_xml_patches_for_overlay, which does
        # structural merging; byte-merging its output would undo that.
        # Stick to last-wins (priority-ordered) for everything else.
        _MERGEABLE_EXTS = (".pabgb", ".pabgh", ".pamt")
        # CDUMM priority: lower number wins. Entries now carry a
        # 'priority' key in their meta (stamped at the JSON / XML
        # extend sites). Resolve ties by meta priority instead of
        # feed order — mixed JSON+XML collisions used to pick the
        # LAST extend, which always meant XML regardless of the
        # user's priorities. C-H6.
        def _winner_idx(idxs: list[int]) -> int:
            # Lowest priority number wins; fall back to LAST index
            # (old behaviour) for entries without priority meta.
            return min(idxs, key=lambda i: (
                entries[i][1].get("priority", 10_000),
                -i,   # prefer later feed position on priority tie
            ))

        result: list[tuple[bytes, dict]] = []
        for (pamt_dir, entry_path), indices in grouped.items():
            if len(indices) < 2:
                result.append(entries[indices[0]])
                continue
            ep_lower = entry_path.lower()
            if not ep_lower.endswith(_MERGEABLE_EXTS):
                winner = _winner_idx(indices)
                logger.info(
                    "Overlay merge: %s is not a mergeable table type "
                    "(%d entries) — keeping priority-winning entry "
                    "(priority=%s)",
                    entry_path, len(indices),
                    entries[winner][1].get("priority", "?"))
                result.append(entries[winner])
                continue
            # Need a vanilla base for three-way byte merge. The
            # _get_vanilla_entry_content helper expects file_path with a
            # pamt_dir/ prefix; constructing it from pamt_dir + the PAMT
            # filename gets us into the lookup.
            vanilla = self._get_vanilla_entry_content(
                f"{pamt_dir}/0.pamt", entry_path)
            if not vanilla:
                winner = _winner_idx(indices)
                logger.warning(
                    "Overlay merge: no vanilla for %s, falling back to "
                    "priority-winner (entry %d)",
                    entry_path, winner)
                result.append(entries[winner])
                continue
            ordered_bodies = [
                (f"overlay_{i}", entries[i][0]) for i in indices
            ]
            # merge_compiled_mod_files always emits a buffer the same
            # length as vanilla and silently drops bytes past that
            # length. If any mod grew or shrank the decomp body (inserts
            # for table extensions, etc.), three-way byte-merge would
            # truncate the inserts. Fall back to last-wins (highest
            # priority) instead so at least one mod's growth survives.
            size_changed = [
                len(body) for _n, body in ordered_bodies
                if len(body) != len(vanilla)
            ]
            if size_changed:
                logger.warning(
                    "Overlay merge: %s has size-changing entries "
                    "(vanilla=%d, mod lens=%s) — using priority-winning "
                    "entry instead of lossy byte-merge",
                    entry_path, len(vanilla), size_changed)
                winner = _winner_idx(indices)
                kept_mod_meta = entries[winner][1]
                kept_name = kept_mod_meta.get(
                    "mod_name", "highest-priority mod")
                # Collect the names of every dropped mod so the user
                # can actually act on the message — earlier versions
                # only said "1 mod(s) were dropped" which left users
                # hunting through the conflict viewer to find which
                # mod silently lost (GioGr on Nexus reported the
                # conflict viewer didn't even show his case).
                dropped_names: list[str] = []
                for i in indices:
                    if i == winner:
                        continue
                    name = entries[i][1].get("mod_name") if entries[i][1] else None
                    dropped_names.append(name or f"mod #{i}")
                # Cap the inline list at 5 to keep banners readable
                # on huge conflict sets; the activity log captures all.
                shown = dropped_names[:5]
                more = len(dropped_names) - len(shown)
                names_block = ", ".join(f"'{n}'" for n in shown)
                if more > 0:
                    names_block += f" and {more} more"
                # Plain-English banner: lead with which mod is
                # currently inactive and the action the user can take.
                # Drop technical phrasing ("file size", "inserts",
                # "byte-level merge", internal "aggregated JSON" label)
                # in favor of one sentence + one action + an affected-
                # file footnote for users who want to dig in.
                if len(dropped_names) == 1:
                    msg = (
                        f"'{shown[0]}' could not be applied. Another "
                        f"enabled mod is changing the same game data, "
                        f"and the two mods cannot be combined. To use "
                        f"'{shown[0]}' instead, move it higher in the "
                        f"mod list than the other mod. "
                        f"(File: {entry_path})"
                    )
                else:
                    msg = (
                        f"Some mods could not be applied: "
                        f"{names_block}. Other enabled mods are "
                        f"changing the same game data, and these mods "
                        f"cannot be combined with them. To activate "
                        f"one of these, move it higher in the mod "
                        f"list than the other mods. "
                        f"(File: {entry_path})"
                    )
                if hasattr(self, "_soft_warnings"):
                    self._soft_warnings.append(msg)
                try:
                    self.warning.emit(msg)
                except Exception:
                    pass
                result.append(entries[winner])
                continue
            try:
                merged_body, warnings = merge_compiled_mod_files(
                    vanilla, ordered_bodies)
            except Exception as e:
                logger.warning(
                    "Overlay merge for %s failed (%s) — last-wins fallback",
                    entry_path, e)
                result.append(entries[indices[-1]])
                continue
            if warnings:
                logger.info(
                    "Overlay byte-merge: %d byte-range overlap(s) "
                    "collapsed (last-mod-wins in each)",
                    len(warnings))
                for w in warnings:
                    logger.debug("Overlay byte-merge: %s", w)
            if merged_body and merged_body != vanilla:
                first_meta = dict(entries[indices[0]][1])
                first_meta["_merged_from"] = len(indices)
                result.append((merged_body, first_meta))
                logger.info(
                    "Overlay merge: collapsed %d entries for %s",
                    len(indices), entry_path)
            else:
                result.append(entries[indices[-1]])
        return result

    def _get_vanilla_entry_content(self, file_path: str, entry_path: str) -> bytes | None:
        """Get vanilla decompressed content for a specific PAMT entry.

        Accepts ``entry_path`` as either the exact PAMT path
        (e.g. "gamedata/iteminfo.pabgb") OR a basename
        ("iteminfo.pabgb"). Format 3 mods target by basename, so
        exact-match-only would silently fail to extract vanilla
        bytes for them.

        GitHub #61 (Loe-Aner, 2026-05-02): the overlay dedup phase
        calls this once per unique entry group. P3rdpc Mod V 3.5
        with 97k deltas hit it 500+ times for the same pamt_dir,
        and each call re-parsed the PAMT from disk (~2ms each), so
        Apply stalled past the 180s watchdog. Cache the parsed
        entries per-PAMT-path within an apply run.
        """
        from cdumm.archive.paz_parse import parse_pamt
        from cdumm.engine.json_patch_handler import _extract_from_paz

        pamt_dir = file_path.split("/")[0]
        entry_basename = entry_path.rsplit("/", 1)[-1]

        if not hasattr(self, "_pamt_entries_cache"):
            self._pamt_entries_cache: dict[str, list] = {}

        for base in [self._vanilla_dir, self._game_dir]:
            pamt_path = base / pamt_dir / "0.pamt"
            if not pamt_path.exists():
                continue
            cache_key = str(pamt_path)
            entries = self._pamt_entries_cache.get(cache_key)
            if entries is None:
                try:
                    entries = parse_pamt(
                        str(pamt_path), paz_dir=str(base / pamt_dir))
                except Exception as e:
                    logger.warning(
                        "Vanilla PAMT parse failed for %s: %s", pamt_path, e)
                    continue
                self._pamt_entries_cache[cache_key] = entries
            try:
                # Prefer exact path match.
                for e in entries:
                    if e.path == entry_path:
                        return _extract_from_paz(e)
                # Fall back to basename match — mirrors
                # _find_pamt_entry's behavior (json_patch_handler.py
                # :1462) so callers passing a Format-3 basename
                # target resolve correctly.
                for e in entries:
                    if e.path.rsplit("/", 1)[-1] == entry_basename:
                        return _extract_from_paz(e)
            except Exception as e:
                # GitHub #62 (UnLuckyLust, 2026-05-02): the prior
                # bare except Exception:pass swallowed extraction
                # errors silently and surfaced as a misleading
                # "file may not exist" warning. Log the real cause.
                logger.warning(
                    "Vanilla extraction failed for %s in %s: %s",
                    entry_path, file_path, e)
        return None

    def _merge_json_patch_deltas(
        self, file_path: str, deltas: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Merge multiple mods that modify the same decompressed game file.

        Two paths:
        1. Fast path (v1.5.0+ imports): json_patches data is stored — apply
           all patches from all mods to vanilla content directly.
        2. Fallback (pre-v1.5.0 imports): no json_patches data — apply each
           mod's byte delta to vanilla independently, diff each result against
           vanilla to derive per-mod patches, then three-way merge.

        Both paths produce a merged decompressed content that contains all
        non-overlapping changes. Overlapping bytes go to the higher-priority mod.

        Returns (merged_entry_deltas, remaining_deltas).
        """
        import json
        from cdumm.archive.paz_parse import PazEntry
        from cdumm.engine.delta_engine import apply_delta_from_file

        pamt_dir = file_path.split("/")[0]
        from cdumm.storage.config import Config as _Config
        vanilla_dir = (
            get_cdmods_root(_Config(self._db), self._game_dir) / "vanilla"
        )
        base_dir = vanilla_dir if vanilla_dir.exists() else self._game_dir

        # ── Step 1: Find which deltas overlap at the same PAMT entry ──
        # Group deltas by the PAMT entry they modify (via byte range overlap
        # or json_patches entry_path).
        # For JSON mods, multiple deltas for the same PAZ often target the
        # same compressed entry — detect this via overlapping byte ranges.

        # Collect json_patches info (fast path)
        patches_by_game_file: dict[str, list[tuple[dict, dict]]] = {}
        for d in deltas:
            jp = d.get("json_patches")
            if not jp:
                continue
            try:
                patch_info = json.loads(jp)
                game_file = patch_info.get("entry_path") or patch_info.get("game_file")
                if game_file:
                    patches_by_game_file.setdefault(game_file, []).append(
                        (d, patch_info))
            except (json.JSONDecodeError, TypeError):
                continue

        # Check for fast-path merges (2+ mods with json_patches for same file)
        fast_merges = {gf: patches for gf, patches in patches_by_game_file.items()
                       if len(patches) >= 2}

        # Check for fallback merges: 2+ mods with overlapping byte ranges
        # but no json_patches data. Group by byte range overlap.
        fallback_groups = self._find_overlapping_delta_groups(deltas, fast_merges)

        if not fast_merges and not fallback_groups:
            return [], deltas

        from cdumm.engine.json_patch_handler import _find_pamt_entry, _extract_from_paz
        merged_deltas = []
        deltas_to_exclude: set[str] = set()

        # ── Fast path: merge using stored JSON patch data ──
        for game_file, mod_patches in fast_merges.items():
            entry = _find_pamt_entry(game_file, base_dir)
            if not entry:
                continue

            try:
                van_paz = base_dir / pamt_dir / f"{entry.paz_index}.paz"
                van_entry = PazEntry(
                    path=entry.path, paz_file=str(van_paz),
                    offset=entry.offset, comp_size=entry.comp_size,
                    orig_size=entry.orig_size, flags=entry.flags,
                    paz_index=entry.paz_index,
                )
                vanilla_content = _extract_from_paz(van_entry)
            except Exception as e:
                logger.warning("JSON merge: can't extract vanilla %s: %s",
                               game_file, e)
                continue

            # Apply all patches: lowest precedence first, highest last
            # (wins). _get_file_deltas orders by m.priority DESC and
            # CDUMM's convention is "lower priority number wins", so
            # the list already arrives losers-first / winner-last.
            # Iterating in list order makes the winner write last and
            # own any overlapping bytes. (A reversed() here previously
            # inverted that and let the lowest-precedence mod win.)
            # Detect the mod's offset convention from its first few
            # string offsets: if any contain a-f, treat ALL string
            # offsets for this mod as hex (Kliff Wears Damiane convention).
            # Otherwise fall back to int(s, 0) which handles "0x..." and
            # decimal. This avoids misinterpreting '120460' as decimal
            # when the mod actually meant 0x120460.
            merged = bytearray(vanilla_content)
            mod_names = []
            import re as _re
            _HEX_ONLY = _re.compile(r"^[0-9a-fA-F]+$")
            _HAS_AF = _re.compile(r"[a-fA-F]")
            for d, patch_info in mod_patches:
                mod_is_bare_hex = False
                try:
                    for ch in patch_info.get("changes", []):
                        r = ch.get("offset")
                        if isinstance(r, str) and _HAS_AF.search(r):
                            mod_is_bare_hex = True
                            break
                except Exception:
                    pass
                for change in patch_info.get("changes", []):
                    raw_off = change.get("offset", 0)
                    try:
                        if isinstance(raw_off, str):
                            if mod_is_bare_hex and _HEX_ONLY.match(raw_off):
                                offset = int(raw_off, 16)
                            else:
                                offset = int(raw_off, 0)
                        else:
                            offset = int(raw_off)
                    except (ValueError, TypeError):
                        logger.warning(
                            "JSON merge: unreadable offset %r in %s — "
                            "skipping patch", raw_off,
                            d.get("mod_name", "?"))
                        continue
                    try:
                        patched = bytes.fromhex(change.get("patched", ""))
                        if offset + len(patched) <= len(merged):
                            merged[offset:offset + len(patched)] = patched
                    except (ValueError, IndexError):
                        continue
                mod_names.append(d.get("mod_name", "?"))
                deltas_to_exclude.add(d["delta_path"])

            if bytes(merged) != vanilla_content:
                merged_deltas.append(self._make_merged_entry(
                    entry, pamt_dir, bytes(merged), mod_names))
                logger.info("JSON merge (fast): %s from %s",
                            game_file, ", ".join(mod_names))

        # ── Fallback: derive patches by diffing each mod's result vs vanilla ──
        for entry_key, group_deltas in fallback_groups.items():
            # entry_key is (pamt_dir, approximate_offset)
            # All deltas in the group overlap at roughly the same PAZ region.
            # Find which PAMT entry they target by parsing the PAMT.
            entry = self._find_entry_at_offset(
                pamt_dir, group_deltas[0], base_dir)
            if not entry:
                continue

            try:
                van_paz = base_dir / pamt_dir / f"{entry.paz_index}.paz"
                van_entry = PazEntry(
                    path=entry.path, paz_file=str(van_paz),
                    offset=entry.offset, comp_size=entry.comp_size,
                    orig_size=entry.orig_size, flags=entry.flags,
                    paz_index=entry.paz_index,
                )
                vanilla_content = _extract_from_paz(van_entry)
            except Exception as e:
                logger.warning("JSON merge fallback: can't extract %s: %s",
                               entry.path, e)
                continue

            # Get vanilla PAZ bytes to apply each mod's delta independently
            van_paz_path = base_dir / pamt_dir / f"{entry.paz_index}.paz"
            if not van_paz_path.exists():
                van_paz_path = self._game_dir / pamt_dir / f"{entry.paz_index}.paz"
            if not van_paz_path.exists():
                continue
            vanilla_paz = van_paz_path.read_bytes()

            # Three-way merge: for each mod, apply its delta to vanilla PAZ,
            # extract the entry, diff against vanilla decompressed content.
            # Collect per-byte changes, then merge.
            merged = bytearray(vanilla_content)
            mod_names = []

            # Apply lowest precedence first. _get_file_deltas orders by
            # m.priority DESC (lower number wins), so the list is already
            # losers-first: iterating in order lets the winner write last.
            for d in group_deltas:
                try:
                    mod_paz = apply_delta_from_file(
                        vanilla_paz, Path(d["delta_path"]))
                    # Extract the entry from the mod's PAZ
                    mod_entry = PazEntry(
                        path=entry.path, paz_file="",
                        offset=entry.offset, comp_size=entry.comp_size,
                        orig_size=entry.orig_size, flags=entry.flags,
                        paz_index=entry.paz_index,
                    )
                    # Read from mod PAZ bytes at the entry offset
                    raw = mod_paz[mod_entry.offset:
                                  mod_entry.offset + mod_entry.comp_size]
                    # Decompress using shared utility
                    from cdumm.engine.json_patch_handler import decompress_entry
                    mod_content = decompress_entry(raw, entry)

                    # Three-way merge: only apply bytes that THIS mod changed
                    for i in range(min(len(vanilla_content), len(mod_content))):
                        if mod_content[i] != vanilla_content[i]:
                            merged[i] = mod_content[i]

                    mod_names.append(d.get("mod_name", "?"))
                    deltas_to_exclude.add(d["delta_path"])
                except Exception as e:
                    logger.debug("JSON merge fallback: failed for %s: %s",
                                 d.get("mod_name", "?"), e)
                    continue

            if len(mod_names) >= 2 and bytes(merged) != vanilla_content:
                merged_deltas.append(self._make_merged_entry(
                    entry, pamt_dir, bytes(merged), mod_names))
                logger.info("JSON merge (fallback): %s from %s",
                            entry.path, ", ".join(mod_names))

        remaining = [d for d in deltas if d["delta_path"] not in deltas_to_exclude]
        return merged_deltas, remaining

    def _make_merged_entry(self, entry, pamt_dir: str,
                           content: bytes, mod_names: list[str]) -> dict:
        """Create a synthetic ENTR-style delta dict for merged content."""
        return {
            "entry_path": entry.path,
            "delta_path": None,
            "_merged_content": content,
            "_merged_metadata": {
                "pamt_dir": pamt_dir,
                "entry_path": entry.path,
                "paz_index": entry.paz_index,
                "compression_type": entry.compression_type,
                "flags": entry.flags,
                "vanilla_offset": entry.offset,
                "vanilla_comp_size": entry.comp_size,
                "vanilla_orig_size": entry.orig_size,
                "encrypted": entry.encrypted,
            },
            "mod_name": " + ".join(mod_names),
        }

    def _find_overlapping_delta_groups(
        self, deltas: list[dict], already_merged: dict,
    ) -> dict[tuple, list[dict]]:
        """Find groups of 2+ deltas with overlapping byte ranges and no json_patches.

        Returns {(pamt_dir, approx_offset): [deltas]} for groups that need
        fallback merging.
        """
        # Skip deltas already handled by fast-path or that have entry_path
        already_files = set()
        for gf, patches in already_merged.items():
            for d, _ in patches:
                already_files.add(d["delta_path"])

        # Group byte-range deltas by approximate region (same file, overlapping ranges).
        # Skip FULL_COPY deltas — they replace the entire file and are handled
        # correctly by _compose_file's standard full_replace logic.
        from collections import defaultdict
        range_deltas = []
        for d in deltas:
            if d["delta_path"] in already_files:
                continue
            if d.get("entry_path") or d.get("is_new") or d.get("json_patches"):
                continue
            # Skip FULL_COPY deltas (byte_start=0 and huge range = full file)
            try:
                dp = Path(d["delta_path"])
                with open(dp, "rb") as f:
                    magic = f.read(4)
                if magic == b"FULL":
                    continue
            except Exception:
                continue
            # Read byte range from DB
            try:
                row = self._db.connection.execute(
                    "SELECT byte_start, byte_end FROM mod_deltas WHERE delta_path = ? LIMIT 1",
                    (d["delta_path"],)).fetchone()
                if row and row[0] is not None:
                    range_deltas.append((row[0], row[1], d))
            except Exception:
                continue

        if len(range_deltas) < 2:
            return {}

        # Find overlapping pairs
        range_deltas.sort(key=lambda x: x[0])
        groups: dict[int, list[dict]] = {}
        used = set()

        for i in range(len(range_deltas)):
            if i in used:
                continue
            s1, e1, d1 = range_deltas[i]
            group = [d1]
            group_id = i
            for j in range(i + 1, len(range_deltas)):
                if j in used:
                    continue
                s2, e2, d2 = range_deltas[j]
                if s2 < e1:  # overlap
                    group.append(d2)
                    used.add(j)
                    e1 = max(e1, e2)
            if len(group) >= 2:
                used.add(i)
                groups[s1] = group

        # Convert to keyed format
        pamt_dir = ""
        if groups:
            pamt_dir = list(groups.values())[0][0].get("delta_path", "").split("/")[-1]
            # Actually get from file_path
        result = {}
        for offset, grp in groups.items():
            result[("", offset)] = grp

        return result

    def _find_entry_at_offset(self, pamt_dir: str, delta: dict,
                              base_dir) -> "PazEntry | None":
        """Find the PAMT entry whose compressed data occupies a given
        PAZ offset.

        R3: shares the PAMT entries cache with _get_vanilla_entry_content
        so the JSON-merge fallback loop doesn't re-parse the PAMT once
        per overlapping group.
        """
        from cdumm.archive.paz_parse import parse_pamt

        try:
            row = self._db.connection.execute(
                "SELECT byte_start, byte_end FROM mod_deltas WHERE delta_path = ? LIMIT 1",
                (delta["delta_path"],)).fetchone()
            if not row or row[0] is None:
                return None
            target_offset = row[0]

            pamt_path = base_dir / pamt_dir / "0.pamt"
            if not pamt_path.exists():
                return None

            if not hasattr(self, "_pamt_entries_cache"):
                self._pamt_entries_cache: dict[str, list] = {}
            cache_key = str(pamt_path)
            entries = self._pamt_entries_cache.get(cache_key)
            if entries is None:
                entries = parse_pamt(
                    str(pamt_path), str(base_dir / pamt_dir))
                self._pamt_entries_cache[cache_key] = entries
            # Find entry whose offset range contains our target
            for e in entries:
                if e.offset <= target_offset < e.offset + e.comp_size:
                    return e
        except Exception as e:
            logger.debug("Failed to find entry at offset: %s", e)
        return None

    def _apply_entry_deltas(self, file_path: str, buf: bytearray,
                            entry_deltas: list[dict]) -> bytes:
        """Apply entry-level deltas to a PAZ file buffer.

        Each entry delta stores decompressed file content + PAMT entry metadata.
        The content is recompressed and written at the entry's offset in the PAZ.
        If the recompressed data doesn't fit, it's appended to the end.

        PAMT updates are tracked in self._pamt_entry_updates for Phase 2.
        """
        from cdumm.archive.paz_parse import PazEntry
        from cdumm.archive.paz_repack import repack_entry_bytes
        from cdumm.engine.delta_engine import load_entry_delta

        pamt_dir = file_path.split("/")[0]

        # Group by entry_path — last mod (highest priority in sorted order) wins
        by_entry: dict[str, dict] = {}
        for d in entry_deltas:
            by_entry[d["entry_path"]] = d

        for entry_path, d in by_entry.items():
            # Support both on-disk ENTR deltas and in-memory merged content
            if d.get("_merged_content") is not None:
                content = d["_merged_content"]
                metadata = d["_merged_metadata"]
            elif d.get("delta_path"):
                try:
                    content, metadata = load_entry_delta(Path(d["delta_path"]))
                except Exception as e:
                    # Bug D: surface to user
                    self._warn_entr_load_failure(d, e)
                    continue
            else:
                continue

            entry = PazEntry(
                path=metadata["entry_path"],
                paz_file="",
                offset=metadata["vanilla_offset"],
                comp_size=metadata["vanilla_comp_size"],
                orig_size=metadata["vanilla_orig_size"],
                flags=metadata["flags"],
                paz_index=metadata["paz_index"],
                _encrypted_override=metadata.get("encrypted"),
            )

            try:
                payload, actual_comp, actual_orig = repack_entry_bytes(
                    content, entry, allow_size_change=True)
            except Exception as e:
                logger.warning("Failed to repack entry %s: %s", entry_path, e)
                continue

            new_offset = entry.offset
            new_paz_size = None

            if actual_comp > entry.comp_size:
                # Doesn't fit — append to end of PAZ
                new_offset = len(buf)
                buf.extend(payload)
                new_paz_size = len(buf)
                logger.info("Entry %s appended at offset %d (grew %d->%d)",
                            entry_path, new_offset, entry.comp_size, actual_comp)
            else:
                # Fits in original slot
                buf[entry.offset:entry.offset + len(payload)] = payload

            # Track PAMT update for Phase 2
            self._pamt_entry_updates.setdefault(pamt_dir, []).append({
                "entry": entry,
                "new_comp_size": actual_comp,
                "new_offset": new_offset,
                "new_orig_size": actual_orig,
                "new_paz_size": new_paz_size,
            })

            logger.info("Applied entry delta: %s in %s from %s",
                        entry_path, file_path, d.get("mod_name", "?"))

        return bytes(buf)

    def _compose_pamt(self, pamt_path: str, pamt_dir: str,
                      byte_deltas: list[dict],
                      entry_updates: list[dict]) -> bytes | None:
        """Compose a PAMT file from vanilla + entry updates + byte deltas.

        Entry updates come from PAZ entry-level composition (Phase 1).
        Byte deltas come from non-script mods that modify the PAMT directly.
        """
        vanilla = self._get_vanilla_bytes(pamt_path)
        if vanilla is None:
            game_path = self._game_dir / pamt_path.replace("/", os.sep)
            if game_path.exists():
                vanilla = game_path.read_bytes()
            else:
                logger.warning("PAMT not found: %s", pamt_path)
                return None

        buf = bytearray(vanilla)

        # Apply entry-level PAMT updates (from PAZ entry composition)
        for update in entry_updates:
            _apply_pamt_entry_update(buf, update)

        # Apply byte-level PAMT deltas on top (from zip/JSON mods)
        if byte_deltas:
            current = bytes(buf)
            for d in byte_deltas:
                try:
                    current = apply_delta_from_file(
                        current, Path(d["delta_path"]))
                except ValueError as e:
                    # Corrupt delta: skip this mod's PAMT delta rather
                    # than writing garbage over the index.
                    self._warn_corrupt_delta(d, e)
            buf = bytearray(current)

        # Recompute PAMT hash
        from cdumm.archive.hashlittle import compute_pamt_hash
        correct_hash = compute_pamt_hash(bytes(buf))
        stored_hash = struct.unpack_from("<I", buf, 0)[0]
        if stored_hash != correct_hash:
            struct.pack_into("<I", buf, 0, correct_hash)
            logger.info("Recomputed PAMT hash for %s: %08X -> %08X",
                        pamt_path, stored_hash, correct_hash)

        return bytes(buf)

    def _get_vanilla_bytes(self, file_path: str) -> bytes | None:
        """Get vanilla version of a file from backup (range or full).

        Warning: range backup only covers positions that mods explicitly
        touched. If the game file has modifications outside those positions
        (from other mods or manual edits), those leak into the result.
        """
        # Try full backup first
        full_path = self._vanilla_dir / file_path.replace("/", os.sep)
        if full_path.exists():
            return full_path.read_bytes()

        # Try range backup — reconstruct vanilla from game file + ranges
        game_path = self._game_dir / file_path.replace("/", os.sep)
        if not game_path.exists():
            return None

        range_entries = _load_range_backup(self._vanilla_dir, file_path)
        if range_entries:
            buf = bytearray(game_path.read_bytes())
            _apply_ranges_to_buf(buf, range_entries)
            result = bytes(buf)
            # Verify reconstructed vanilla against snapshot
            try:
                snap = self._db.connection.execute(
                    "SELECT file_hash FROM snapshots WHERE file_path = ?",
                    (file_path,)).fetchone()
                if snap:
                    import xxhash
                    h = xxhash.xxh3_128(result).hexdigest()
                    if h != snap[0]:
                        logger.warning(
                            "Range-reconstructed vanilla for %s doesn't match snapshot "
                            "(game file may have untracked modifications)", file_path)
            except Exception:
                pass
            return result

        return None

    def _verify_vanilla_files(self, txn, active_files: set[str],
                              modified_pamts: dict[str, bytes]) -> None:
        """Safety net: find files that should be vanilla but aren't.

        After a mod is removed, its deltas are deleted from the DB. But the
        game files may still be modded. Two detection methods:
        1. Size mismatch vs snapshot (fast, catches most cases)
        2. Vanilla backup exists but no enabled mod manages the file
           (catches same-size modifications like PAMT byte patches)
        """
        try:
            cursor = self._db.connection.execute(
                "SELECT file_path, file_hash, file_size FROM snapshots")
            snap_map = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
        except Exception:
            return

        # Method 1: size mismatch
        for file_path, (snap_hash, snap_size) in snap_map.items():
            if file_path in active_files or file_path == "meta/0.papgt":
                continue
            game_file = self._game_dir / file_path.replace("/", os.sep)
            if not game_file.exists():
                continue
            try:
                actual_size = game_file.stat().st_size
            except OSError:
                continue
            if actual_size != snap_size:
                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes:
                    txn.stage_file(file_path, vanilla_bytes)
                    if file_path.endswith(".pamt"):
                        modified_pamts[file_path.split("/")[0]] = vanilla_bytes
                    logger.warning("Restored orphaned file to vanilla: %s "
                                   "(size %d != snapshot %d)",
                                   file_path, actual_size, snap_size)

        # Method 2: vanilla backup exists but file isn't actively managed.
        # If we have a backup (range or full) for a file, it was previously
        # modified. If no enabled mod touches it now, restore it.
        if not self._vanilla_dir or not self._vanilla_dir.exists():
            return
        for backup in self._vanilla_dir.rglob("*"):
            if not backup.is_file():
                continue
            # Determine the game file path from backup path
            if backup.name.endswith(".vranges"):
                # Range backup: filename is file_path with / replaced by _
                rel = backup.name[:-len(".vranges")].replace("_", "/")
            else:
                rel = str(backup.relative_to(self._vanilla_dir)).replace("\\", "/")

            if rel in active_files or rel == "meta/0.papgt":
                continue
            if rel not in snap_map:
                continue

            game_file = self._game_dir / rel.replace("/", os.sep)
            if not game_file.exists():
                continue

            # This file has a backup but no enabled mod manages it — restore
            vanilla_bytes = self._get_vanilla_bytes(rel)
            if vanilla_bytes:
                snap_hash, snap_size = snap_map[rel]
                # Only restore if file actually differs from vanilla
                import hashlib
                if len(vanilla_bytes) == snap_size:
                    game_bytes = game_file.read_bytes()
                    if game_bytes != vanilla_bytes:
                        txn.stage_file(rel, vanilla_bytes)
                        if rel.endswith(".pamt"):
                            modified_pamts[rel.split("/")[0]] = vanilla_bytes
                        logger.warning("Restored orphaned file to vanilla: %s "
                                       "(backup exists, no active mod)", rel)

    def _update_pathc_for_overlay(self, txn, overlay_packed) -> None:
        """Register DDS overlay entries in meta/0.pathc.

        The game uses PATHC as a texture path index to find DDS files.
        Without registration, DDS textures in the overlay PAZ are invisible
        to the game's texture loader.

        Mirrors JMM's ``UpdatePathcForTextures``: reads the m-values and
        DDS template header straight from the bytes build_overlay produced
        (``BuildPartialDdsPayload`` output), not from pre-build content, so
        PATHC and overlay PAZ agree on reserved1 / last4.

        If the vanilla PATHC cannot be decompressed (game-version
        compression change, encrypted file, corrupted backup), log a
        warning and skip DDS registration rather than failing the
        whole apply. The user's non-DDS mods still apply successfully;
        DDS-only mods may not appear in-game until the PATHC issue is
        resolved (usually by re-running 'Fix Everything' to rebuild
        the vanilla backup).
        """
        if not overlay_packed:
            return
        try:
            self._update_pathc_for_overlay_inner(txn, overlay_packed)
        except Exception as e:
            msg = (f"DDS texture registration failed: {e}. "
                   f"Non-DDS mods still applied. If any DDS texture mods "
                   f"look wrong in-game, run Settings → Fix Everything "
                   f"to rebuild the vanilla PATHC backup, then Apply "
                   f"again.")
            logger.error("PATHC update skipped: %s", e, exc_info=True)
            if hasattr(self, "_soft_warnings"):
                self._soft_warnings.append(msg)
            try:
                self.warning.emit(msg)
            except Exception:
                pass

    def _update_pathc_for_overlay_inner(self, txn, overlay_packed) -> None:
        """Inner implementation — kept separate so the public entry
        point can wrap it in a broad try/except."""

        # Build a lookup of DDS entries {entry_path: (OverlayEntry, content_bytes)}.
        dds_entries: list[tuple[str, "OverlayEntry", bytes]] = []
        for content, metadata in self._overlay_entries:
            entry_path = metadata.get("entry_path", "")
            if entry_path.lower().endswith(".dds"):
                dds_entries.append((entry_path, None, content))
        if not dds_entries:
            return

        # Index overlay_packed by "dir_path/filename" so we can pair each
        # source (entry_path, content) with the OverlayEntry carrying the
        # final m-values.
        packed_by_filename: dict[str, "OverlayEntry"] = {}
        for oe in overlay_packed:
            packed_by_filename[oe.filename.lower()] = oe

        # Backup vanilla PATHC if not already backed up.
        pathc_path = self._game_dir / "meta" / "0.pathc"
        if not pathc_path.exists():
            logger.debug("No meta/0.pathc found, skipping DDS registration")
            return

        vanilla_pathc = self._vanilla_dir / "meta" / "0.pathc"
        if not vanilla_pathc.exists():
            vanilla_pathc.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(pathc_path, vanilla_pathc)
            logger.info("Backed up vanilla PATHC: %s", vanilla_pathc)

        try:
            from cdumm.archive.pathc_handler import (
                read_pathc, serialize_pathc, update_entry, get_path_hash,
            )
        except ImportError:
            logger.debug("PATHC handler not available, skipping DDS registration")
            return

        try:
            pathc = read_pathc(vanilla_pathc)
            import bisect
            import struct as _st

            updated = 0
            added = 0
            preserved = 0
            for entry_path, _placeholder, content in dds_entries:
                filename = entry_path.rsplit("/", 1)[-1]
                oe = packed_by_filename.get(filename.lower())
                # Prefer m-values from the OverlayEntry (authoritative for
                # what went into the overlay PAZ). Fall back to re-reading
                # from the content bytes if the field is missing.
                if oe and oe.dds_m_values is not None:
                    m = oe.dds_m_values
                elif len(content) >= 128 and content[:4] == b"DDS ":
                    # Pathological fallback — OverlayEntry lost its m-values
                    # (e.g. old cache schema). Recompute from the source bytes
                    # so we never register PATHC entries with zero m-values
                    # (game would reject the texture).
                    try:
                        from cdumm.archive.overlay_builder import (
                            _build_dds_partial_payload,
                        )
                        _, m = _build_dds_partial_payload(content)
                    except Exception as e:
                        logger.warning(
                            "PATHC update: partial-payload fallback "
                            "failed for %s: %s — skipping entry",
                            entry_path, e)
                        continue
                else:
                    logger.warning(
                        "PATHC update: entry %s has neither OverlayEntry "
                        "m-values nor a valid DDS header — skipping",
                        entry_path)
                    continue

                # PATHC keys are FULL hierarchical paths (e.g. "/ui/texture/
                # cd_icon_map_00.dds"), NOT the flattened PAMT entry_path
                # ("ui/cd_icon_map_00.dds"). OverlayEntry.dir_path carries
                # the full folder path resolved via _build_full_path_map;
                # prefer that when available. Fall back to entry_path for
                # cases where the builder couldn't resolve a dir_path.
                if oe and oe.dir_path:
                    vpath = "/" + oe.dir_path.strip("/") + "/" + filename
                else:
                    vpath = "/" + entry_path.lstrip("/")
                target_hash = get_path_hash(vpath)
                idx = bisect.bisect_left(pathc.key_hashes, target_hash)
                existing = (idx < len(pathc.key_hashes)
                            and pathc.key_hashes[idx] == target_hash)
                if existing and pathc.map_entries[idx].m1 == m[0]:
                    preserved += 1
                    continue

                # DDS template record for PATHC — JMM uses the header+padding
                # portion of the MOD's DDS bytes (clipped to pathc's per-record
                # size). Detect DX10 via fourcc for correct 148-byte length.
                record_size = pathc.header.dds_record_size
                fourcc = content[84:88] if len(content) >= 88 else b""
                head_size = 148 if (fourcc == b"DX10" and len(content) >= 148) else 128
                dds_rec = bytearray(record_size)
                to_copy = min(len(content), head_size, record_size)
                dds_rec[:to_copy] = content[:to_copy]
                dds_rec = bytes(dds_rec)

                try:
                    dds_idx = pathc.dds_records.index(dds_rec)
                except ValueError:
                    pathc.dds_records.append(dds_rec)
                    dds_idx = len(pathc.dds_records) - 1

                update_entry(pathc, vpath, dds_idx, m)
                if existing:
                    updated += 1
                else:
                    added += 1

            pathc.header.dds_record_count = len(pathc.dds_records)
            pathc.header.hash_count = len(pathc.key_hashes)

            pathc_bytes = serialize_pathc(pathc)
            # Fast path: if every DDS overlay entry was preserved (same m1
            # as vanilla) we're writing bytes identical to vanilla. Check
            # live file and skip the stage+commit cycle if it already
            # matches. Saves ~6.8 MB staging write + atomic-rename churn
            # every texture apply where no DDS index actually changed.
            staged = txn.stage_file_if_changed("meta/0.pathc", pathc_bytes)
            logger.info("Updated PATHC: %d updated, %d added, %d preserved "
                        "(%d DDS overlay entries)%s",
                        updated, added, preserved, len(dds_entries),
                        "" if staged else " — already in sync, skipped write")

        except Exception as e:
            logger.error("Failed to update PATHC for DDS overlay: %s", e, exc_info=True)

    def _apply_language_redirect(self, file_deltas: dict, revert_files: set
                                 ) -> tuple[dict, set]:
        """Redirect standalone PAZ deltas from a localisation group that
        doesn't match the user's Steam language.

        Ports JMM ``CmdApply`` language-redirect logic (ModManager.cs:3403-
        3472): when a PAZ-replacement mod targets ``0020`` (English) but
        the user is running Korean (``0019``), rewrite every ``0020/``
        delta key to ``0019/`` and — for the ``.pamt`` file — replace the
        embedded ``localizationstring_eng.paloc`` filename with
        ``localizationstring_kor.paloc`` so the game's VFS resolves the
        mod under the correct per-language slot.

        Returns a possibly new ``(file_deltas, revert_files)`` pair. If no
        redirect is needed the inputs are returned unchanged.
        """
        try:
            from cdumm.engine.language import (
                STEAM_LANG_TO_GROUP, LOCALIZATION_GROUPS,
                GROUP_TO_PALOC_SUFFIX, detect_steam_language,
            )
            from cdumm.archive.paz_parse import rewrite_pamt_localization_filename
        except ImportError:
            return file_deltas, revert_files

        lang = detect_steam_language(self._game_dir)
        user_group = STEAM_LANG_TO_GROUP.get(lang.lower()) if lang else None
        if not user_group or user_group not in LOCALIZATION_GROUPS:
            return file_deltas, revert_files

        # Collect keys needing redirect.
        redirects: list[tuple[str, str, str]] = []  # (old_key, new_key, source_group)
        for fp in list(file_deltas.keys()):
            top = fp.split("/", 1)[0] if "/" in fp else fp
            if top in LOCALIZATION_GROUPS and top != user_group:
                new_key = fp.replace(f"{top}/", f"{user_group}/", 1)
                redirects.append((fp, new_key, top))

        if not redirects:
            return file_deltas, revert_files

        from_suffixes = {src: GROUP_TO_PALOC_SUFFIX.get(src) for _, _, src in redirects}
        to_suffix = GROUP_TO_PALOC_SUFFIX.get(user_group)
        logger.info(
            "language redirect: user=%s group=%s — redirecting %d delta key(s)",
            lang, user_group, len(redirects))

        new_file_deltas: dict = dict(file_deltas)
        for old_key, new_key, src_group in redirects:
            deltas = new_file_deltas.pop(old_key, None)
            if deltas is None:
                continue
            # Copy each delta dict so `_rewritten_bytes` never leaks onto
            # the underlying objects that other code paths may share.
            deltas = [dict(d) for d in deltas]
            from_suffix = from_suffixes.get(src_group)
            # Rewrite PAMT bytes in-place for every is_new PAMT delta under
            # this key so the staged bytes carry the correct .paloc name.
            if old_key.endswith(".pamt") and from_suffix and to_suffix:
                for d in deltas:
                    if not d.get("is_new"):
                        continue
                    dp = d.get("delta_path")
                    if not dp:
                        continue
                    try:
                        src_bytes = Path(dp).read_bytes()
                    except OSError as e:
                        logger.warning("lang redirect: read failed %s: %s", dp, e)
                        continue
                    rewritten = rewrite_pamt_localization_filename(
                        src_bytes, from_suffix, to_suffix)
                    if rewritten:
                        # Stash rewritten bytes on the delta so _compose_pamt
                        # / new-PAMT handler picks them up.
                        d["_rewritten_bytes"] = rewritten
                        logger.info(
                            "lang redirect: %s → %s (PAMT filename: %s→%s)",
                            old_key, new_key, from_suffix, to_suffix)
            new_file_deltas[new_key] = deltas

        # Also redirect any revert paths pointing at the original language dir.
        new_reverts = set()
        for rf in revert_files:
            top = rf.split("/", 1)[0]
            if top in LOCALIZATION_GROUPS and top != user_group:
                new_reverts.add(rf.replace(f"{top}/", f"{user_group}/", 1))
            else:
                new_reverts.add(rf)

        return new_file_deltas, new_reverts

    def _allocate_overlay_dir(self, staged_dirs: set[str] | None = None) -> str:
        """Find the next available 4-digit directory >= 0037 for the overlay PAZ.

        Args:
            staged_dirs: directories already claimed by standalone mods in this
                         apply session (from file_deltas). Overlay must not
                         collide with these.
        """
        taken = staged_dirs or set()
        max_num = 36  # start after 0036 (used by standalone mods)
        for d in self._game_dir.iterdir():
            if d.is_dir() and d.name.isdigit() and len(d.name) == 4:
                num = int(d.name)
                if num > max_num:
                    max_num = num
        for d in taken:
            if d.isdigit() and len(d) == 4:
                num = int(d)
                if num > max_num:
                    max_num = num
        overlay_num = max_num + 1
        return f"{overlay_num:04d}"

    def _get_files_to_revert(self, enabled_files: set[str]) -> list[str]:
        """Find files modified by disabled mods that no enabled mod covers.

        For ENTR deltas (entry-level PAZ modifications), the PAMT is also
        modified during apply but has no delta record. Include the PAMT
        for any PAZ directory being reverted so it's restored to vanilla too.
        """
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path, md.entry_path "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 0 AND m.mod_type = 'paz'"
        )
        disabled_files: set[str] = set()
        disabled_pamt_dirs: set[str] = set()
        for file_path, entry_path in cursor.fetchall():
            disabled_files.add(file_path)
            # Track directories where disabled ENTR deltas modified the PAMT
            if entry_path and "/" in file_path:
                disabled_pamt_dirs.add(file_path.rsplit("/", 1)[0])

        # Only add PAMTs for directories where NO enabled mod has ENTR deltas.
        # If an enabled mod uses the same directory, the PAMT will be updated
        # by the ENTR apply (Phase 2) and must NOT be overwritten by revert.
        enabled_entr_dirs: set[str] = set()
        for fp in enabled_files:
            if "/" in fp:
                enabled_entr_dirs.add(fp.rsplit("/", 1)[0])
        for pamt_dir in disabled_pamt_dirs - enabled_entr_dirs:
            disabled_files.add(pamt_dir + "/0.pamt")

        return sorted(disabled_files - enabled_files)

    def _get_new_files_to_delete(self, enabled_files: set[str]) -> set[str]:
        """Find new files from disabled mods that no enabled mod provides."""
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 0 AND m.mod_type = 'paz' AND md.is_new = 1"
        )
        disabled_new = {row[0] for row in cursor.fetchall()}
        # Don't delete if an enabled mod also provides this new file
        return disabled_new - enabled_files

    def _get_file_deltas(self) -> dict[str, list[dict]]:
        """Get all deltas for enabled mods, grouped by file path."""
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path, md.delta_path, m.name, "
            "md.is_new, md.entry_path, md.json_patches, m.force_inplace, "
            "m.game_version_hash, md.byte_end, m.json_source, m.priority "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
            "ORDER BY CASE WHEN m.conflict_mode='override' THEN 1 ELSE 0 END, "
            "m.priority DESC, md.file_path"
        )

        file_deltas: dict[str, list[dict]] = {}
        seen_deltas: set[str] = set()

        for file_path, delta_path, mod_name, is_new, entry_path, json_patches, force_inplace, game_ver_hash, byte_end, json_source, m_priority in cursor.fetchall():
            # #145 Option Y originally hard-skipped ENTR deltas from
            # mods with json_source, assuming the Phase 1a aggregator
            # would always cover them. That silently dropped a mod
            # whenever process_json_patches_for_overlay skipped its
            # patch (byte identity vs vanilla, mount-time extract
            # failure, non-data-table mismatch, missing source.json).
            # SirFapZalot's packed Improved Controller hit that on
            # v3.1.3 — aggregator drops, ENTR skipped, mod inert.
            #
            # Fix: keep the ENTR delta in play. When the aggregator
            # DOES produce an overlay entry, we end up with two
            # entries for the same (pamt_dir, entry_path); they
            # collide in _merge_same_target_overlay_entries, which
            # picks the priority-winner (aggregator tags its entries
            # with the lowest priority number among contributors, so
            # it wins on overlap while the ENTR remains as fallback).
            if delta_path in seen_deltas:
                continue
            # Skip deltas whose files are missing (zombie entries from old resets)
            if not Path(delta_path).exists():
                logger.warning("Skipping missing delta: %s (%s)", delta_path, mod_name)
                continue
            seen_deltas.add(delta_path)
            d = {
                "delta_path": delta_path,
                "mod_name": mod_name,
                "is_new": bool(is_new),
                # Carried so downstream winner-selection (e.g. the
                # full-replace branch in _compose_file, which sorts
                # ascending and picks [0] because the lower priority
                # number wins) has a real key to sort on. Without it,
                # every delta sorted as 0 and the winner was whatever
                # order the SQL produced.
                "priority": m_priority if m_priority is not None else 0,
            }
            if entry_path:
                d["entry_path"] = entry_path
            if json_patches:
                d["json_patches"] = json_patches
            if force_inplace:
                d["force_inplace"] = True
            file_deltas.setdefault(file_path, []).append(d)

        return file_deltas


class RevertWorker(QObject):
    """Background worker for revert operation."""

    progress_updated = Signal(int, str)
    finished = Signal()
    error_occurred = Signal(str)
    warning = Signal(str)

    def __init__(self, game_dir: Path, vanilla_dir: Path, db_path: Path) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir
        self._db_path = db_path

    def run(self) -> None:
        try:
            self._db = Database(self._db_path)
            self._db.initialize()
            self._revert()
            self._db.close()
        except Exception as e:
            logger.error("Revert failed: %s", e, exc_info=True)
            self.error_occurred.emit(f"Revert failed: {e}")

    def _revert(self) -> None:
        """Revert all mod-affected files to vanilla using range or full backups."""
        # Invalidate apply fingerprint
        try:
            from cdumm.storage.config import Config as _Config
            fp_path = (
                get_cdmods_root(_Config(self._db), self._game_dir)
                / ".apply_fingerprint"
            )
            if fp_path.exists():
                fp_path.unlink()
        except Exception:
            pass

        # Get all files any mod has ever touched
        cursor = self._db.connection.execute(
            "SELECT DISTINCT file_path, is_new, entry_path FROM mod_deltas")
        rows = cursor.fetchall()
        mod_files = [row[0] for row in rows]
        new_files = {row[0] for row in rows if row[1]}
        # Files with ONLY entry deltas (overlay) — game files are untouched
        entr_files: set[str] = set()
        byte_files: set[str] = set()
        for fp, is_new, entry_path in rows:
            if entry_path:
                entr_files.add(fp)
            else:
                byte_files.add(fp)
        overlay_only_files = entr_files - byte_files  # files that ONLY have ENTR deltas

        if not mod_files:
            self.error_occurred.emit("No mod data found. Nothing to revert.")
            return

        total = len(mod_files)
        self.progress_updated.emit(0, f"Reverting {total} file(s) to vanilla...")

        staging_dir = self._game_dir / ".cdumm_staging"
        staging_dir.mkdir(exist_ok=True)
        txn = TransactionalIO(self._game_dir, staging_dir)

        reverted = 0
        failed_files: list[str] = []
        try:
            for i, file_path in enumerate(mod_files):
                pct = int((i / total) * 90)
                self.progress_updated.emit(pct, f"Restoring {file_path}...")
                _yield_gil()

                if file_path in new_files:
                    # New file — delete it (didn't exist in vanilla)
                    game_path = self._game_dir / file_path.replace("/", os.sep)
                    if game_path.exists():
                        game_path.unlink()
                        logger.info("Deleted mod-added file: %s", file_path)
                        reverted += 1
                    continue

                if file_path in overlay_only_files:
                    # ENTR-only file — game file was never modified (overlay handles it)
                    # No backup needed, just skip. Overlay cleanup happens below.
                    reverted += 1
                    continue

                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes:
                    txn.stage_file(file_path, vanilla_bytes)
                    reverted += 1
                else:
                    logger.warning("Cannot revert %s — no backup found", file_path)
                    failed_files.append(file_path)

            if reverted == 0:
                self.error_occurred.emit(
                    "No vanilla backups found. Use Steam 'Verify Integrity' to restore.")
                return

            # Restore implicitly modified files (PATHC, PAMTs with CRC fixes)
            for implicit_file in ["meta/0.pathc"]:
                vanilla_bytes = self._get_vanilla_bytes(implicit_file)
                if vanilla_bytes:
                    game_path = self._game_dir / implicit_file.replace("/", os.sep)
                    if game_path.exists():
                        current = game_path.read_bytes()
                        if current != vanilla_bytes:
                            txn.stage_file(implicit_file, vanilla_bytes)
                            logger.info("Restored implicit backup: %s", implicit_file)
                            reverted += 1

            # Restore any PAMTs and PAZs that differ from vanilla.
            # GitHub #71 (jscrump1278): this loop reads every vanilla
            # backup PAZ (some 100+MB) AND the matching live PAZ for a
            # bytes-equal check, with NO progress updates between 90%
            # and 91%. On a typical 36-dir install that's ~7GB of disk
            # I/O while the progress bar appears frozen, leading users
            # to assume the worker hung. Emit per-dir progress so the
            # bar moves and add a fast hash-comparison short-circuit
            # before the full bytes-compare.
            vanilla_dirs = sorted(
                d for d in self._game_dir.iterdir()
                if d.is_dir() and d.name.isdigit() and len(d.name) == 4
                and int(d.name) < 36
            )
            for vd_idx, d in enumerate(vanilla_dirs):
                # The percent stays at 90 (orphan cleanup is at 91),
                # but the status message changes per dir so users see
                # the worker is actively making progress (not hung).
                self.progress_updated.emit(
                    90,
                    f"Verifying {d.name}/ "
                    f"({vd_idx + 1}/{len(vanilla_dirs)})...")
                _yield_gil()
                for fname in ["0.pamt", "0.paz"]:
                    rel = f"{d.name}/{fname}"
                    # Per-file try/except so a single read failure
                    # (locked file, antivirus interference, transient
                    # I/O error) doesn't abort the entire revert and
                    # leave the user stuck. Log + continue.
                    try:
                        vanilla_bytes = self._get_vanilla_bytes(rel)
                    except OSError as e:
                        logger.warning(
                            "Revert verify: skipped %s due to read "
                            "error: %s", rel, e)
                        continue
                    if vanilla_bytes:
                        fpath = d / fname
                        if fpath.exists():
                            try:
                                actual_size = fpath.stat().st_size
                                if actual_size == len(vanilla_bytes):
                                    # Same size — check content. For
                                    # large files, stream-hash live
                                    # and compare to in-memory hash of
                                    # vanilla_bytes. Avoids loading
                                    # 200MB at once for 100MB PAZ.
                                    if actual_size > 5 * 1024 * 1024:
                                        import hashlib
                                        h = hashlib.sha256()
                                        with open(fpath, "rb") as f:
                                            while True:
                                                chunk = f.read(1024 * 1024)
                                                if not chunk:
                                                    break
                                                h.update(chunk)
                                        live_h = h.digest()
                                        van_h = hashlib.sha256(
                                            vanilla_bytes).digest()
                                        if live_h != van_h:
                                            txn.stage_file(rel, vanilla_bytes)
                                            logger.info(
                                                "Restored %s (content diff)",
                                                rel)
                                            reverted += 1
                                    else:
                                        if fpath.read_bytes() != vanilla_bytes:
                                            txn.stage_file(rel, vanilla_bytes)
                                            logger.info(
                                                "Restored %s (content diff)",
                                                rel)
                                            reverted += 1
                                elif actual_size != len(vanilla_bytes):
                                    txn.stage_file(rel, vanilla_bytes)
                                    logger.info(
                                        "Restored %s (size diff)", rel)
                                    reverted += 1
                            except OSError as e:
                                # File locked / antivirus / transient
                                # I/O. Log + continue to next file
                                # rather than aborting the whole
                                # revert.
                                logger.warning(
                                    "Revert verify: read failure on "
                                    "%s, skipping: %s", rel, e)
                                continue

            # Clean up orphan mod directories (0036+) that are empty or
            # only existed because of standalone mods. Respect the same
            # external-tool protection as the apply path (#83): a 0036+
            # dir with a 0.pamt file is being actively used by some
            # other patcher (e.g. HAWT) and must not be touched.
            self.progress_updated.emit(91, "Cleaning orphan directories...")
            try:
                from cdumm.storage.config import Config as _Cfg
                _raw = _Cfg(self._db).get("protected_external_dirs") or ""
                _user_protected = {
                    s.strip() for s in _raw.split(",") if s.strip()}
            except Exception:
                _user_protected = set()
            # Deletions are DEFERRED until after txn.commit(); the
            # vanilla PAPGT restored below has no entries for mod dirs
            # anyway, and a failed commit rolls back to the pre-revert
            # PAPGT which may still reference them (audit finding C1).
            revert_dir_deletions: list[Path] = []
            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) < 36:
                    continue
                if d.name in _user_protected:
                    logger.info(
                        "Fix Everything: keeping user-protected dir %s",
                        d.name)
                    continue
                # #141 marker check: stale CDUMM overlay dir (from a
                # prior apply) should always be cleaned during Fix
                # Everything since the user's intent is "revert to
                # vanilla".
                if (d / "_cdumm_overlay.marker").exists():
                    revert_dir_deletions.append(d)
                    logger.info(
                        "Fix Everything: queued stale CDUMM overlay "
                        "dir %s for post-commit deletion", d.name)
                    continue
                # Same widened check as the apply path (#83): any
                # .pamt or .paz artifact in the dir means an external
                # tool owns the slot and we must not delete it.
                try:
                    has_paz_artifacts = any(
                        c.suffix.lower() in (".pamt", ".paz")
                        for c in d.iterdir()
                    )
                except OSError as _e:
                    logger.warning(
                        "Fix Everything: could not list %s, keeping "
                        "to be safe: %s", d.name, _e)
                    has_paz_artifacts = True
                if has_paz_artifacts:
                    logger.info(
                        "Fix Everything: keeping externally-managed "
                        "dir %s (has PAZ artifacts)", d.name)
                    continue
                # Check if this directory is in the snapshot (vanilla)
                snap_check = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",),
                ).fetchone()[0]
                if snap_check == 0:
                    # Not in snapshot: orphan from mods, queue removal
                    revert_dir_deletions.append(d)
                    logger.info(
                        "Queued orphan mod directory for post-commit "
                        "deletion: %s", d.name)

            # Restore vanilla PAPGT.
            # Always rebuild from scratch during revert to ensure only vanilla
            # directories are included. The backup may be stale (created after
            # a standalone mod added directory 0036+).
            self.progress_updated.emit(92, "Restoring PAPGT...")
            vanilla_papgt = self._vanilla_dir / "meta" / "0.papgt"
            snap_papgt = self._db.connection.execute(
                "SELECT file_size FROM snapshots WHERE file_path = 'meta/0.papgt'"
            ).fetchone()

            # Use backup only if its size matches the snapshot (truly vanilla)
            if (vanilla_papgt.exists() and snap_papgt
                    and vanilla_papgt.stat().st_size == snap_papgt[0]):
                txn.stage_file("meta/0.papgt", vanilla_papgt.read_bytes())
                logger.info("Restored vanilla PAPGT from backup (size matches snapshot)")
            else:
                # Backup is stale or missing — rebuild with only vanilla directories.
                # Feed vanilla PAMT data so all hashes are correct.
                if vanilla_papgt.exists() and snap_papgt:
                    logger.info("PAPGT backup stale (size %d != snapshot %d), rebuilding",
                                vanilla_papgt.stat().st_size, snap_papgt[0])
                papgt_mgr = PapgtManager(self._game_dir, self._vanilla_dir)
                vanilla_pamts: dict[str, bytes] = {}
                # Read all vanilla PAMTs from backed up or game files
                for d in sorted(self._game_dir.iterdir()):
                    if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                        continue
                    if int(d.name) >= 36:
                        continue  # skip mod directories
                    pamt_path = f"{d.name}/0.pamt"
                    pamt_bytes = self._get_vanilla_bytes(pamt_path)
                    if pamt_bytes:
                        vanilla_pamts[d.name] = pamt_bytes
                try:
                    # exclude_dirs: the queued-for-deletion dirs are
                    # still on disk at this point (deletion deferred
                    # until post-commit), so without the exclusion the
                    # disk scan would re-add them to the index.
                    papgt_bytes = papgt_mgr.rebuild(
                        modified_pamts=vanilla_pamts if vanilla_pamts else None,
                        exclude_dirs={
                            d.name for d in revert_dir_deletions})
                    txn.stage_file("meta/0.papgt", papgt_bytes)
                    logger.info("Rebuilt vanilla PAPGT for revert (%d dirs)",
                                len(vanilla_pamts))
                except FileNotFoundError:
                    pass

            self.progress_updated.emit(95, "Committing revert...")
            txn.commit()

            # Deferred destructive deletions, post-commit only (C1).
            import shutil as _shutil
            for d in revert_dir_deletions:
                _shutil.rmtree(d, ignore_errors=True)
                logger.info(
                    "Post-commit revert cleanup removed %s", d.name)

            if failed_files:
                self.warning.emit(
                    f"{len(failed_files)} file(s) could not be reverted "
                    f"(no backup found). Use Steam 'Verify Integrity' to "
                    f"fully restore: {', '.join(failed_files[:5])}"
                    + (f" (+{len(failed_files)-5} more)" if len(failed_files) > 5 else ""))

            self.progress_updated.emit(100, "Revert complete!")
            self.finished.emit()

        except Exception:
            txn.cleanup_staging()
            raise
        finally:
            txn.cleanup_staging()

    def _get_vanilla_bytes(self, file_path: str) -> bytes | None:
        """Get vanilla version from full backup or range backup.

        GitHub #67 (Doleun, 2026-05-03): when the vanilla backup is
        missing (e.g. large PAZ file skipped by _refresh_vanilla_backups,
        or a newly-vanilla path after a game patch), fall back to the
        live game file IF its hash matches the snapshot. That means the
        file is already vanilla on disk — restoring is a no-op for the
        bytes but the path returns successfully so revert doesn't error
        with 'no backup found'.
        """
        full_path = self._vanilla_dir / file_path.replace("/", os.sep)
        if full_path.exists():
            return full_path.read_bytes()

        game_path = self._game_dir / file_path.replace("/", os.sep)
        if not game_path.exists():
            return None

        range_entries = _load_range_backup(self._vanilla_dir, file_path)
        if range_entries:
            buf = bytearray(game_path.read_bytes())
            _apply_ranges_to_buf(buf, range_entries)
            return bytes(buf)

        # Snapshot-hash fallback: if the live file matches the snapshot,
        # it IS vanilla. Return its bytes so revert treats this path
        # as already-clean instead of failing.
        try:
            from cdumm.engine.snapshot_manager import hash_matches
            row = self._db.connection.execute(
                "SELECT file_hash FROM snapshots WHERE file_path = ?",
                (file_path,)).fetchone()
            if row and row[0] and hash_matches(game_path, row[0]):
                return game_path.read_bytes()
        except Exception as e:
            logger.debug(
                "snapshot-hash fallback failed for %s: %s", file_path, e)

        return None
