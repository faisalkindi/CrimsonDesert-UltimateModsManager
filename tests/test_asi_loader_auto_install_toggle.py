"""UNIVERSE69 (Nexus) — OptiScaler ships its own winmm.dll proxy.
CDUMM's ASI page used to auto-reinstall its bundled winmm on every
refresh, overwriting OptiScaler's. Adding an opt-out avoids the fight.

This test covers the gating logic itself — no need to boot the full
ASI page or construct a bin64 with an actual loader. The invariant:
when ``asi_auto_install_loader=false`` is set in the Config KV table,
``AsiPage._install_bundled_loader`` returns without touching the
filesystem.
"""
from __future__ import annotations

import sys

import pytest
from pathlib import Path

from cdumm.storage.config import Config
from cdumm.storage.database import Database

pytest_qt = pytest.importorskip("pytestqt")

# The ASI loader installs a Win32 ``winmm.dll`` proxy that hooks
# ``CrimsonDesert.exe``. There's no equivalent on the native macOS
# build (no Windows exe to inject into) so ``_install_bundled_loader``
# short-circuits on non-Windows. The toggle-gating tests below
# assume the install path actually fires; skip them on darwin.
pytestmark = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="ASI loader auto-install short-circuits on non-Windows; "
           "the toggle gating logic is Windows-only.")


def _build_page_with_db(qtbot, db: Database, bin64: Path):
    from cdumm.asi.asi_manager import AsiManager
    from cdumm.gui.pages.asi_page import AsiPluginsPage
    page = AsiPluginsPage()
    qtbot.addWidget(page)
    page._db = db
    page._asi_manager = AsiManager(bin64)
    bin64.mkdir(parents=True, exist_ok=True)
    return page


def test_toggle_off_skips_winmm_install(qtbot, tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    Config(db).set("asi_auto_install_loader", "false")

    bin64 = tmp_path / "bin64"
    page = _build_page_with_db(qtbot, db, bin64)
    page._install_bundled_loader()

    assert not (bin64 / "winmm.dll").exists(), (
        "asi_auto_install_loader=false must leave bin64/winmm.dll alone "
        "so OptiScaler's own loader isn't overwritten")
    db.close()


def test_toggle_unset_still_installs(qtbot, tmp_path):
    """Absence of the key must mean default-ON so pre-v3.1.4 users
    don't silently lose their ASI loader on upgrade."""
    db = Database(tmp_path / "test.db")
    db.initialize()

    bin64 = tmp_path / "bin64"
    page = _build_page_with_db(qtbot, db, bin64)
    page._install_bundled_loader()

    # bundled winmm.dll exists in the source tree; asi_page resolves
    # it relative to the cdumm package root. If the source file is
    # present the install ran; otherwise the method early-returned
    # for a non-bug reason and we can't tell either way.
    from pathlib import Path as _P
    import cdumm as _cdumm_pkg
    bundled = _P(_cdumm_pkg.__file__).resolve().parents[2] / "asi_loader" / "winmm.dll"
    if bundled.exists():
        assert (bin64 / "winmm.dll").exists(), (
            "default-ON (unset key) must install bundled winmm.dll")
    db.close()


def test_toggle_on_installs_loader(qtbot, tmp_path):
    """Explicit 'true' behaves the same as unset — install runs."""
    db = Database(tmp_path / "test.db")
    db.initialize()
    Config(db).set("asi_auto_install_loader", "true")

    bin64 = tmp_path / "bin64"
    page = _build_page_with_db(qtbot, db, bin64)
    page._install_bundled_loader()

    from pathlib import Path as _P
    import cdumm as _cdumm_pkg
    bundled = _P(_cdumm_pkg.__file__).resolve().parents[2] / "asi_loader" / "winmm.dll"
    if bundled.exists():
        assert (bin64 / "winmm.dll").exists()
    db.close()
