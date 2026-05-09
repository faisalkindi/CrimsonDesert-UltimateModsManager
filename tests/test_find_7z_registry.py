"""7-Zip discovery via Windows registry.

Bug from Faisal 2026-04-27: femdogga had 7-Zip installed but
CDUMM rejected the .rar import with "RAR import requires 7-Zip".
Root cause: `_find_7z()` only checks `Program Files`, `Program
Files (x86)`, and `shutil.which("7z")`. Misses Scoop, Chocolatey,
NanaZip, portable installs, or any case where the 7-Zip directory
isn't on PATH (the official installer does NOT add itself to PATH).

Fix: read the documented registry key the 7-Zip installer writes:
- HKEY_LOCAL_MACHINE\SOFTWARE\7-Zip\Path  (admin install)
- HKEY_CURRENT_USER\Software\7-Zip\Path   (user install)
- HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\7-Zip\Path
  (32-bit 7-Zip on 64-bit Windows)
The value is the install directory; we append `7z.exe`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.mark.skipif(sys.platform != "win32",
                    reason="winreg is Windows-only")
def test_find_7z_uses_hklm_registry_when_default_paths_missing(tmp_path):
    """7-Zip in custom location with HKLM\\SOFTWARE\\7-Zip\\Path set
    must be discovered."""
    from cdumm.engine import import_handler

    # Real 7z.exe at a non-default path
    install_dir = tmp_path / "Apps" / "7-Zip"
    install_dir.mkdir(parents=True)
    seven_z = install_dir / "7z.exe"
    seven_z.write_bytes(b"")

    def fake_open(hive, sub):
        import winreg
        # Match HKLM\SOFTWARE\7-Zip
        if hive == winreg.HKEY_LOCAL_MACHINE and sub == r"SOFTWARE\7-Zip":
            mock_key = MagicMock()
            mock_key.__enter__ = lambda s: s
            mock_key.__exit__ = lambda *a: None
            return mock_key
        raise FileNotFoundError

    def fake_query(key, name):
        if name == "Path":
            return (str(install_dir), 1)
        raise FileNotFoundError

    with patch.object(import_handler, "_FIND_7Z_DEFAULT_PATHS", []):
        with patch("winreg.OpenKey", side_effect=fake_open):
            with patch("winreg.QueryValueEx", side_effect=fake_query):
                with patch("shutil.which", return_value=None):
                    found = import_handler._find_7z()
    assert found == str(seven_z), (
        f"_find_7z() must return the registry-discovered path, got {found!r}")


@pytest.mark.skipif(sys.platform != "win32",
                    reason="winreg is Windows-only")
def test_find_7z_uses_hkcu_registry_for_user_install(tmp_path):
    """7-Zip user-only install (HKCU) must be discovered when HKLM
    is empty."""
    from cdumm.engine import import_handler

    install_dir = tmp_path / "user_7zip"
    install_dir.mkdir(parents=True)
    seven_z = install_dir / "7z.exe"
    seven_z.write_bytes(b"")

    def fake_open(hive, sub):
        import winreg
        if hive == winreg.HKEY_CURRENT_USER and sub == r"Software\7-Zip":
            mock_key = MagicMock()
            mock_key.__enter__ = lambda s: s
            mock_key.__exit__ = lambda *a: None
            return mock_key
        raise FileNotFoundError

    def fake_query(key, name):
        if name == "Path":
            return (str(install_dir), 1)
        raise FileNotFoundError

    with patch.object(import_handler, "_FIND_7Z_DEFAULT_PATHS", []):
        with patch("winreg.OpenKey", side_effect=fake_open):
            with patch("winreg.QueryValueEx", side_effect=fake_query):
                with patch("shutil.which", return_value=None):
                    found = import_handler._find_7z()
    assert found == str(seven_z)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="winreg is Windows-only")
def test_find_7z_uses_wow6432node_for_32bit_on_64bit(tmp_path):
    """32-bit 7-Zip on 64-bit Windows lives at WOW6432Node — must be
    discovered when HKLM\\SOFTWARE\\7-Zip is empty."""
    from cdumm.engine import import_handler

    install_dir = tmp_path / "Program Files (x86)" / "7-Zip"
    install_dir.mkdir(parents=True)
    seven_z = install_dir / "7z.exe"
    seven_z.write_bytes(b"")

    def fake_open(hive, sub):
        import winreg
        if hive == winreg.HKEY_LOCAL_MACHINE and sub == r"SOFTWARE\WOW6432Node\7-Zip":
            mock_key = MagicMock()
            mock_key.__enter__ = lambda s: s
            mock_key.__exit__ = lambda *a: None
            return mock_key
        raise FileNotFoundError

    def fake_query(key, name):
        if name == "Path":
            return (str(install_dir), 1)
        raise FileNotFoundError

    with patch.object(import_handler, "_FIND_7Z_DEFAULT_PATHS", []):
        with patch("winreg.OpenKey", side_effect=fake_open):
            with patch("winreg.QueryValueEx", side_effect=fake_query):
                with patch("shutil.which", return_value=None):
                    found = import_handler._find_7z()
    assert found == str(seven_z)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="winreg is Windows-only")
def test_find_7z_returns_none_when_registry_path_missing_exe(tmp_path):
    """Defensive: registry says 7-Zip lives at PATH/X but X/7z.exe
    doesn't exist (uninstalled-but-not-cleaned). Don't return a
    bogus path."""
    from cdumm.engine import import_handler

    install_dir = tmp_path / "ghost"
    install_dir.mkdir(parents=True)
    # Note: NO 7z.exe inside install_dir

    def fake_open(hive, sub):
        import winreg
        if hive == winreg.HKEY_LOCAL_MACHINE and sub == r"SOFTWARE\7-Zip":
            mock_key = MagicMock()
            mock_key.__enter__ = lambda s: s
            mock_key.__exit__ = lambda *a: None
            return mock_key
        raise FileNotFoundError

    def fake_query(key, name):
        if name == "Path":
            return (str(install_dir), 1)
        raise FileNotFoundError

    with patch.object(import_handler, "_FIND_7Z_DEFAULT_PATHS", []):
        with patch("winreg.OpenKey", side_effect=fake_open):
            with patch("winreg.QueryValueEx", side_effect=fake_query):
                with patch("shutil.which", return_value=None):
                    found = import_handler._find_7z()
    assert found is None, (
        "Registry path that doesn't actually contain 7z.exe must not "
        "be returned — the caller would just fail with 'file not found'.")


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only test path. macOS / Linux _find_7z searches "
           "_FIND_7Z_UNIX_PATHS first (Homebrew sevenzip / system bsdtar) "
           "regardless of mocked _FIND_7Z_DEFAULT_PATHS.")
def test_find_7z_default_paths_still_work(tmp_path):
    """Regression: when 7-Zip IS at the default path, return it
    without touching the registry."""
    from cdumm.engine import import_handler

    seven_z = tmp_path / "7z.exe"
    seven_z.write_bytes(b"")

    with patch.object(import_handler, "_FIND_7Z_DEFAULT_PATHS",
                      [str(seven_z)]):
        found = import_handler._find_7z()
    assert found == str(seven_z)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only test path. macOS / Linux _find_7z searches "
           "_FIND_7Z_UNIX_PATHS first regardless of the mocked which().")
def test_find_7z_falls_back_to_which_when_all_else_fails(tmp_path):
    """If registry is empty AND default paths missing, fall back to
    shutil.which (PATH search)."""
    from cdumm.engine import import_handler

    seven_z = tmp_path / "from_path" / "7z.exe"
    seven_z.parent.mkdir(parents=True)
    seven_z.write_bytes(b"")

    with patch.object(import_handler, "_FIND_7Z_DEFAULT_PATHS", []):
        with patch.object(import_handler, "_find_7z_in_registry",
                          return_value=None):
            with patch("shutil.which", return_value=str(seven_z)):
                found = import_handler._find_7z()
    assert found == str(seven_z)
