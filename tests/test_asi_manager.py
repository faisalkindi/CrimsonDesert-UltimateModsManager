from pathlib import Path

from cdmm.asi.asi_manager import AsiManager, AsiPlugin


def _setup_bin64(tmp_path: Path) -> Path:
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    return bin64


def test_scan_finds_asi_plugins(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"DLL_DATA")
    (bin64 / "ModB.asi").write_bytes(b"DLL_DATA")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    assert len(plugins) == 2
    assert all(p.enabled for p in plugins)
    names = {p.name for p in plugins}
    assert "ModA" in names
    assert "ModB" in names


def test_scan_finds_disabled_plugins(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi.disabled").write_bytes(b"DLL_DATA")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    assert len(plugins) == 1
    assert plugins[0].enabled is False
    assert plugins[0].name == "ModA"


def test_scan_finds_ini(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"DLL_DATA")
    (bin64 / "ModA.ini").write_text("[General]\nSpeed=1.0\n")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    assert plugins[0].ini_path is not None
    assert plugins[0].ini_path.name == "ModA.ini"


def test_has_loader(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    mgr = AsiManager(bin64)
    assert mgr.has_loader() is False

    (bin64 / "winmm.dll").write_bytes(b"LOADER")
    assert mgr.has_loader() is True


def test_enable_disable(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"DLL_DATA")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    plugin = plugins[0]
    assert plugin.enabled is True

    mgr.disable(plugin)
    assert plugin.enabled is False
    assert (bin64 / "ModA.asi.disabled").exists()
    assert not (bin64 / "ModA.asi").exists()

    mgr.enable(plugin)
    assert plugin.enabled is True
    assert (bin64 / "ModA.asi").exists()
    assert not (bin64 / "ModA.asi.disabled").exists()


def test_detect_conflicts_overlapping_hooks(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"DLL")
    (bin64 / "ModA.ini").write_text("[Hooks]\nTargetDLL=xinput1_3.dll\n")
    (bin64 / "ModB.asi").write_bytes(b"DLL")
    (bin64 / "ModB.ini").write_text("[Hooks]\nTargetDLL=xinput1_3.dll\n")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    conflicts = mgr.detect_conflicts(plugins)

    assert len(conflicts) >= 1
    assert any("hook" in c.reason.lower() or "TargetDLL" in c.reason for c in conflicts)


def test_detect_conflicts_no_overlap(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "ModA.asi").write_bytes(b"DLL")
    (bin64 / "ModA.ini").write_text("[General]\nSpeed=1.0\n")
    (bin64 / "ModB.asi").write_bytes(b"DLL")
    (bin64 / "ModB.ini").write_text("[Settings]\nVolume=50\n")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    conflicts = mgr.detect_conflicts(plugins)
    assert len(conflicts) == 0


def test_scan_empty_dir(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    mgr = AsiManager(bin64)
    assert mgr.scan() == []


def test_scan_nonexistent_dir(tmp_path: Path) -> None:
    mgr = AsiManager(tmp_path / "nonexistent")
    assert mgr.scan() == []
