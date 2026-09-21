"""An ASI install on a Microsoft Store / Game Pass install says up front
that most ASI plugins target the Steam exe.

GitHub #429 (woowoots). QuickSlotLockFilter refused itself with "exe size
differs from build 1.0.0.2850" (the Steam build) and Female Kliff
Longsword Animation Fix loaded and did nothing, both on a Store install.
CDUMM's copy into bin64 succeeded either way, so the success toast was
the last thing the user saw before a silent no-op. The warning is advice
after the success, not a refusal: Character Creator works on both.

The method is exercised directly with the window's dependencies stubbed
(no Qt event loop), and both install paths are pinned to call it.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cdumm.i18n import tr

pytest.importorskip("pytestqt")

SRC = (Path(__file__).resolve().parents[1]
       / "src" / "cdumm" / "gui" / "fluent_window.py")


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_both_asi_install_success_paths_call_the_warning():
    src = _src()
    # The batch path: success toast for N plugins, then the warning.
    i = src.find('content=f"{asi_count} ASI plugin(s) installed."')
    assert i != -1
    assert "_warn_asi_on_store_install()" in src[i:i + 400]
    # The single-drop path: the localized success toast, then the warning.
    j = src.find('title=tr("infobar.asi_installed")')
    assert j != -1
    assert "_warn_asi_on_store_install()" in src[j:j + 500]


def test_warning_strings_exist_in_every_locale():
    import json
    tdir = SRC.parents[1] / "translations"
    for f in sorted(tdir.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        assert "infobar.asi_store_install" in d, f.name
        assert "infobar.asi_store_install_msg" in d, f.name
        # Technical tokens stay in Latin script in every language.
        for tok in ("ASI", "Steam", "bin64"):
            assert tok in d["infobar.asi_store_install_msg"], (f.name, tok)
        # German compounds it as "Game-Pass-Version"; the token still appears.
        assert re.search(r"Game.Pass", d["infobar.asi_store_install_msg"]), f.name


def _call(game_dir, detector):
    """Run the real method unbound on a stand-in self, capturing InfoBars.

    Same shape as test_check_stale_appdata: import the class, patch the
    module-level collaborators, pass a SimpleNamespace as ``self``.
    """
    from cdumm.gui.fluent_window import CdummWindow
    fake_self = SimpleNamespace(_game_dir=game_dir)
    with patch("cdumm.gui.fluent_window.InfoBar") as bar,             patch("cdumm.storage.game_finder.is_xbox_install", detector):
        CdummWindow._warn_asi_on_store_install(fake_self)
    return bar.warning.call_args_list


def test_store_install_shows_the_warning(tmp_path):
    calls = _call(tmp_path, lambda p: True)
    assert len(calls) == 1
    kw = calls[0].kwargs
    assert kw["title"] == tr("infobar.asi_store_install")
    assert kw["content"] == tr("infobar.asi_store_install_msg")


def test_steam_install_stays_quiet(tmp_path):
    assert _call(tmp_path, lambda p: False) == []


def test_no_game_dir_stays_quiet():
    assert _call(None, lambda p: True) == []


@pytest.mark.parametrize("exc", [ImportError, RuntimeError])
def test_detector_failure_never_blocks_the_install(tmp_path, exc):
    """The warning is advice; a broken detector must not raise out of
    the success path."""
    def boom(p):
        raise exc("no")
    assert _call(tmp_path, boom) == []
