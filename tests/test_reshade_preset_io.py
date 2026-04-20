"""ReShade preset read/write + supporting helpers.

- path resolution (absolute / relative against BasePath / fallback to bin64)
- raw read of [GENERAL] PresetPath=
- surgical write that preserves comments
- is_game_running() process check
- same_preset() Windows-safe path comparison
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cdumm.engine.reshade_preset import (
    is_game_running,
    read_active_preset,
    read_active_preset_raw,
    read_preset_sections,
    resolve_preset_path,
    same_preset,
    set_active_preset,
)


# ---- resolve_preset_path ---------------------------------------------------

def test_resolve_absolute_path_returned_asis(tmp_path: Path) -> None:
    abs_path = tmp_path / "somewhere" / "preset.ini"
    result = resolve_preset_path(None, tmp_path / "bin64", str(abs_path))
    assert result == abs_path


def test_resolve_relative_uses_base_path_when_set(tmp_path: Path) -> None:
    base = tmp_path / "custom_shaders"
    result = resolve_preset_path(base, tmp_path / "bin64", "foo.ini")
    assert result == base / "foo.ini"


def test_resolve_relative_falls_back_to_bin64_when_no_base(tmp_path: Path) -> None:
    bin64 = tmp_path / "bin64"
    result = resolve_preset_path(None, bin64, "foo.ini")
    assert result == bin64 / "foo.ini"


def test_resolve_relative_with_subdir(tmp_path: Path) -> None:
    base = tmp_path / "shaders"
    result = resolve_preset_path(base, tmp_path / "bin64", "folder/inner.ini")
    assert result == base / "folder" / "inner.ini"


# ---- read_active_preset[_raw] ---------------------------------------------

def _write_ini(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")


def test_read_active_preset_raw_returns_exact_text(tmp_path: Path) -> None:
    ini = tmp_path / "ReShade.ini"
    _write_ini(ini, "[GENERAL]\nPresetPath=D:/presets/cinematic.ini  \nOther=x\n")
    # Note: configparser strips surrounding whitespace but our raw reader
    # should return the value as ReShade wrote it (trimmed of surrounding
    # whitespace is acceptable — ReShade itself normalizes).
    raw = read_active_preset_raw(ini)
    assert raw is not None
    assert raw.strip() == "D:/presets/cinematic.ini"


def test_read_active_preset_absolute_path(tmp_path: Path) -> None:
    ini = tmp_path / "ReShade.ini"
    absolute = tmp_path / "abs" / "preset.ini"
    _write_ini(ini, f"[GENERAL]\nPresetPath={absolute}\n")

    result = read_active_preset(ini, None, tmp_path / "bin64")
    assert result == absolute


def test_read_active_preset_relative_with_base_path(tmp_path: Path) -> None:
    ini = tmp_path / "ReShade.ini"
    base = tmp_path / "base_presets"
    _write_ini(ini, "[GENERAL]\nPresetPath=foo.ini\n")

    result = read_active_preset(ini, base, tmp_path / "bin64")
    assert result == base / "foo.ini"


def test_read_active_preset_missing_key_returns_none(tmp_path: Path) -> None:
    ini = tmp_path / "ReShade.ini"
    _write_ini(ini, "[GENERAL]\nSomethingElse=1\n")

    result = read_active_preset(ini, None, tmp_path / "bin64")
    assert result is None


def test_read_active_preset_missing_file_returns_none(tmp_path: Path) -> None:
    result = read_active_preset(tmp_path / "nonexistent.ini", None, tmp_path)
    assert result is None


# ---- set_active_preset ----------------------------------------------------

def test_set_active_preset_accepts_raw_string_and_returns_previous(
    tmp_path: Path,
) -> None:
    ini = tmp_path / "ReShade.ini"
    _write_ini(ini, "[GENERAL]\nPresetPath=before.ini\nOther=keep\n")

    previous = set_active_preset(ini, "after.ini")

    assert previous == "before.ini"
    text = ini.read_text(encoding="utf-8")
    assert "PresetPath=after.ini" in text
    assert "Other=keep" in text


def test_set_active_preset_preserves_comments(tmp_path: Path) -> None:
    """Comments, blank lines, and other keys must survive a preset switch."""
    ini = tmp_path / "ReShade.ini"
    original = (
        "; My custom ReShade setup -- don't edit\n"
        "\n"
        "[GENERAL]\n"
        "; this is the current preset\n"
        "PresetPath=cinematic.ini\n"
        "Something=42\n"
        "\n"
        "[INPUT]\n"
        "KeyOverlay=36,0,0,0\n"
    )
    _write_ini(ini, original)

    set_active_preset(ini, "photoreal.ini")

    written = ini.read_text(encoding="utf-8")
    assert "; My custom ReShade setup" in written
    assert "; this is the current preset" in written
    assert "PresetPath=photoreal.ini" in written
    assert "[INPUT]" in written
    assert "KeyOverlay=36,0,0,0" in written


def test_set_active_preset_preserves_other_general_keys(tmp_path: Path) -> None:
    ini = tmp_path / "ReShade.ini"
    _write_ini(ini, "[GENERAL]\nPresetPath=a.ini\nStartupPresetPath=b.ini\n")

    set_active_preset(ini, "c.ini")

    text = ini.read_text(encoding="utf-8")
    assert "PresetPath=c.ini" in text
    assert "StartupPresetPath=b.ini" in text


def test_set_active_preset_adds_key_when_missing(tmp_path: Path) -> None:
    """ReShade writes PresetPath on first save, but a very fresh install
    may not have it yet. We must be able to add it."""
    ini = tmp_path / "ReShade.ini"
    _write_ini(ini, "[GENERAL]\nOther=x\n")

    previous = set_active_preset(ini, "new.ini")

    assert previous == ""  # no previous value
    text = ini.read_text(encoding="utf-8")
    assert "PresetPath=new.ini" in text


# ---- read_preset_sections -------------------------------------------------

def test_read_preset_sections_parses_shader_blocks(tmp_path: Path) -> None:
    preset = tmp_path / "foo.ini"
    preset.write_text(
        "[Bloom.fx]\n"
        "Threshold=0.5\n"
        "Intensity=1.2\n"
        "[SMAA.fx]\n"
        "Quality=2\n"
        "Techniques=Bloom,SMAA\n",
        encoding="utf-8",
    )

    sections = read_preset_sections(preset)
    assert "Bloom.fx" in sections
    assert sections["Bloom.fx"]["threshold"] == "0.5"
    assert "SMAA.fx" in sections
    assert sections["SMAA.fx"]["quality"] == "2"


# ---- is_game_running ------------------------------------------------------

def test_is_game_running_detects_cd_process() -> None:
    fake_processes = [
        SimpleNamespace(info={"name": "explorer.exe"}),
        SimpleNamespace(info={"name": "CrimsonDesert.exe"}),
    ]
    with patch("psutil.process_iter", return_value=fake_processes):
        assert is_game_running() is True


def test_is_game_running_case_insensitive() -> None:
    fake_processes = [
        SimpleNamespace(info={"name": "crimsondesert.exe"}),
    ]
    with patch("psutil.process_iter", return_value=fake_processes):
        assert is_game_running() is True


def test_is_game_running_returns_false_when_absent() -> None:
    fake_processes = [
        SimpleNamespace(info={"name": "chrome.exe"}),
        SimpleNamespace(info={"name": "pycharm64.exe"}),
    ]
    with patch("psutil.process_iter", return_value=fake_processes):
        assert is_game_running() is False


def test_is_game_running_tolerates_process_lookup_errors() -> None:
    """psutil raises NoSuchProcess/AccessDenied on individual process reads.
    Our helper must survive and return a best-effort answer."""
    import psutil

    class BrokenProcess:
        info = property(lambda self: (_ for _ in ()).throw(psutil.AccessDenied()))

    fake_processes = [
        BrokenProcess(),
        SimpleNamespace(info={"name": "CrimsonDesert.exe"}),
    ]
    with patch("psutil.process_iter", return_value=fake_processes):
        assert is_game_running() is True


# ---- same_preset ----------------------------------------------------------

def test_same_preset_exact_match(tmp_path: Path) -> None:
    p = tmp_path / "preset.ini"
    p.write_text("x")
    assert same_preset(p, p) is True


def test_same_preset_case_insensitive_on_windows() -> None:
    # We don't actually need the files to exist for same_preset — it uses
    # normcase + normpath.
    a = Path("C:/Foo/Preset.INI")
    b = Path("c:/foo/preset.ini")
    assert same_preset(a, b) is True


def test_same_preset_handles_mixed_separators() -> None:
    a = Path("C:/Foo/x.ini")
    b = Path("C:\\Foo\\x.ini")
    assert same_preset(a, b) is True


def test_same_preset_different_files_returns_false() -> None:
    a = Path("C:/Foo/a.ini")
    b = Path("C:/Foo/b.ini")
    assert same_preset(a, b) is False


def test_same_preset_resolves_dot_segments() -> None:
    a = Path("C:/Foo/./x.ini")
    b = Path("C:/Foo/x.ini")
    assert same_preset(a, b) is True
