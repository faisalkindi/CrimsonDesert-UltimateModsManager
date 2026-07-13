"""Tests for detect_preset_groups in config_panel."""
from cdumm.gui.components.config_panel import detect_preset_groups

from tests.fixture_loaders import MOD_FIXTURE_HOWTO, real_mod_fixture


def _patches(*labels):
    return [{"label": l} for l in labels]


def test_returns_none_when_single_tag():
    p = _patches("[0%] foo", "[0%] bar")
    assert detect_preset_groups(p) is None


def test_returns_none_when_no_tags():
    p = _patches("foo bar", "baz qux")
    assert detect_preset_groups(p) is None


def test_returns_none_when_mixed_tagged_and_untagged():
    p = _patches("[0%] foo", "untagged bar")
    assert detect_preset_groups(p) is None


def test_detects_percent_presets():
    p = _patches(
        "[0%] foo", "[0%] bar",
        "[25%] foo", "[25%] bar",
        "[100%] foo", "[100%] bar",
    )
    groups = detect_preset_groups(p)
    assert groups is not None
    assert set(groups) == {"0%", "25%", "100%"}
    assert groups["0%"] == [0, 1]
    assert groups["25%"] == [2, 3]
    assert groups["100%"] == [4, 5]


def test_detects_known_vocab_presets():
    p = _patches(
        "[Off] foo",
        "[On] foo",
    )
    assert detect_preset_groups(p) is not None


def test_detects_equal_count_arbitrary_names():
    p = _patches(
        "[Lazy Run] a", "[Lazy Run] b", "[Lazy Run] c",
        "[Marathon] a", "[Marathon] b", "[Marathon] c",
        "[Sprint] a", "[Sprint] b", "[Sprint] c",
    )
    groups = detect_preset_groups(p)
    assert groups is not None
    assert set(groups) == {"Lazy Run", "Marathon", "Sprint"}


def test_returns_none_when_unequal_counts_and_arbitrary_names():
    # Without percent or vocab match, unequal counts means not a preset family.
    p = _patches(
        "[CategoryA] a",
        "[CategoryB] a", "[CategoryB] b",
    )
    assert detect_preset_groups(p) is None


def test_real_mod_1103_and_regen():
    """Live integration: Nexus mod 1103 'Stamina/Spirit Adjuster + Regen'
    must be detected as an 11-preset selector with 531 patches per tag.
    Skips if the file isn't on disk (e.g. CI)."""
    import json
    from pathlib import Path
    p = real_mod_fixture("JSON Stamina - Spirit Adjuster And Regen-1103-1-4-1777707454/Stamina Spirit Adjuster + Regen.json")
    if not p.exists():
        import pytest
        pytest.skip("mod 1103 fixture not on disk")
    with p.open(encoding="utf-8") as f:
        d = json.load(f)
    # Flatten: ConfigPanel sees one combined patches list per mod.
    flat = [c for patch in d["patches"] for c in patch["changes"]]
    groups = detect_preset_groups(flat)
    assert groups is not None
    assert len(groups) == 11
    assert all(len(idxs) == 531 for idxs in groups.values())
    # Tags include the documented set.
    assert {"0%", "25%", "50%", "100%"}.issubset(groups.keys())
