"""ASI install must be symmetric with uninstall.

Bug from Faisal 2026-04-27: enowai uninstalled two ASI mods and
'version files' remained in bin64. Root cause: install copies any
.ini from the source folder regardless of stem
(`asi_manager.py:88-121`), but uninstall only deletes .ini files
where `f.stem.lower().startswith(plugin.name.lower())`
(`asi_manager.py:127-144`). Companion files with non-matching
stems (e.g. `config.ini` next to `MyMod.asi`, or any non-.ini
metadata file) get orphaned.

Fix: at install time, write a sidecar manifest
`<plugin>.cdumm-files.json` listing every file copied. Uninstall
reads the manifest and removes exactly those, plus the manifest
itself. Falls back to the legacy stem-prefix heuristic when the
manifest is missing (existing installs survive the upgrade).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_asi_source(src_dir: Path, asi_stem: str = "MyMod",
                     extra_inis: list[str] | None = None) -> Path:
    """Build a fake ASI mod folder. Returns the folder path."""
    src_dir.mkdir(parents=True, exist_ok=True)
    asi_path = src_dir / f"{asi_stem}.asi"
    asi_path.write_bytes(b"\x4D\x5A")  # MZ header — anything will do
    (src_dir / f"{asi_stem}.ini").write_text("# matching-stem ini")
    for extra in (extra_inis or []):
        (src_dir / extra).write_text(f"# {extra}")
    return src_dir


def test_install_writes_sidecar_manifest(tmp_path):
    """After install, a `<plugin>.cdumm-files.json` next to the
    .asi must list every file installed."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    src = _make_asi_source(tmp_path / "src", "MyMod",
                           extra_inis=["config.ini", "shared.ini"])
    mgr = AsiManager(bin64)
    installed = mgr.install(src)

    assert "MyMod.asi" in installed
    sidecar = bin64 / "MyMod.cdumm-files.json"
    assert sidecar.exists(), "Install must write a sidecar manifest"
    data = json.loads(sidecar.read_text())
    assert "files" in data and isinstance(data["files"], list)
    names = set(data["files"])
    assert "MyMod.asi" in names
    assert "MyMod.ini" in names
    assert "config.ini" in names
    assert "shared.ini" in names


def test_uninstall_removes_orphan_inis_via_sidecar(tmp_path):
    """After install + uninstall, every file listed in the sidecar
    must be deleted — including non-matching-stem .ini files that
    the legacy heuristic would have orphaned."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    src = _make_asi_source(tmp_path / "src", "MyMod",
                           extra_inis=["config.ini", "shared.ini"])
    mgr = AsiManager(bin64)
    mgr.install(src)

    plugins = mgr.scan()
    target = next(p for p in plugins if p.name == "MyMod")
    deleted = mgr.uninstall(target)

    assert "MyMod.asi" in deleted
    assert "MyMod.ini" in deleted
    assert "config.ini" in deleted
    assert "shared.ini" in deleted

    # bin64 must be empty (no orphans, no leftover sidecar)
    leftovers = list(bin64.iterdir())
    assert leftovers == [], (
        f"bin64 should be empty after uninstall, got {[p.name for p in leftovers]}")


def test_uninstall_legacy_install_falls_back_to_stem_match(tmp_path):
    """A plugin installed before this fix has no sidecar. Uninstall
    must still delete the .asi and matching-stem .ini files
    (existing behavior — backward-compat)."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    # Hand-build a "legacy" install: .asi + matching .ini, no sidecar
    (bin64 / "Legacy.asi").write_bytes(b"\x4D\x5A")
    (bin64 / "Legacy.ini").write_text("legacy")
    # Also drop a stranger .ini that legacy uninstall WOULDN'T remove
    (bin64 / "stranger.ini").write_text("not mine")

    mgr = AsiManager(bin64)
    plugins = mgr.scan()
    target = next(p for p in plugins if p.name == "Legacy")
    deleted = mgr.uninstall(target)

    assert "Legacy.asi" in deleted
    assert "Legacy.ini" in deleted
    # stranger.ini stays — legacy heuristic only deletes stem-matched.
    # We do NOT delete random .ini files just because they're in bin64.
    assert (bin64 / "stranger.ini").exists()


def test_install_does_not_write_companion_files_to_sidecar_list(tmp_path):
    """Sidecar must NOT list shared loader files (winmm.dll, etc.) —
    they're shared between ASI mods. Deleting them on uninstall
    would break every other ASI mod."""
    from cdumm.asi.asi_manager import AsiManager, ASI_LOADER_NAMES

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "MyMod.asi").write_bytes(b"\x4D\x5A")
    (src / "MyMod.ini").write_text("ok")
    # Some ASI mods bundle the loader. Pick the first known name.
    loader_name = next(iter(ASI_LOADER_NAMES))
    (src / loader_name).write_bytes(b"\x4D\x5A")

    mgr = AsiManager(bin64)
    mgr.install(src)

    sidecar = bin64 / "MyMod.cdumm-files.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert loader_name not in data["files"], (
        f"{loader_name} is a shared loader — must not be tracked by "
        "any single mod's sidecar.")


def test_reinstall_overwrites_sidecar(tmp_path):
    """If a mod is reinstalled (e.g., via `update`), the sidecar
    must reflect the NEW file set, not accumulate. Otherwise an
    uninstall-after-update would leak stale file references."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()

    # Install v1 with two .ini files
    src1 = _make_asi_source(tmp_path / "v1", "MyMod",
                            extra_inis=["old_only.ini"])
    mgr = AsiManager(bin64)
    mgr.install(src1)
    sidecar = bin64 / "MyMod.cdumm-files.json"
    v1_files = set(json.loads(sidecar.read_text())["files"])
    assert "old_only.ini" in v1_files

    # Re-install v2 without `old_only.ini` — sidecar must be rewritten
    src2 = _make_asi_source(tmp_path / "v2", "MyMod",
                            extra_inis=["new_only.ini"])
    # Remove the file the v2 source doesn't ship to simulate clean re-install
    (bin64 / "old_only.ini").unlink()
    mgr.install(src2)
    v2_files = set(json.loads(sidecar.read_text())["files"])
    assert "new_only.ini" in v2_files
    assert "old_only.ini" not in v2_files, (
        "Reinstall must overwrite the sidecar with the new file set, "
        "not merge with the old one.")
