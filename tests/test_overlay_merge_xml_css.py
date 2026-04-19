"""HIGH #12: byte-merge on XML/CSS/JSON overlays is unsafe.

Byte-level three-way merge assumes non-overlapping byte ranges mean
'different field in same table'. That's true for .pabgb/.pabgh/.pamt
(fixed-layout tables) but FALSE for text formats:
  * XML: xml_patch_handler already processes structural patches and
    re-serialises. A second byte-merge on top would splice half-tokens.
  * CSS / JSON: format-reflows (minified vs pretty) produce identical
    semantic content but byte-differing outputs, so merge sees phantom
    conflicts and corrupts attribute orders.

The mergeable extension list must be restricted to structured-table
formats only. When XML/CSS/JSON hit this path, fall back to last-wins
(highest-priority entry).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cdumm.engine.apply_engine import ApplyWorker


def _make_worker():
    w = ApplyWorker.__new__(ApplyWorker)
    w._db = MagicMock()
    w._game_dir = MagicMock()
    w._vanilla_dir = MagicMock()
    return w


def test_xml_entries_fall_through_last_wins_not_byte_merge():
    """Two overlay entries for the same .xml target must NOT be byte-merged."""
    w = _make_worker()
    meta_a = {"pamt_dir": "0006", "entry_path": "script/ui.xml"}
    meta_b = {"pamt_dir": "0006", "entry_path": "script/ui.xml"}
    entries = [(b"A_xml_bytes", meta_a), (b"B_xml_bytes", meta_b)]

    result = w._merge_same_target_overlay_entries(entries)
    # Either last-wins (second entry) OR both preserved. Byte-merge would
    # have called _get_vanilla_entry_content — we assert the last entry's
    # bytes survive unchanged (no frankenstein merge).
    merged_bytes = [body for body, _ in result]
    assert b"B_xml_bytes" in merged_bytes, (
        f"highest-priority xml entry must survive intact; got {merged_bytes}")
    # The merged result must NOT be some combined byte-merge artifact.
    for body, _ in result:
        assert body in (b"A_xml_bytes", b"B_xml_bytes"), (
            f"byte-merge produced a frankenstein output: {body!r}")


def test_css_entries_fall_through_last_wins():
    w = _make_worker()
    meta = {"pamt_dir": "0001", "entry_path": "ui/style.css"}
    entries = [(b"body{color:red}", dict(meta)), (b"body{color:blue}", dict(meta))]
    result = w._merge_same_target_overlay_entries(entries)
    bodies = [b for b, _ in result]
    assert b"body{color:blue}" in bodies
    for body, _ in result:
        assert body in (b"body{color:red}", b"body{color:blue}")


def test_pabgb_entries_are_byte_merged_when_possible():
    """Regression: .pabgb remains in the mergeable list (structured table)."""
    from cdumm.engine.apply_engine import ApplyWorker as _AW
    # We don't exercise the actual byte-merge here — just confirm that the
    # _MERGEABLE_EXTS list still contains .pabgb so PABGB overlays don't
    # regress to last-wins.
    import inspect
    src = inspect.getsource(_AW._merge_same_target_overlay_entries)
    assert ".pabgb" in src
    assert ".pamt" in src
    # And that XML/CSS/JSON are NOT in the byte-merge allowlist anymore.
    # (They still get last-wins treatment via the fall-through branch.)
    assert '".xml"' not in src, "XML must not be byte-merged"
    assert '".css"' not in src, "CSS must not be byte-merged"
    assert '".json"' not in src, "JSON must not be byte-merged"
