"""Preset CRUD: import a .ini from anywhere, delete safely (Recycle Bin),
and merge two presets (main + additions overlay).

All engine-level operations; no Qt.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cdumm.engine.reshade_preset_ops import (
    MergeResult,
    filter_visible_presets,
    import_preset_file,
    merge_into_main,
    read_preset_for_merge,
    relative_to_base,
    write_preset_sections,
)


# ---- Import --------------------------------------------------------------

def test_import_copies_preset_into_base_path(tmp_path: Path) -> None:
    src = tmp_path / "downloaded.ini"
    src.write_text("Techniques=Bloom\n[Bloom.fx]\nThreshold=0.5\n")
    base = tmp_path / "base"
    base.mkdir()

    result = import_preset_file(src, base)

    assert result.exists()
    assert result.parent == base
    assert result.read_text() == src.read_text()


def test_import_rejects_non_preset_ini(tmp_path: Path) -> None:
    """ReShade.ini-style config file with no [*.fx] / Techniques= is NOT a preset."""
    src = tmp_path / "looks_like_config.ini"
    src.write_text("[GENERAL]\nPresetPath=x.ini\n")
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(ValueError, match="not look like a ReShade preset"):
        import_preset_file(src, base)


def test_import_rejects_non_ini_extension(tmp_path: Path) -> None:
    src = tmp_path / "notapreset.txt"
    src.write_text("Techniques=Bloom\n[Bloom.fx]\nx=1\n")
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(ValueError, match=r"\.ini"):
        import_preset_file(src, base)


def test_import_refuses_overwrite_without_flag(tmp_path: Path) -> None:
    src = tmp_path / "new.ini"
    src.write_text("Techniques=X\n[X.fx]\na=1\n")
    base = tmp_path / "base"
    base.mkdir()
    (base / "new.ini").write_text("existing")

    with pytest.raises(FileExistsError):
        import_preset_file(src, base)


def test_import_overwrites_when_flag_set(tmp_path: Path) -> None:
    src = tmp_path / "new.ini"
    src.write_text("Techniques=X\n[X.fx]\na=1\n")
    base = tmp_path / "base"
    base.mkdir()
    (base / "new.ini").write_text("existing")

    result = import_preset_file(src, base, overwrite=True)
    assert result.read_text() == src.read_text()


def test_import_creates_base_if_missing(tmp_path: Path) -> None:
    src = tmp_path / "preset.ini"
    src.write_text("Techniques=X\n[X.fx]\na=1\n")
    base = tmp_path / "new_base"

    result = import_preset_file(src, base)
    assert result.exists()
    assert base.is_dir()


def test_import_rejects_reshade_ini_as_reserved_name(tmp_path: Path) -> None:
    """Importing a file named ReShade.ini would overwrite the user's config.
    Refuse it regardless of the overwrite flag."""
    src = tmp_path / "ReShade.ini"
    src.write_text("Techniques=X\n[X.fx]\nfoo=1\n")
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(ValueError, match="reserved"):
        import_preset_file(src, base)


def test_import_rejects_reshade_ini_case_insensitive(tmp_path: Path) -> None:
    """`reshade.ini` (lowercase) is the same file on Windows -- reject it."""
    src = tmp_path / "reshade.ini"
    src.write_text("Techniques=X\n[X.fx]\nfoo=1\n")
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(ValueError, match="reserved"):
        import_preset_file(src, base)


# ---- Hide (soft-delete; filter from CDUMM view only) ---------------------

def test_filter_visible_returns_all_when_nothing_hidden(tmp_path: Path) -> None:
    """Empty hidden set -> every preset is visible."""
    presets = [tmp_path / "a.ini", tmp_path / "b.ini"]
    assert filter_visible_presets(presets, hidden=set()) == presets


def test_filter_visible_excludes_hidden_paths(tmp_path: Path) -> None:
    """Hidden paths are removed, order preserved for the rest."""
    a = tmp_path / "a.ini"
    b = tmp_path / "b.ini"
    c = tmp_path / "c.ini"
    result = filter_visible_presets([a, b, c], hidden={str(b)})
    assert result == [a, c]


def test_filter_visible_handles_case_insensitive_paths(tmp_path: Path) -> None:
    """Windows: hidden path stored as 'C:/Foo/a.ini' should match 'c:/foo/a.ini'."""
    preset = tmp_path / "a.ini"
    hidden = {str(preset).upper()}
    result = filter_visible_presets([preset], hidden=hidden)
    assert result == []


def test_filter_visible_ignores_stale_hidden_entries(tmp_path: Path) -> None:
    """If a hidden path no longer matches any preset, it's silently ignored.
    (The preset was deleted elsewhere; stale Config entry shouldn't crash.)"""
    a = tmp_path / "a.ini"
    result = filter_visible_presets([a], hidden={str(tmp_path / "gone.ini")})
    assert result == [a]


def test_filter_visible_matches_relative_hidden_paths(tmp_path: Path) -> None:
    """New format: hidden entries stored relative to base_path survive
    moving the game directory."""
    base = tmp_path / "presets"
    base.mkdir()
    preset = base / "Cinematic.ini"
    preset.write_text("x")
    # Relative-to-base hidden entry.
    hidden = {"Cinematic.ini"}
    result = filter_visible_presets([preset], hidden, base_path=base)
    assert result == []


def test_filter_visible_matches_relative_hidden_path_in_subfolder(tmp_path: Path) -> None:
    """Hidden entry for a preset in a subfolder of base_path."""
    base = tmp_path / "presets"
    base.mkdir()
    subdir = base / "Cinematic"
    subdir.mkdir()
    preset = subdir / "Cinematic.ini"
    preset.write_text("x")
    hidden = {"Cinematic/Cinematic.ini"}
    result = filter_visible_presets([preset], hidden, base_path=base)
    assert result == []


def test_filter_visible_absolute_and_relative_hidden_entries_coexist(tmp_path: Path) -> None:
    """Legacy absolute entries still work alongside new relative entries."""
    base = tmp_path / "presets"
    base.mkdir()
    a = base / "A.ini"
    a.write_text("x")
    b = base / "B.ini"
    b.write_text("x")
    hidden = {"A.ini", str(b)}  # relative + absolute
    result = filter_visible_presets([a, b], hidden, base_path=base)
    assert result == []


def test_relative_to_base_inside_base(tmp_path: Path) -> None:
    base = tmp_path / "presets"
    base.mkdir()
    preset = base / "sub" / "foo.ini"
    preset.parent.mkdir()
    preset.write_text("x")
    assert relative_to_base(preset, base) == "sub/foo.ini"


def test_relative_to_base_outside_returns_absolute(tmp_path: Path) -> None:
    """Preset lives outside base_path -> use absolute path (fail-safe)."""
    base = tmp_path / "presets"
    base.mkdir()
    outside = tmp_path / "somewhere_else" / "external.ini"
    outside.parent.mkdir()
    outside.write_text("x")
    result = relative_to_base(outside, base)
    # Not a simple relative name; the absolute path is returned.
    assert "external.ini" in result
    assert ":" in result or result.startswith("/")


# ---- Merge: reading ------------------------------------------------------

def test_read_preset_for_merge_returns_section_dict(tmp_path: Path) -> None:
    """Preserves ORIGINAL KEY CASE. ReShade writes Threshold / Intensity in
    PascalCase; if we lowercased we'd produce ugly files."""
    preset = tmp_path / "cinematic.ini"
    preset.write_text(
        "[GENERAL]\nTechniques=Bloom,SMAA\n"
        "[Bloom.fx]\nThreshold=0.5\n"
        "[SMAA.fx]\nQuality=2\n"
    )
    sections = read_preset_for_merge(preset)
    assert "Bloom.fx" in sections
    assert sections["Bloom.fx"]["Threshold"] == "0.5"
    assert "SMAA.fx" in sections


def test_read_preset_for_merge_handles_reshade_preamble_layout(tmp_path: Path) -> None:
    """ReShade's real presets start with keys BEFORE any [section] header.
    These preamble keys must be captured, not silently dropped -- otherwise
    merging produces an empty file."""
    preset = tmp_path / "real_reshade.ini"
    # Mirror the actual user preset from the field report:
    preset.write_text(
        "KeyBorder@Border.fx=191,0,0,0\n"
        "PreprocessorDefinitions=INFINITE_BOUNCES=1\n"
        "Techniques=Bloom@Bloom.fx,SMAA@SMAA.fx\n"
        "\n"
        "[Bloom.fx]\nThreshold=0.5\n"
    )
    sections = read_preset_for_merge(preset)
    # Preamble is preserved under the synthetic section.
    assert "__preamble__" in sections
    assert sections["__preamble__"]["Techniques"] == "Bloom@Bloom.fx,SMAA@SMAA.fx"
    assert sections["__preamble__"]["KeyBorder@Border.fx"] == "191,0,0,0"
    # Regular sections still work.
    assert "Bloom.fx" in sections


def test_write_preset_emits_preamble_without_section_header(tmp_path: Path) -> None:
    """When a preset has a __preamble__ section (from ReShade's top-level
    keys layout), write it back as BARE KEYS at the top — not under a
    [__preamble__] header which would confuse ReShade."""
    from cdumm.engine.reshade_preset_ops import _PREAMBLE_SECTION
    output = tmp_path / "out.ini"
    sections = {
        _PREAMBLE_SECTION: {"Techniques": "Bloom@Bloom.fx", "Extra": "1"},
        "Bloom.fx": {"Threshold": "0.5"},
    }
    write_preset_sections(output, sections)

    content = output.read_text(encoding="utf-8")
    # The synthetic section name must NEVER appear in the output.
    assert "__preamble__" not in content
    # The keys appear at the top.
    assert content.startswith("Techniques=Bloom@Bloom.fx\n") or \
        content.startswith("Techniques=Bloom@Bloom.fx\nExtra=1\n")
    assert "[Bloom.fx]" in content
    # The written file is recognizable as a preset by our own check.
    from cdumm.engine.reshade_detect import _is_preset_file
    assert _is_preset_file(output)


def test_merge_of_reshade_real_format_produces_valid_preset(tmp_path: Path) -> None:
    """End-to-end: two ReShade-style presets (top-level preamble keys plus
    [*.fx] sections) merge into a non-empty file that our own detection
    accepts as a preset."""
    a = tmp_path / "A.ini"
    b = tmp_path / "B.ini"
    a.write_text(
        "Techniques=Bloom@Bloom.fx\n"
        "PreprocessorDefinitions=X=1\n"
        "[Bloom.fx]\nThreshold=0.3\n"
    )
    b.write_text(
        "Techniques=HDR@HDR.fx\n"
        "[HDR.fx]\nExposure=2.0\n"
        "[DOF.fx]\nNearPlane=5\n"
    )

    main = read_preset_for_merge(a)
    other = read_preset_for_merge(b)
    assert main and other  # neither parsed empty

    result = merge_into_main(main, other, sections_to_take=["HDR.fx", "DOF.fx"])
    output = tmp_path / "merged.ini"
    write_preset_sections(output, result.sections)

    assert output.exists()
    assert output.stat().st_size > 0
    from cdumm.engine.reshade_detect import _is_preset_file
    assert _is_preset_file(output), output.read_text()
    # Merged content has both main's preamble + added sections.
    content = output.read_text(encoding="utf-8")
    assert "Techniques=Bloom@Bloom.fx" in content
    assert "[HDR.fx]" in content
    assert "[DOF.fx]" in content
    assert "[Bloom.fx]" in content


# ---- Merge: logic --------------------------------------------------------

def _example_main() -> dict[str, dict[str, str]]:
    return {
        "Bloom.fx": {"Threshold": "0.3"},
        "SMAA.fx": {"Quality": "1"},
    }


def _example_other() -> dict[str, dict[str, str]]:
    return {
        "Bloom.fx": {"Threshold": "0.9"},  # conflicts with main
        "HDR.fx": {"Intensity": "2.0"},    # new section, no conflict
        "DOF.fx": {"NearPlane": "5"},      # new section, no conflict
    }


def test_merge_empty_selection_returns_main_unchanged() -> None:
    """User picks no sections from the other preset -> main stays as-is."""
    main = _example_main()
    other = _example_other()
    result = merge_into_main(main, other, sections_to_take=[])

    assert result.sections == main
    assert result.overwrote == []
    assert result.added == []


def test_merge_single_conflict_section_overwrites(tmp_path: Path) -> None:
    main = _example_main()
    other = _example_other()
    result = merge_into_main(main, other, sections_to_take=["Bloom.fx"])

    assert result.sections["Bloom.fx"]["Threshold"] == "0.9"  # from other
    assert result.sections["SMAA.fx"] == main["SMAA.fx"]  # untouched
    assert result.overwrote == ["Bloom.fx"]
    assert result.added == []


def test_merge_single_new_section_adds() -> None:
    main = _example_main()
    other = _example_other()
    result = merge_into_main(main, other, sections_to_take=["HDR.fx"])

    assert result.sections["HDR.fx"]["Intensity"] == "2.0"
    # Main's sections still present.
    assert "Bloom.fx" in result.sections
    assert result.overwrote == []
    assert result.added == ["HDR.fx"]


def test_merge_mixed_conflict_and_new() -> None:
    main = _example_main()
    other = _example_other()
    result = merge_into_main(main, other,
                             sections_to_take=["Bloom.fx", "HDR.fx"])

    assert result.sections["Bloom.fx"]["Threshold"] == "0.9"
    assert result.sections["HDR.fx"]["Intensity"] == "2.0"
    assert set(result.overwrote) == {"Bloom.fx"}
    assert set(result.added) == {"HDR.fx"}


def test_merge_ignores_unknown_section_in_to_take() -> None:
    """Defensive: caller passes a section name that's not in `other`.
    Should be silently skipped (shouldn't crash)."""
    main = _example_main()
    other = _example_other()
    result = merge_into_main(main, other,
                             sections_to_take=["NoSuchShader.fx"])
    assert result.sections == main
    assert result.overwrote == []
    assert result.added == []


def test_merge_preserves_main_only_sections() -> None:
    """Sections that exist only in main (not in other) are always kept."""
    main = {"OnlyInMain.fx": {"foo": "1"}}
    other = {"New.fx": {"bar": "2"}}
    result = merge_into_main(main, other, sections_to_take=["New.fx"])

    assert "OnlyInMain.fx" in result.sections
    assert "New.fx" in result.sections


# ---- Merge: writing ------------------------------------------------------

def test_write_preset_sections_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "merged.ini"
    sections = {
        "Bloom.fx": {"Threshold": "0.7", "Intensity": "1.5"},
        "HDR.fx": {"Exposure": "2.0"},
    }
    write_preset_sections(out, sections)

    roundtrip = read_preset_for_merge(out)
    assert roundtrip["Bloom.fx"]["Threshold"] == "0.7"
    assert roundtrip["Bloom.fx"]["Intensity"] == "1.5"
    assert roundtrip["HDR.fx"]["Exposure"] == "2.0"


def test_write_preset_sections_output_is_a_preset(tmp_path: Path) -> None:
    """The written file must pass our own is-a-preset check so it shows up
    in the preset list after write."""
    from cdumm.engine.reshade_detect import _is_preset_file
    out = tmp_path / "merged.ini"
    sections = {"Bloom.fx": {"Threshold": "0.5"}}
    write_preset_sections(out, sections)

    assert _is_preset_file(out)


# ---- MergeResult shape ---------------------------------------------------

def test_mergeresult_contains_expected_fields() -> None:
    r = MergeResult(
        sections={"X.fx": {"a": "1"}},
        added=["X.fx"],
        overwrote=[],
    )
    assert r.sections == {"X.fx": {"a": "1"}}
    assert r.added == ["X.fx"]
    assert r.overwrote == []
