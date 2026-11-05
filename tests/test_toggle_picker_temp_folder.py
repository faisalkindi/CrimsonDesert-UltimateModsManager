"""#485: the drop-time toggle picker must import only the ticked
options, and its cleanup must never reach past its own temp folder.

Before the fix, the filtered JSON could not be written (the detector's
``_json_path`` is a ``Path``), so every option was imported. Its
cleanup slot also named the system temp folder itself, so making the
write succeed would have wiped the user's whole temp folder on the
next successful import. AgentKush found both.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import cdumm.gui.fluent_window as fw
import cdumm.gui.preset_picker as pp

_MOD = {
    "name": "Toggle Test",
    "patches": [{
        "game_file": "gamedata/test.pabgb",
        "changes": [
            {"offset": 0, "original": "00", "patched": "01",
             "label": "[Speed] faster"},
            {"offset": 4, "original": "00", "patched": "02",
             "label": "[Stamina] more"},
        ],
    }],
}


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Redirect the system temp folder into the test, and fence rmtree
    to it, so nothing here can touch the real temp folder."""
    root = tmp_path / "systemp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    real_rmtree = shutil.rmtree

    def fenced(p, *a, **k):
        assert Path(p).resolve().is_relative_to(tmp_path.resolve()), p
        return real_rmtree(p, *a, **k)

    monkeypatch.setattr(shutil, "rmtree", fenced)
    return root


def _drop(tmp_path: Path, monkeypatch, untick: str | None):
    """Run the real pre-checks on a loose JSON drop, with the picker
    accepting at once and ``untick`` left unticked."""
    src = tmp_path / "drop" / "Toggle Test.json"
    src.parent.mkdir()
    src.write_text(json.dumps(_MOD), encoding="utf-8")

    class _Picker:
        def __init__(self, data, parent=None):
            keys = {(0, i) for i, c in enumerate(data["patches"][0]["changes"])
                    if c["label"] != untick}
            # The real dialog keeps every top-level key, _json_path too.
            self.selected_data = pp._filter_patches_by_keys(data, keys)
            self.selected_labels = sorted(
                c["label"] for c in data["patches"][0]["changes"]
                if c["label"] != untick)

        def exec(self):
            return True

    monkeypatch.setattr(pp, "TogglePickerDialog", _Picker)
    launched: list[Path] = []
    win = SimpleNamespace(
        _active_worker=None,
        _game_dir=tmp_path / "game",
        _snapshot=SimpleNamespace(has_snapshot=lambda: True),
        _mod_manager=None,
        _process_next_import=lambda: None,
        _launch_import_worker=lambda p, mid=None: launched.append(p),
    )
    fw.CdummWindow._import_with_prechecks(win, src)
    assert len(launched) == 1
    return win, launched[0], src


def test_unticked_option_is_not_imported(tmp_path, monkeypatch, temp_root):
    win, sent, src = _drop(tmp_path, monkeypatch, untick="[Stamina] more")
    assert sent != src, "the worker got the original file, every option included"
    labels = [c["label"] for c in json.loads(sent.read_text("utf-8"))
              ["patches"][0]["changes"]]
    assert labels == ["[Speed] faster"]
    assert win._configurable_labels == ["[Speed] faster"]


def test_filtered_json_gets_its_own_folder_and_the_original_name(
        tmp_path, monkeypatch, temp_root):
    win, sent, _src = _drop(tmp_path, monkeypatch, untick=None)
    assert sent.name == "Toggle Test.json"
    assert sent.parent.parent == temp_root
    assert sent.parent.name.startswith("cdumm_toggle_")
    assert win._pending_tmp_cleanup == sent.parent
    assert "_json_path" not in json.loads(sent.read_text("utf-8"))


def test_cleanup_removes_only_the_picker_folder(tmp_path, monkeypatch, temp_root):
    other = temp_root / "another_app.tmp"
    other.write_text("keep")
    mei = temp_root / "_MEI12345" / "certifi"
    mei.mkdir(parents=True)
    (mei / "cacert.pem").write_text("keep")
    win, sent, _src = _drop(tmp_path, monkeypatch, untick=None)

    fw._remove_pending_tmp(win)

    assert not sent.parent.exists()
    assert other.read_text() == "keep"
    assert (mei / "cacert.pem").read_text() == "keep"
    assert win._pending_tmp_cleanup is None


@pytest.mark.parametrize("slot", ["root", "unrelated"])
def test_cleanup_refuses_anything_but_a_picker_folder(tmp_path, temp_root, slot):
    target = temp_root if slot == "root" else temp_root / "someone_elses"
    target.mkdir(exist_ok=True)
    (target / "file.txt").write_text("keep")
    win = SimpleNamespace(_pending_tmp_cleanup=target)

    fw._remove_pending_tmp(win)

    assert (target / "file.txt").read_text() == "keep"
    assert win._pending_tmp_cleanup is None
