"""D2: group cog variants by 'Category / Variant' prefix for collapsible sections.

When a mega-pack flattens to 40+ variants all in one radio group, a
flat list is unreadable. Variants carry labels like 'Abyss Gears /
AbyssGear_1' — parse the prefix so the cog can render a collapsible
section per category.
"""
from __future__ import annotations

from cdumm.gui.components.config_panel import (
    _group_variants_by_category_prefix,
    _strip_category_prefix,
)


def _v(label: str, enabled: bool = False) -> dict:
    return {"label": label, "enabled": enabled}


def test_group_by_prefix_returns_map():
    variants = [
        _v("Abyss Gears / AbyssGear_1"),
        _v("Abyss Gears / AbyssGear_2"),
        _v("Armors / AllArmor_1"),
        _v("Armors / AllBackpacks_1"),
        _v("Weapons / Ammo_1"),
    ]
    groups = _group_variants_by_category_prefix(variants)
    assert groups is not None
    assert set(groups.keys()) == {"Abyss Gears", "Armors", "Weapons"}
    assert groups["Abyss Gears"] == [0, 1]
    assert groups["Armors"] == [2, 3]
    assert groups["Weapons"] == [4]


def test_group_none_when_only_one_category():
    variants = [
        _v("Abyss Gears / AbyssGear_1"),
        _v("Abyss Gears / AbyssGear_2"),
    ]
    # Only one category doesn't warrant collapsible sections.
    assert _group_variants_by_category_prefix(variants) is None


def test_group_none_when_some_variants_lack_prefix():
    """A mix of prefixed and bare labels = not a clean category set."""
    variants = [
        _v("Abyss Gears / AbyssGear_1"),
        _v("Loose_Variant"),     # no prefix
        _v("Armors / AllArmor_1"),
    ]
    assert _group_variants_by_category_prefix(variants) is None


def test_group_trims_whitespace_in_category_name():
    variants = [
        _v("Abyss Gears  /  AbyssGear_1"),
        _v("Armors  /  AllArmor_1"),
    ]
    groups = _group_variants_by_category_prefix(variants)
    assert groups is not None
    assert "Abyss Gears" in groups
    assert "Armors" in groups


def test_strip_prefix_returns_right_side():
    assert _strip_category_prefix("Abyss Gears / AbyssGear_1") == "AbyssGear_1"
    assert _strip_category_prefix("Armors / AllArmor_1") == "AllArmor_1"


def test_strip_prefix_passthrough_without_slash():
    """Labels that don't fit the 'X / Y' shape get returned unchanged."""
    assert _strip_category_prefix("SimpleVariant") == "SimpleVariant"
    assert _strip_category_prefix("Has/slash/no/spaces") == "Has/slash/no/spaces"
