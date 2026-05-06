"""Phase 3f gap: validator rejects buffinfo nested-item paths.

Found via systematic-debugging sweep on the wiring. The validator's
``_diagnose_unsupported_intent`` rejects ANY field with a dot in
the name as "nested struct sub-field not supported". For buffinfo
that's wrong , the clean-room parser DOES resolve item paths
like ``buff_data_list[0].data.base.id``, but they never reach the
apply path because the validator skips them with a generic message.

User-facing impact: a real mod targeting any item-level field on
buffinfo (e.g. norva2 mod 2276 patching ``buff_data_list[0].data.
base.flags_a``) would import, classify all item intents as
"skipped: nested struct sub-field not supported", and surface a
warning telling the author to flatten , wrong advice for buffinfo.

Fix: special-case buffinfo paths in ``_diagnose_unsupported_intent``.
"""
from __future__ import annotations

from cdumm.engine.format3_handler import (
    Format3Intent, validate_intents,
)


def _intent(field: str) -> Format3Intent:
    return Format3Intent(
        entry="X", key=42, field=field, op="set", new=1)


def test_buffinfo_wrapper_path_supported():
    """Sanity guard , wrapper names already work."""
    res = validate_intents("buffinfo.pabgb", [_intent("min_level")])
    assert len(res.supported) == 1
    assert len(res.skipped) == 0


def test_buffinfo_buff_data_list_index_zero_absent_flag_supported():
    """``buff_data_list[0].absent_flag`` resolves via locate_buff_field
    , validator must accept it instead of skipping as 'nested struct'."""
    res = validate_intents("buffinfo.pabgb", [
        _intent("buff_data_list[0].absent_flag")])
    assert len(res.supported) == 1, (
        f"buffinfo item-path was incorrectly skipped: "
        f"{res.skipped[0][1] if res.skipped else 'no reason'}")


def test_buffinfo_buff_data_list_data_base_path_supported():
    """``buff_data_list[N].data.base.X`` for X in the payload-common
    field set must validate."""
    paths = [
        "buff_data_list[0].data.base.tag",
        "buff_data_list[0].data.base.id",
        "buff_data_list[0].data.base.flags_a",
        "buff_data_list[2].data.base.qword_a",
        "buff_data_list[5].data.base.lookup_88",
    ]
    res = validate_intents(
        "buffinfo.pabgb", [_intent(p) for p in paths])
    assert len(res.supported) == len(paths), (
        f"item paths skipped: "
        f"{[(i.field, r) for i, r in res.skipped]}")


def test_buffinfo_unrelated_dotted_path_still_skipped():
    """Don't blanket-allow dots , a path that doesn't match the
    item-list shape should still be skipped (falling out of the
    apply helper to no-bytes-emitted is fine, but the validator
    shouldn't claim it's supported)."""
    res = validate_intents("buffinfo.pabgb", [
        _intent("totally.unrelated.dotted.thing")])
    assert len(res.skipped) == 1
    assert "nested" in res.skipped[0][1].lower() or \
        "dotted" in res.skipped[0][1].lower() or \
        "not implemented" in res.skipped[0][1].lower()
