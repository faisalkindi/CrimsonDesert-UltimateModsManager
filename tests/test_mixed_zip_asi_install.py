"""Mixed-zip ASI install: a zip containing both .asi and JSON
variants must install the ASI alongside the JSON variant import.

Bug from ZapZockt 2026-04-26 (GitHub #49, Character Creator v4.9):
The Character Creator zip ships:

    CharacterCreator.asi
    CharacterCreator.ini
    CharacterCreatorFemale/HumanFemale/FemaleAnimations.json
    CharacterCreatorFemale/OrcFemale/FemaleAnimations.json
    ...

The variant-pack import path at fluent_window.py:3554 calls
`import_multi_variant`, then returns. The .asi never installs
because the variant path doesn't go through `import_from_zip`
(which DOES stage ASIs). User reports: ASI not in CDUMM's ASI
mods tab.

Fix: extract a helper `install_companion_asis(extract_dir, asi_mgr)`
that scans the extract dir and installs any .asi files. Call it
from the variant import path right after `import_multi_variant`
returns.
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock


def test_install_companion_asis_calls_asi_mgr_when_asi_present(tmp_path):
    from cdumm.engine.import_handler import install_companion_asis

    # Mixed-zip extracted dir with a .asi and a JSON variant
    (tmp_path / "CharacterCreator.asi").write_bytes(b"\x4D\x5A\x90\x00")
    (tmp_path / "CharacterCreator.ini").write_text(
        "[Settings]\nKey=Value\n", encoding="utf-8")
    (tmp_path / "CharacterCreatorFemale").mkdir()
    (tmp_path / "CharacterCreatorFemale" / "FemaleAnimations.json").write_text(
        '{"name":"Female Animations","patches":[]}', encoding="utf-8")

    asi_mgr = MagicMock()
    asi_mgr.install.return_value = ["CharacterCreator"]

    result = install_companion_asis(tmp_path, asi_mgr)

    asi_mgr.install.assert_called_once()
    assert result == ["CharacterCreator"]


def test_install_companion_asis_skips_when_no_asi(tmp_path):
    from cdumm.engine.import_handler import install_companion_asis

    # JSON-only dir (no .asi anywhere)
    (tmp_path / "Some_Mod.json").write_text(
        '{"name":"Some Mod","patches":[]}', encoding="utf-8")

    asi_mgr = MagicMock()
    result = install_companion_asis(tmp_path, asi_mgr)

    asi_mgr.install.assert_not_called()
    assert result == []


def test_install_companion_asis_finds_asi_in_subdir(tmp_path):
    """Some mixed zips bury the ASI one level deep (e.g. plugins/X.asi).
    The helper should still detect it via rglob."""
    from cdumm.engine.import_handler import install_companion_asis

    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "Helper.asi").write_bytes(b"\x4D\x5A\x90\x00")

    asi_mgr = MagicMock()
    asi_mgr.install.return_value = ["Helper"]
    result = install_companion_asis(tmp_path, asi_mgr)

    asi_mgr.install.assert_called_once()
    assert result == ["Helper"]


def test_install_companion_asis_does_not_install_unrelated_inis(tmp_path):
    """REGRESSION: when tmp_extract contains a .asi PLUS unrelated .ini
    files belonging to JSON variants, only the .asi (and its same-named
    companion .ini) must be installed. Unrelated .ini files must NOT
    land in bin64.

    Previously install_companion_asis passed tmp_extract directly to
    AsiManager.install, which rglobs and copies EVERY .ini in the tree.
    For Character Creator-style zips this could dump JSON-variant
    config .ini files into bin64 and have them tracked as "owned" by
    the ASI mod (deleted on uninstall).
    """
    from cdumm.engine.import_handler import install_companion_asis
    from cdumm.asi.asi_manager import AsiManager

    extract_dir = tmp_path / "extract"
    bin64 = tmp_path / "bin64"
    extract_dir.mkdir()
    bin64.mkdir()

    # Mixed-zip layout: ASI + INI (companion) at root,
    # JSON variants in subdir with their own unrelated .ini.
    (extract_dir / "CharacterCreator.asi").write_bytes(b"\x4D\x5A\x90\x00")
    (extract_dir / "CharacterCreator.ini").write_text(
        "[ASI Config]\nKey=Value\n", encoding="utf-8")
    (extract_dir / "CharacterCreatorFemale").mkdir()
    (extract_dir / "CharacterCreatorFemale" / "FemaleAnimations.json").write_text(
        '{"name":"Female Animations","patches":[]}', encoding="utf-8")
    (extract_dir / "CharacterCreatorFemale" / "variant_config.ini").write_text(
        "# unrelated JSON variant config\nfoo=bar\n", encoding="utf-8")

    asi_mgr = AsiManager(bin64)
    install_companion_asis(extract_dir, asi_mgr)

    # ASI and its companion .ini installed.
    assert (bin64 / "CharacterCreator.asi").exists(), (
        "ASI plugin must be installed")
    assert (bin64 / "CharacterCreator.ini").exists(), (
        "Companion .ini matching ASI stem must be installed")
    # Unrelated .ini must NOT be installed.
    assert not (bin64 / "variant_config.ini").exists(), (
        "Unrelated .ini from JSON variants must NOT be installed in "
        "bin64. Found stale: "
        + ", ".join(p.name for p in bin64.iterdir()))
