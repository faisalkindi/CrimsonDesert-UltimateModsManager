"""ASI update must not silently wipe user-tuned INI config.

``AsiManager.update`` copies every companion .ini from the source
over bin64 unconditionally. Users tune those INIs (key remaps, speed
values); the update has to keep working that way (mod authors ship
new keys in new INIs) but must park the user's previous file at
``<name>.ini.bak`` first. One .bak generation is enough; a stale
.bak gets overwritten. Fresh installs are unaffected.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.asi.asi_manager import AsiManager


def _setup_bin64(tmp_path: Path) -> Path:
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    return bin64


def _plugin(mgr: AsiManager, name: str):
    return next(p for p in mgr.scan() if p.name == name)


def test_update_from_dir_backs_up_existing_ini(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"OLD_DLL")
    (bin64 / "ModA.ini").write_text("[General]\nSpeed=9.9 ; user tuned\n")

    source = tmp_path / "new_version"
    source.mkdir()
    (source / "ModA.asi").write_bytes(b"NEW_DLL")
    (source / "ModA.ini").write_text("[General]\nSpeed=1.0\n")

    mgr = AsiManager(bin64)
    mgr.update(_plugin(mgr, "ModA"), source)

    assert (bin64 / "ModA.asi").read_bytes() == b"NEW_DLL"
    assert (bin64 / "ModA.ini").read_text() == "[General]\nSpeed=1.0\n"
    bak = bin64 / "ModA.ini.bak"
    assert bak.exists(), "user-tuned INI was overwritten without a backup"
    assert "user tuned" in bak.read_text()


def test_update_from_file_backs_up_existing_ini(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"OLD_DLL")
    (bin64 / "ModA.ini").write_text("[General]\nSpeed=9.9 ; user tuned\n")

    source = tmp_path / "dl"
    source.mkdir()
    new_asi = source / "ModA.asi"
    new_asi.write_bytes(b"NEW_DLL")
    (source / "ModA.ini").write_text("[General]\nSpeed=1.0\n")

    mgr = AsiManager(bin64)
    mgr.update(_plugin(mgr, "ModA"), new_asi)

    assert (bin64 / "ModA.ini").read_text() == "[General]\nSpeed=1.0\n"
    bak = bin64 / "ModA.ini.bak"
    assert bak.exists()
    assert "user tuned" in bak.read_text()


def test_update_overwrites_stale_bak(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"OLD_DLL")
    (bin64 / "ModA.ini").write_text("current user config\n")
    (bin64 / "ModA.ini.bak").write_text("ancient backup\n")

    source = tmp_path / "new_version"
    source.mkdir()
    (source / "ModA.asi").write_bytes(b"NEW_DLL")
    (source / "ModA.ini").write_text("fresh defaults\n")

    mgr = AsiManager(bin64)
    mgr.update(_plugin(mgr, "ModA"), source)

    assert (bin64 / "ModA.ini.bak").read_text() == "current user config\n"


def test_update_creates_no_bak_for_new_ini(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"OLD_DLL")

    source = tmp_path / "new_version"
    source.mkdir()
    (source / "ModA.asi").write_bytes(b"NEW_DLL")
    (source / "ModA.ini").write_text("fresh defaults\n")

    mgr = AsiManager(bin64)
    mgr.update(_plugin(mgr, "ModA"), source)

    assert (bin64 / "ModA.ini").exists()
    assert not (bin64 / "ModA.ini.bak").exists()


def test_fresh_install_creates_no_bak(tmp_path: Path) -> None:
    """install() semantics are unchanged: no backups on install,
    even when an INI with the same name already sits in bin64."""
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.ini").write_text("leftover config\n")

    source = tmp_path / "mod"
    source.mkdir()
    (source / "ModA.asi").write_bytes(b"DLL")
    (source / "ModA.ini").write_text("shipped config\n")

    mgr = AsiManager(bin64)
    mgr.install(source)

    assert (bin64 / "ModA.ini").read_text() == "shipped config\n"
    assert not (bin64 / "ModA.ini.bak").exists()
