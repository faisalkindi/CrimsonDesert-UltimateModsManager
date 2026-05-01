"""Skill Format 3 list-of-dict field writer.

Uses the vendored NattKh skill parser at
`src/cdumm/_vendor/nattkh_skillinfo_parser.py` (MPL-2.0).

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


# Skill list-of-dict fields that NattKh's parser exposes. Mirrors
# the keys produced by `parse_skill_entry`.
SUPPORTED_FIELDS = {
    "_useResourceStatList",
    "_buffLevelList",
}


def _candidate_dirs() -> list[Path]:
    out: list[Path] = [_DEV_VENDOR_DIR]
    if hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        out.append(meipass / "cdumm" / "_vendor")
        out.append(meipass / "_vendor")
    return out


def _get_parser():
    """Load nattkh_skillinfo_parser from the vendor dir, dev or frozen."""
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
            import nattkh_skillinfo_parser as _mod
            _cached_module = _mod
            logger.info("nattkh_skillinfo_parser loaded from %s", candidate)
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
        logger.error("skill parse failed: %s", e, exc_info=True)
        return None

    by_key = {e["key"]: e for e in entries}
    applied = 0
    skipped_op = 0
    skipped_key = 0
    skipped_field = 0
    for intent in intents:
        if intent.key not in by_key:
            skipped_key += 1
            logger.debug(
                "skill writer: key %d not in table, skipping", intent.key)
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
        try:
            by_key[intent.key][intent.field] = intent.new
            applied += 1
        except Exception as e:
            logger.warning(
                "skill writer: applying intent on key=%d field=%r "
                "failed: %s", intent.key, intent.field, e)

    if applied == 0:
        skip_total = skipped_op + skipped_key + skipped_field
        if skip_total:
            logger.warning(
                "skill writer: 0 of %d intent(s) applied "
                "(%d non-'set' op, %d unknown key, %d unknown field). "
                "No change emitted.",
                skip_total, skipped_op, skipped_key, skipped_field)
        return None

    try:
        _, new_pabgb = parser.serialize_all(entries)
    except Exception as e:
        logger.error("skill serialize failed: %s", e, exc_info=True)
        return None

    if new_pabgb == vanilla_body:
        return None

    skip_total = skipped_op + skipped_key + skipped_field
    if skip_total:
        skip_summary_parts = []
        if skipped_op:
            skip_summary_parts.append(f"{skipped_op} non-'set' op")
        if skipped_key:
            skip_summary_parts.append(f"{skipped_key} unknown key")
        if skipped_field:
            skip_summary_parts.append(f"{skipped_field} unknown field")
        skip_summary = ", ".join(skip_summary_parts)
        label = (
            f"skill Format 3 intents ({applied} applied, "
            f"{skip_total} skipped: {skip_summary})"
        )
    else:
        label = f"skill Format 3 intents ({applied} applied)"

    return {
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_pabgb.hex(),
        "label": label,
    }
