"""Skill Format 3 list-of-dict field writer.

Uses the vendored skill parser at
`src/cdumm/_vendor/skillinfo_parser.py`. That parser file is
distributed under MPL-2.0 (see
`src/cdumm/_vendor/skillinfo_parser_LICENSE_MPL2`).

Whole-table approach (mirrors iteminfo_writer): the parser is
already verified byte-roundtrip on vanilla 1.0.0.4 skill.pabgb,
so we parse vanilla, mutate target entries' list fields, serialize,
and emit a single offset=0 change.

Bug from timuela on GitHub #41 (focus_aerial_roll skill mod):
Format 3 mods targeting skill.pabgb with `_useResourceStatList`
were skipped at validation time.
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from cdumm.engine.format3_handler import Format3Intent

logger = logging.getLogger(__name__)

_DEV_VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor"
_cached_module: Any | None = None
_load_attempted = False


# Skill list-of-dict fields the vendored parser exposes. Mirrors
# the keys produced by `parse_skill_entry`.
SUPPORTED_FIELDS = {
    "_useResourceStatList",
    "_buffLevelList",
}


def _shape_ok(field: str, new) -> bool:
    """Per-intent shape gate before dict assignment (audit 2026-06-11).

    The vendored serializer iterates these lists with dict-key access;
    a list of ints (or a bare int) raises deep inside serialize_all,
    which kills the WHOLE multi-mod batch on this table. On-disk
    shapes: _useResourceStatList is a list of resource-stat dicts;
    _buffLevelList is a list of levels, each level a list of buff-data
    dicts.
    """
    if field == "_useResourceStatList":
        return (isinstance(new, list)
                and all(isinstance(x, dict) for x in new))
    if field == "_buffLevelList":
        return (isinstance(new, list)
                and all(isinstance(lvl, list)
                        and all(isinstance(bd, dict) for bd in lvl)
                        for lvl in new))
    return True


def _candidate_dirs() -> list[Path]:
    out: list[Path] = [_DEV_VENDOR_DIR]
    if hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        out.append(meipass / "cdumm" / "_vendor")
        out.append(meipass / "_vendor")
    return out


def _get_parser():
    """Load skillinfo_parser from the vendor dir, dev or frozen."""
    global _cached_module, _load_attempted
    if _load_attempted:
        return _cached_module
    _load_attempted = True
    for candidate in _candidate_dirs():
        if not candidate.exists():
            continue
        try:
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            import skillinfo_parser as _mod
            _cached_module = _mod
            logger.info("skillinfo_parser loaded from %s", candidate)
            return _cached_module
        except Exception as e:
            logger.debug(
                "skill parser load attempt at %s failed: %s", candidate, e)
            continue
    logger.warning("skill parser not loadable; skill list writer unavailable")
    return None


def build_skill_intent_change(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: "list[Format3Intent]",
) -> Optional[dict]:
    """Apply Format 3 intents to skill.pabgb and emit a single
    offset=0 v2 change.

    Needs both .pabgb body AND .pabgh header (the parser requires
    the index to walk records). Returns None when no intents
    applied.
    """
    parser = _get_parser()
    if parser is None:
        return None
    try:
        entries = parser.parse_all(vanilla_header, vanilla_body)
    except Exception as e:
        # GitHub #182 (CD 1.09): if the iteminfo schema shifted, the
        # skill schema may have too. Surface the version context in
        # the log so users do not waste time debugging their mod
        # when the actual cause is a game-side layout change.
        logger.error(
            "skill parse failed (%s). If you are on Crimson Desert "
            "1.09 this may be related to the iteminfo schema shift "
            "tracked under GitHub #182. Format 3 list-of-dict intents "
            "on skill will be skipped until the parser catches up. "
            "Format 2 / offset-based byte patches still apply.",
            e, exc_info=True)
        return None

    # Round-trip pre-flight: re-serialize the UNMODIFIED parse and
    # require the body to reproduce vanilla byte-for-byte before
    # mutating anything (audit finding I7, 2026-06-10, the parser
    # ships raw-fallback salvage paths, and a lossy parse on a new
    # game version would otherwise emit a whole-table change that
    # silently rewrites unrelated bytes). The identity index also
    # validates the .pabgh offset-rewrite path used below.
    from cdumm.engine.pabgh_rewrite import rewrite_pabgh_offsets
    try:
        ident_pabgh, ident_pabgb = parser.serialize_all(entries)
    except Exception as e:
        logger.error(
            "skill identity serialize failed (%s); refusing to "
            "write this table", e, exc_info=True)
        return None
    if ident_pabgb != vanilla_body:
        first_diff = next(
            (i for i in range(min(len(ident_pabgb), len(vanilla_body)))
             if ident_pabgb[i] != vanilla_body[i]),
            min(len(ident_pabgb), len(vanilla_body)))
        logger.error(
            "skill round-trip pre-flight FAILED: identity serialize "
            "differs from vanilla at byte %d (vanilla %d bytes, "
            "serialized %d bytes). The parser does not model this "
            "game version's layout; refusing to emit a whole-table "
            "change.", first_diff, len(vanilla_body), len(ident_pabgb))
        return None
    ident_offsets = _offsets_from_synth_pabgh(ident_pabgh)
    header_rewritable = (
        ident_offsets is not None
        and rewrite_pabgh_offsets(
            vanilla_header, "skill", ident_offsets) == vanilla_header)
    if not header_rewritable:
        logger.warning(
            "skill .pabgh pre-flight failed: identity offsets do not "
            "reproduce the vanilla index. Size-changing skill edits "
            "will be refused; same-size edits still apply.")

    by_key = {e["key"]: e for e in entries}
    # Format 3 dialect contract: "lookup by entry name first, key as
    # fallback". Key-omitted intents arrive with the sentinel key=0,
    # so resolve through the entry's name when the numeric key misses
    # (mirrors the multichangeinfo/characterinfo writers).
    by_name: dict = {}
    for e in entries:
        nm = e.get("name")
        if isinstance(nm, str) and nm:
            by_name.setdefault(nm, e)
    applied = 0
    name_resolved = 0
    skipped_op = 0
    skipped_key = 0
    skipped_field = 0
    skipped_shape = 0
    for intent in intents:
        target_entry = by_key.get(intent.key)
        if target_entry is None and intent.entry:
            target_entry = by_name.get(intent.entry)
            if target_entry is not None:
                name_resolved += 1
                logger.debug(
                    "skill writer: intent key %r missed, resolved by "
                    "entry name %r (key=%d)",
                    intent.key, intent.entry, target_entry.get("key"))
        if target_entry is None:
            skipped_key += 1
            logger.debug(
                "skill writer: key %d / entry %r not in table, "
                "skipping", intent.key, intent.entry)
            continue
        if intent.field not in SUPPORTED_FIELDS:
            skipped_field += 1
            logger.warning(
                "skill writer: field %r not supported (only %s); "
                "intent on key=%d dropped",
                intent.field, ", ".join(sorted(SUPPORTED_FIELDS)),
                intent.key)
            continue
        if intent.op != "set":
            skipped_op += 1
            logger.warning(
                "skill writer: op %r not supported (only 'set'); "
                "intent on key=%d field=%r dropped",
                intent.op, intent.key, intent.field)
            continue
        if not _shape_ok(intent.field, intent.new):
            skipped_shape += 1
            logger.warning(
                "skill writer: intent on key=%d field=%r carries a "
                "new value whose shape (%s) does not match the field; "
                "skipping intent instead of letting serialization "
                "fail later", intent.key, intent.field,
                type(intent.new).__name__)
            continue
        try:
            target_entry[intent.field] = intent.new
            applied += 1
        except Exception as e:
            logger.warning(
                "skill writer: applying intent on key=%d field=%r "
                "failed: %s", intent.key, intent.field, e)

    if name_resolved:
        logger.info(
            "skill writer: %d intent(s) resolved by entry name "
            "(key missing or not in table)", name_resolved)
    if applied == 0:
        skip_total = (skipped_op + skipped_key + skipped_field
                      + skipped_shape)
        if skip_total:
            logger.warning(
                "skill writer: 0 of %d intent(s) applied "
                "(%d non-'set' op, %d unknown key, %d unknown field, "
                "%d bad value shape). No change emitted.",
                skip_total, skipped_op, skipped_key, skipped_field,
                skipped_shape)
        return None

    try:
        new_synth_pabgh, new_pabgb = parser.serialize_all(entries)
    except Exception as e:
        logger.error("skill serialize failed: %s", e, exc_info=True)
        return None

    if new_pabgb == vanilla_body:
        return None

    # Companion .pabgh rebuild (audit finding A): the parser hands
    # back a synthesized index whose offsets are authoritative for
    # the rebuilt body, but whose container layout may not match
    # what the game shipped. Extract its key->offset map and rewrite
    # those offsets surgically into a copy of the vanilla header.
    pabgh_companion = None
    new_offsets = _offsets_from_synth_pabgh(new_synth_pabgh)
    if new_offsets is not None and new_offsets != ident_offsets:
        if not header_rewritable:
            logger.error(
                "skill: record offsets shifted (%d bytes size delta) "
                "but the .pabgh pre-flight failed. Refusing the whole "
                "change; shipping the table alone would leave the "
                "index pointing at stale offsets.",
                len(new_pabgb) - len(vanilla_body))
            return None
        new_header = rewrite_pabgh_offsets(
            vanilla_header, "skill", new_offsets)
        if new_header is None:
            logger.error(
                "skill: .pabgh index could not be rewritten after a "
                "size-changing edit; refusing the whole change.")
            return None
        pabgh_companion = {
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": new_header.hex(),
            "label": "skill .pabgh offsets (rebuilt for "
                     "size-changed records)",
        }

    skip_total = skipped_op + skipped_key + skipped_field + skipped_shape
    if skip_total:
        skip_summary_parts = []
        if skipped_op:
            skip_summary_parts.append(f"{skipped_op} non-'set' op")
        if skipped_key:
            skip_summary_parts.append(f"{skipped_key} unknown key")
        if skipped_field:
            skip_summary_parts.append(f"{skipped_field} unknown field")
        if skipped_shape:
            skip_summary_parts.append(f"{skipped_shape} bad value shape")
        skip_summary = ", ".join(skip_summary_parts)
        label = (
            f"skill Format 3 intents ({applied} applied, "
            f"{skip_total} skipped: {skip_summary})"
        )
    else:
        label = f"skill Format 3 intents ({applied} applied)"

    change = {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_pabgb.hex(),
        "label": label,
    }
    if pabgh_companion is not None:
        change["_pabgh_companion"] = pabgh_companion
    return change


def _offsets_from_synth_pabgh(synth: bytes) -> Optional[dict]:
    """Extract key -> offset from the parser-synthesized index
    (u16 count + count x (u32 key, u32 offset), the exact layout
    ``skillinfo_parser.serialize_all`` emits)."""
    import struct
    if len(synth) < 2:
        return None
    count = struct.unpack_from("<H", synth, 0)[0]
    if len(synth) < 2 + count * 8:
        return None
    out: dict = {}
    pos = 2
    for _ in range(count):
        key, off = struct.unpack_from("<II", synth, pos)
        out[key] = off
        pos += 8
    return out
