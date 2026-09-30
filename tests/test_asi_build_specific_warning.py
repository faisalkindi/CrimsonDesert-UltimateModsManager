"""After installing an ASI plugin, say that it is tied to one game build,
and which build is installed.

GitHub #435 (woowoots), correcting #429. The first version of this note
said most ASI plugins target the Steam exe and to look for a Game Pass
build. That was wrong. QuickSlotLockFilter refused with:

    exe size differs from build 1.0.0.2850

and it refuses that way on ANY exe that is not the one it was built for.
A Steam install reports FileVersion 1.0.0.2976 (build 25477059,
2026-09-25), so the same plugin would refuse there too. The store the
game came from was never the cause; the game having moved past the build
the plugin was made for is.

So the note now fires for every ASI install rather than only Store ones,
names the installed build so it can be compared with what the mod page
asks for, and points at the plugin's own log in bin64, which is where
QuickSlotLockFilter had written the reason all along.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cdumm.i18n import load as load_translations
from cdumm.i18n import tr

pytest.importorskip("pytestqt")


@pytest.fixture(autouse=True)
def _english_strings():
    """tr() falls back to the key when nothing is loaded, which would make
    the build-number assertions pass against the key name rather than the
    real sentence. Load English so they test the shipped text."""
    load_translations("en")

SRC = (Path(__file__).resolve().parents[1]
       / "src" / "cdumm" / "gui" / "fluent_window.py")
KEYS = ("infobar.asi_build_specific",
        "infobar.asi_build_specific_msg",
        "infobar.asi_build_specific_msg_nobuild")


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_both_asi_install_success_paths_call_the_note():
    src = _src()
    i = src.find('content=f"{asi_count} ASI plugin(s) installed."')
    assert i != -1
    assert "_warn_asi_build_specific()" in src[i:i + 400]
    j = src.find('title=tr("infobar.asi_installed")')
    assert j != -1
    assert "_warn_asi_build_specific()" in src[j:j + 500]


def test_the_store_specific_wording_is_gone():
    """The retracted claim must not survive anywhere in the shipped app.

    #429 told users to look for a Game Pass build, which is not the fix
    for a build mismatch and sent the reporter chasing the wrong thing.
    """
    assert "asi_store_install" not in _src()
    tdir = SRC.parents[1] / "translations"
    for f in sorted(tdir.glob("*.json")):
        raw = f.read_text(encoding="utf-8")
        assert "asi_store_install" not in raw, f.name


def test_the_note_strings_exist_in_every_locale():
    tdir = SRC.parents[1] / "translations"
    for f in sorted(tdir.glob("*.json")):
        d = json.loads(raw := f.read_text(encoding="utf-8"))
        del raw
        for k in KEYS:
            assert k in d, (f.name, k)
        # The build number is the actionable part; it must survive
        # translation, and only in the variant that has one to show.
        assert "{build}" in d[KEYS[1]], f.name
        assert "{build}" not in d[KEYS[2]], f.name
        for tok in ("ASI", "bin64"):
            assert tok in d[KEYS[1]], (f.name, tok)


def _call(game_dir, version):
    """Run the real method unbound on a stand-in self, capturing InfoBars."""
    from cdumm.gui.fluent_window import CdummWindow
    fake_self = SimpleNamespace(_game_dir=game_dir)
    with patch("cdumm.gui.fluent_window.InfoBar") as bar, \
            patch("cdumm.engine.version_detector.read_exe_file_version",
                  version):
        CdummWindow._warn_asi_build_specific(fake_self)
    return bar.warning.call_args_list


def test_the_note_names_the_installed_build(tmp_path):
    calls = _call(tmp_path, lambda p: "1.0.0.2976")
    assert len(calls) == 1
    kw = calls[0].kwargs
    assert kw["title"] == tr("infobar.asi_build_specific")
    assert "1.0.0.2976" in kw["content"]


def test_the_note_still_fires_when_the_build_cannot_be_read(tmp_path):
    """Advice, not a gate: an unreadable version resource must not
    silence the note, only drop the build number from it."""
    calls = _call(tmp_path, lambda p: None)
    assert len(calls) == 1
    assert calls[0].kwargs["content"] == tr(
        "infobar.asi_build_specific_msg_nobuild")


def test_no_game_dir_still_notes_without_a_build():
    calls = _call(None, lambda p: None)
    assert len(calls) == 1
    assert calls[0].kwargs["content"] == tr(
        "infobar.asi_build_specific_msg_nobuild")


def test_it_fires_regardless_of_store(tmp_path):
    """The whole correction: this is not conditional on the install source.

    If someone reintroduces an is_xbox_install() gate here, a Steam user
    hitting the exact same build mismatch gets told nothing again.
    """
    with patch("cdumm.storage.game_finder.is_xbox_install",
               lambda p: False):
        calls = _call(tmp_path, lambda p: "1.0.0.2976")
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [ImportError, OSError, RuntimeError])
def test_a_failing_version_read_never_blocks_the_install(tmp_path, exc):
    def boom(p):
        raise exc("no")
    calls = _call(tmp_path, boom)
    assert len(calls) == 1
    assert calls[0].kwargs["content"] == tr(
        "infobar.asi_build_specific_msg_nobuild")
