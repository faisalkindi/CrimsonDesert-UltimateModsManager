"""scottykyzer Nexus 2026-05-09 (CDUMM v3.2.13): "There is no
select/deselect all, which means you have to scroll in a tiny
panel for days and days clicking and clicking."

Mods like Simple BackPack Visual Swap ship dozens of independent
toggles. The side panel renders them as a flat checkbox list. With
no bulk action, enabling or disabling the whole mod is N clicks.

Add a Select-All / Deselect-All bar at the top of the patch
section. Clicking should toggle every INDEPENDENT patch. Patches
that are part of a preset family (mutex radio group) must NOT be
flipped by Select-All -- they're an exclusive choice and forcing
all of them on is meaningless. Always-on toggles outside the
family stay in scope.
"""
from __future__ import annotations


def test_select_all_indices_covers_independent_toggles_only():
    from cdumm.gui.components.config_panel import (
        compute_bulk_toggle_indices,
    )

    # 7 patches: 2 always-on + 5 mutex variants (preset family).
    total_indices = list(range(7))
    preset_groups = {
        "Variant A": [2],
        "Variant B": [3],
        "Variant C": [4],
        "Variant D": [5],
        "Variant E": [6],
    }
    always_on = [0, 1]

    select = compute_bulk_toggle_indices(
        total_indices, preset_groups, always_on, target=True)
    assert sorted(select) == [0, 1], (
        f"Select-All should only flip the 2 always-on toggles, "
        f"never the 5 mutex variants (preset family); got: "
        f"{sorted(select)}")


def test_deselect_all_indices_also_covers_independents_only():
    from cdumm.gui.components.config_panel import (
        compute_bulk_toggle_indices,
    )

    total_indices = list(range(7))
    preset_groups = {"X": [2, 3, 4, 5, 6]}
    always_on = [0, 1]

    deselect = compute_bulk_toggle_indices(
        total_indices, preset_groups, always_on, target=False)
    assert sorted(deselect) == [0, 1]


def test_no_preset_family_means_every_patch_flips():
    from cdumm.gui.components.config_panel import (
        compute_bulk_toggle_indices,
    )

    total_indices = list(range(5))
    select = compute_bulk_toggle_indices(
        total_indices, preset_groups=None,
        always_on=[], target=True)
    assert sorted(select) == [0, 1, 2, 3, 4]
