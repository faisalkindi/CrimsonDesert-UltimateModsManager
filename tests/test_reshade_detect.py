"""ReShade install detection: finds ReShade next to CrimsonDesert.exe,
reads [INSTALL] BasePath=, enumerates preset .ini files.

Three states:
  - installed     — DLL + ini both present
  - not_installed — DLL missing (the defining signal of a ReShade install)
  - error         — IO exception while scanning

Pure-logic module; no Qt imports.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cdumm.engine.reshade_detect import ReshadeInstall, detect_reshade_install


def _make_game_dir(root: Path, *, with_dxgi: bool = False,
                   with_d3d12: bool = False, with_ini: bool = False,
                   ini_contents: str | None = None,
                   with_shaders: bool = False,
                   extra_inis: dict[str, str] | None = None) -> Path:
    """Build a synthetic `<game_dir>/bin64/` layout and return game_dir."""
    bin64 = root / "bin64"
    bin64.mkdir(parents=True)
    (bin64 / "CrimsonDesert.exe").write_bytes(b"\x00" * 16)
    if with_dxgi:
        (bin64 / "dxgi.dll").write_bytes(b"\x00" * 16)
    if with_d3d12:
        (bin64 / "d3d12.dll").write_bytes(b"\x00" * 16)
    if with_ini:
        (bin64 / "ReShade.ini").write_text(ini_contents or "[GENERAL]\nPresetPath=\n")
    if with_shaders:
        (bin64 / "reshade-shaders").mkdir()
    if extra_inis:
        for name, contents in extra_inis.items():
            (bin64 / name).write_text(contents)
    return root


def test_detect_empty_game_dir_returns_not_installed(tmp_path: Path) -> None:
    game_dir = _make_game_dir(tmp_path)
    result = detect_reshade_install(game_dir)

    assert result.state == "not_installed"
    assert result.installed is False
    assert result.dll_path is None
    assert result.ini_path is None
    assert result.shaders_dir is None
    assert result.presets == []
    assert result.error is None


def test_detect_dxgi_dll_next_to_exe_returns_installed(tmp_path: Path) -> None:
    game_dir = _make_game_dir(
        tmp_path, with_dxgi=True, with_ini=True, with_shaders=True)
    result = detect_reshade_install(game_dir)

    assert result.state == "installed"
    assert result.installed is True
    assert result.dll_path == game_dir / "bin64" / "dxgi.dll"
    assert result.ini_path == game_dir / "bin64" / "ReShade.ini"
    assert result.shaders_dir == game_dir / "bin64" / "reshade-shaders"


def test_detect_d3d12_dll_alt_name(tmp_path: Path) -> None:
    """DX12 games sometimes use d3d12.dll as the proxy."""
    game_dir = _make_game_dir(
        tmp_path, with_d3d12=True, with_ini=True)
    result = detect_reshade_install(game_dir)

    assert result.state == "installed"
    assert result.dll_path == game_dir / "bin64" / "d3d12.dll"


def test_detect_dll_without_ini_still_counts_as_installed(tmp_path: Path) -> None:
    """Edge case: ReShade installed but ReShade.ini not yet generated (it
    gets written on first game launch). We still consider this installed;
    downstream code will handle the missing-ini case."""
    game_dir = _make_game_dir(tmp_path, with_dxgi=True)
    result = detect_reshade_install(game_dir)

    assert result.state == "installed"
    assert result.dll_path == game_dir / "bin64" / "dxgi.dll"
    assert result.ini_path is None
    assert result.presets == []


def test_detect_presets_enumerates_ini_files(tmp_path: Path) -> None:
    """Preset disambiguation: a file is a preset iff it has Techniques=
    OR at least one [*.fx] section. ReShade.ini itself is excluded."""
    extras = {
        "Cinematic.ini": "[Bloom.fx]\nThreshold=0.5\nTechniques=Bloom,SMAA\n",
        "NotAPreset.ini": "[app]\nkey=value\n",  # config-only; no fx/Techniques
        "Photoreal.ini": "Techniques=HDR\n[HDR.fx]\nfoo=1\n",
    }
    game_dir = _make_game_dir(
        tmp_path, with_dxgi=True, with_ini=True, extra_inis=extras)
    result = detect_reshade_install(game_dir)

    preset_names = {p.name for p in result.presets}
    assert preset_names == {"Cinematic.ini", "Photoreal.ini"}
    assert all(p.name != "ReShade.ini" for p in result.presets)
    assert all(p.name != "NotAPreset.ini" for p in result.presets)


def test_detect_reads_base_path_from_ini(tmp_path: Path) -> None:
    """[INSTALL] BasePath= is the ReShade-documented base for relative paths."""
    custom_base = tmp_path / "custom_shaders"
    custom_base.mkdir()
    ini_contents = (
        "[GENERAL]\n"
        "PresetPath=foo.ini\n"
        "[INSTALL]\n"
        f"BasePath={custom_base}\n"
    )
    game_dir = _make_game_dir(
        tmp_path, with_dxgi=True, with_ini=True, ini_contents=ini_contents)
    result = detect_reshade_install(game_dir)

    assert result.base_path == custom_base


def test_detect_base_path_falls_back_to_bin64(tmp_path: Path) -> None:
    """No BasePath= in ReShade.ini -> base_path defaults to bin64 dir
    (ReShade's own documented fallback)."""
    ini_contents = "[GENERAL]\nPresetPath=\n"  # no [INSTALL] section at all
    game_dir = _make_game_dir(
        tmp_path, with_dxgi=True, with_ini=True, ini_contents=ini_contents)
    result = detect_reshade_install(game_dir)

    assert result.base_path == game_dir / "bin64"


def test_detect_base_path_falls_back_when_dll_but_no_ini(tmp_path: Path) -> None:
    """DLL present but ReShade.ini missing -> base_path = bin64."""
    game_dir = _make_game_dir(tmp_path, with_dxgi=True)
    result = detect_reshade_install(game_dir)

    assert result.base_path == game_dir / "bin64"


def test_detect_io_exception_returns_error_state(tmp_path: Path) -> None:
    """Filesystem failure during detect -> state='error', not 'not_installed'."""
    game_dir = _make_game_dir(tmp_path, with_dxgi=True, with_ini=True)

    def boom(*args, **kwargs):
        raise PermissionError("antivirus blocked access")

    # Fail at the DLL existence probe (first thing detect_reshade_install does
    # inside the try block).
    with patch("cdumm.engine.reshade_detect._dll_path", side_effect=boom):
        result = detect_reshade_install(game_dir)

    assert result.state == "error"
    assert result.installed is False
    assert result.error is not None
    assert "PermissionError" in result.error


def test_detect_relative_base_path_resolves_against_bin64(tmp_path: Path) -> None:
    """If BasePath is relative (e.g. 'my_shaders'), it should resolve against
    bin64 -- not against the current working directory."""
    game_dir = tmp_path
    bin64 = game_dir / "bin64"
    bin64.mkdir()
    (bin64 / "CrimsonDesert.exe").write_bytes(b"\x00")
    (bin64 / "dxgi.dll").write_bytes(b"\x00")
    # Relative path in BasePath=.
    (bin64 / "ReShade.ini").write_text(
        "[GENERAL]\nPresetPath=foo.ini\n"
        "[INSTALL]\nBasePath=my_shaders\n")
    # Create the actual directory the relative path should resolve to.
    (bin64 / "my_shaders").mkdir()
    (bin64 / "my_shaders" / "Preset.ini").write_text(
        "Techniques=Bloom\n[Bloom.fx]\na=1\n")

    result = detect_reshade_install(game_dir)
    # The resolved base_path should live inside bin64, not the CWD.
    assert result.base_path == (bin64 / "my_shaders").resolve(strict=False)
    assert len(result.presets) == 1
    assert result.presets[0].name == "Preset.ini"


def test_detect_quoted_base_path_is_stripped(tmp_path: Path) -> None:
    """ReShade.ini with BasePath=\"my_shaders\" (quoted) should resolve the
    same as the unquoted form."""
    game_dir = _make_game_dir(
        tmp_path, with_dxgi=True, with_ini=True,
        ini_contents='[GENERAL]\nPresetPath=\n[INSTALL]\nBasePath="my_shaders"\n')
    (game_dir / "bin64" / "my_shaders").mkdir()

    result = detect_reshade_install(game_dir)
    assert result.base_path == (game_dir / "bin64" / "my_shaders").resolve(strict=False)


def test_detect_enumerates_presets_in_subfolders(tmp_path: Path) -> None:
    """ReShade supports subfolder-organized presets (verified via crosire's
    own sub-folder tutorial). Enumeration must recurse."""
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "CrimsonDesert.exe").write_bytes(b"\x00")
    (bin64 / "dxgi.dll").write_bytes(b"\x00")
    (bin64 / "ReShade.ini").write_text("[GENERAL]\nPresetPath=\n")
    # Top-level preset
    (bin64 / "Top.ini").write_text("Techniques=Bloom\n[Bloom.fx]\na=1\n")
    # Nested preset pack
    pack = bin64 / "CinematicPack"
    pack.mkdir()
    (pack / "Cinematic.ini").write_text("Techniques=SMAA\n[SMAA.fx]\nb=2\n")
    (pack / "Noir.ini").write_text("Techniques=HDR\n[HDR.fx]\nc=3\n")
    # Deeper subfolder
    deep = pack / "More"
    deep.mkdir()
    (deep / "Extra.ini").write_text("Techniques=DOF\n[DOF.fx]\nd=4\n")

    result = detect_reshade_install(tmp_path)
    names = sorted(p.name for p in result.presets)
    assert names == ["Cinematic.ini", "Extra.ini", "Noir.ini", "Top.ini"]


def test_detect_skips_reshade_shaders_folder(tmp_path: Path) -> None:
    """Don't recurse into `reshade-shaders/` -- it contains thousands of
    per-shader config files that aren't presets."""
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "CrimsonDesert.exe").write_bytes(b"\x00")
    (bin64 / "dxgi.dll").write_bytes(b"\x00")
    (bin64 / "ReShade.ini").write_text("[GENERAL]\nPresetPath=\n")
    shaders = bin64 / "reshade-shaders"
    shaders.mkdir()
    # File inside reshade-shaders that LOOKS like a preset by content but must be skipped.
    (shaders / "ShaderConfig.ini").write_text(
        "Techniques=Bloom\n[Bloom.fx]\nx=1\n")
    (bin64 / "RealPreset.ini").write_text(
        "Techniques=Bloom\n[Bloom.fx]\nx=1\n")

    result = detect_reshade_install(tmp_path)
    names = [p.name for p in result.presets]
    assert names == ["RealPreset.ini"]


def test_detect_enumerates_presets_from_custom_base_path(tmp_path: Path) -> None:
    """When BasePath= points elsewhere, presets are enumerated from there,
    not from bin64."""
    custom_base = tmp_path / "custom_presets"
    custom_base.mkdir()
    (custom_base / "MyPreset.ini").write_text("Techniques=X\n[X.fx]\na=1\n")
    (custom_base / "Other.ini").write_text("Techniques=Y\n")

    ini_contents = (
        "[GENERAL]\nPresetPath=MyPreset.ini\n"
        f"[INSTALL]\nBasePath={custom_base}\n"
    )
    game_dir = _make_game_dir(
        tmp_path, with_dxgi=True, with_ini=True, ini_contents=ini_contents)
    result = detect_reshade_install(game_dir)

    preset_names = {p.name for p in result.presets}
    assert preset_names == {"MyPreset.ini", "Other.ini"}


def test_detect_missing_bin64_dir_returns_not_installed(tmp_path: Path) -> None:
    """game_dir without a bin64 subfolder at all."""
    (tmp_path / "some-other-folder").mkdir()
    result = detect_reshade_install(tmp_path)

    assert result.state == "not_installed"
    assert result.dll_path is None


def test_dataclass_has_installed_convenience_property() -> None:
    """Convenience property for callers that don't want to check state."""
    inst = ReshadeInstall(state="installed", dll_path=None, ini_path=None,
                          shaders_dir=None, presets=[], base_path=None)
    not_inst = ReshadeInstall(state="not_installed", dll_path=None, ini_path=None,
                              shaders_dir=None, presets=[], base_path=None)
    err = ReshadeInstall(state="error", dll_path=None, ini_path=None,
                         shaders_dir=None, presets=[], base_path=None,
                         error="boom")

    assert inst.installed is True
    assert not_inst.installed is False
    assert err.installed is False
