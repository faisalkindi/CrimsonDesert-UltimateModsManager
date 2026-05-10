"""scottykyzer Nexus 2026-05-09 (CDUMM v3.2.13): the Conflicts load
order card contradicts itself.

  * Hint reads "Higher rows win. Use the arrows to reorder."
  * Sort puts the HIGHEST priority number at the top.
  * Engine treats LOWER priority number as the winner.

Combined effect: row 1 of the panel is labelled "priority 3" and
loses to row 3 labelled "priority 0", which directly contradicts
"Higher rows win". User report:

    "I have 3 mods that conflict ... CDUMM reports that #3 will
     win over #2, and over #1, and that #2 will win over #1.
     This suggests that LOWER rows with HIGHER row NUMBERS and
     LOWER priorities will win. All of this is backward."

Fix: sort ascending by priority value so the engine winner sits at
the top and "Higher rows win" becomes literally true. The pair of
move-up / move-down arrows keeps decreasing / increasing the
mod's priority number as before, which now matches the visual
direction (move up = closer to winner).
"""
from __future__ import annotations


def test_priority_mods_sorted_winner_first():
    """The displayed order in the load-order card must put the
    engine winner (lowest priority number) at the top."""
    from cdumm.gui.conflicts_dialog import _sort_priority_mods_for_display

    mods_by_id = {
        100: {"priority": 3, "name": "Cloak Resistance Buff"},
        200: {"priority": 2, "name": "Greater Durability"},
        300: {"priority": 0, "name": "Simple BackPack Visual Swap"},
    }
    ordered = _sort_priority_mods_for_display(
        [100, 200, 300], mods_by_id)
    assert ordered == [300, 200, 100], (
        f"engine winner (priority 0) must be at the top of the "
        f"display so 'Higher rows win' is literal; got: "
        f"{[mods_by_id[m]['priority'] for m in ordered]}")


def test_priority_mods_sort_handles_missing_priority():
    """Mods that somehow lack a priority value should sort last
    (treated as priority +infinity, i.e. lowest priority)."""
    from cdumm.gui.conflicts_dialog import _sort_priority_mods_for_display

    mods_by_id = {
        100: {"priority": 1, "name": "A"},
        200: {"name": "B"},  # no priority key
        300: {"priority": 0, "name": "C"},
    }
    ordered = _sort_priority_mods_for_display(
        [100, 200, 300], mods_by_id)
    assert ordered == [300, 100, 200], (
        f"missing-priority mods must sort to the bottom (loser end); "
        f"got id order {ordered}")
