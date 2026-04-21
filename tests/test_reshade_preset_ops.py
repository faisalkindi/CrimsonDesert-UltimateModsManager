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
    delete_preset,
    import_preset_file,
    merge_into_main,
    read_preset_for_merge,
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


# ---- Delete --------------------------------------------------------------

def test_delete_uses_send2trash(tmp_path: Path) -> None:
    """Verify delete goes through send2trash (Recycle Bin) -- users can recover."""
    preset = tmp_path / "preset.ini"
    preset.write_text("Techniques=X\n[X.fx]\na=1\n")

    with patch("cdumm.engine.reshade_preset_ops.send2trash") as mock_send:
        delete_preset(preset)
    mock_send.assert_called_once_with(str(preset))


def test_delete_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        delete_preset(tmp_path / "nonexistent.ini")


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
