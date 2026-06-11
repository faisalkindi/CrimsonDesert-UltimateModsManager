"""Legacy (pre-sidecar) ASI uninstall must not delete prefix siblings.

The pre-sidecar fallback deleted every .ini whose stem merely STARTED
with the plugin stem, so uninstalling a plugin named "CD" deleted
CDNoHelm.ini, CDNorthLock.ini, etc. The fallback now deletes an INI
only when its stem EQUALS the plugin stem, or when it is the plugin's
resolved ini_path AND no other plugin in bin64 owns it.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.asi.asi_manager import AsiManager


def _setup_bin64(tmp_path: Path) -> Path:
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    return bin64


def _plugin_by_name(mgr: AsiManager, name: str):
    return next(p for p in mgr.scan() if p.name == name)


def test_legacy_uninstall_spares_prefix_sibling_inis(tmp_path: Path) -> None:
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "CD.asi").write_bytes(b"DLL")
    (bin64 / "CD.ini").write_text("[General]\nA=1\n")
    (bin64 / "CDNoHelm.asi").write_bytes(b"DLL")
    (bin64 / "CDNoHelm.ini").write_text("[General]\nB=2\n")

    mgr = AsiManager(bin64)
    deleted = mgr.uninstall(_plugin_by_name(mgr, "CD"))

    assert "CD.asi" in deleted
    assert "CD.ini" in deleted
    assert not (bin64 / "CD.asi").exists()
    assert not (bin64 / "CD.ini").exists()
    # The prefix siblings must survive.
    assert (bin64 / "CDNoHelm.asi").exists()
    assert (bin64 / "CDNoHelm.ini").exists(), (
        "uninstalling 'CD' deleted CDNoHelm.ini via the legacy "
        "prefix heuristic")


def test_legacy_uninstall_spares_forward_prefix_resolved_ini(
        tmp_path: Path) -> None:
    """When CD.asi has no CD.ini, _find_ini's forward-prefix fallback
    resolves CDNoHelm.ini as CD's ini_path. The uninstall fallback
    must still not delete it because CDNoHelm.asi owns it."""
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "CD.asi").write_bytes(b"DLL")
    (bin64 / "CDNoHelm.asi").write_bytes(b"DLL")
    (bin64 / "CDNoHelm.ini").write_text("[General]\nB=2\n")

    mgr = AsiManager(bin64)
    deleted = mgr.uninstall(_plugin_by_name(mgr, "CD"))

    assert deleted == ["CD.asi"]
    assert (bin64 / "CDNoHelm.ini").exists()


def test_legacy_uninstall_still_removes_versioned_plugin_ini(
        tmp_path: Path) -> None:
    """The legitimate reverse-prefix case keeps working: the author
    baked the version into the .asi name but kept the .ini stable
    (EnhancedFlightv31.asi reads EnhancedFlight.ini). No other plugin
    owns the INI, so legacy uninstall removes it with the plugin."""
    bin64 = _setup_bin64(tmp_path)
    (bin64 / "EnhancedFlightv31.asi").write_bytes(b"DLL")
    (bin64 / "EnhancedFlight.ini").write_text("[General]\nSpeed=1.0\n")

    mgr = AsiManager(bin64)
    deleted = mgr.uninstall(_plugin_by_name(mgr, "EnhancedFlightv31"))

    assert "EnhancedFlightv31.asi" in deleted
    assert "EnhancedFlight.ini" in deleted
    assert not (bin64 / "EnhancedFlight.ini").exists()
