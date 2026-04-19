"""CRITICAL #4: XML overlay must honour CDUMM's 'lower priority wins' convention.

apply_engine.py previously queried XML deltas with `ORDER BY priority ASC`
and passed the priority through unchanged. xml_patch_handler uses JMM
convention ('higher priority wins, executes last'), so CDUMM's priority=1
mods (user intent: WIN) were ending up FIRST in the sort and getting
their patches overwritten by priority=5 mods (user intent: LOSE).

JSON mount-time already uses ORDER BY priority DESC to align with
merge_compiled_mod_files. XML must end up in the same winning order.

The fix: transform CDUMM priority -> xml-handler-expected priority at the
apply_engine call site so the sort in xml_patch_handler puts CDUMM's
lowest priority value LAST (where it wins).
"""
from __future__ import annotations

from cdumm.engine.apply_engine import cdumm_to_xml_priority


def test_lower_cdumm_priority_sorts_last_under_xml_handler():
    """xml_patch_handler sorts items ASC; CDUMM priority=1 (winner) must
    map to the HIGHEST transformed value so it sorts LAST."""
    p1 = cdumm_to_xml_priority(1)
    p5 = cdumm_to_xml_priority(5)
    p10 = cdumm_to_xml_priority(10)

    # After sorting ASC: [p10, p5, p1] — CDUMM=1 runs last (winner).
    transformed = sorted([p5, p1, p10])
    assert transformed[-1] == p1, (
        f"CDUMM priority 1 must land LAST after ASC sort; got {transformed}")


def test_transform_is_monotonic():
    for a, b in [(1, 2), (2, 3), (5, 10), (1, 100)]:
        ta = cdumm_to_xml_priority(a)
        tb = cdumm_to_xml_priority(b)
        assert ta > tb, (
            f"CDUMM a={a} should have higher xml priority than b={b}; "
            f"got transforms {ta} vs {tb}")


def test_equal_priorities_stay_equal():
    assert cdumm_to_xml_priority(3) == cdumm_to_xml_priority(3)
