"""HIGH #6: deriving pamt_dir from entry.paz_file must warn when empty.

Overlay entries keyed on an empty pamt_dir collide with each other in
the builder and/or land in the wrong PAMT directory at write-time.
Callers currently compute `Path(entry.paz_file).parent.name` inline,
and when the path had no parent (just a filename), the result was a
silent empty string.
"""
from __future__ import annotations

import logging

from cdumm.engine.json_patch_handler import _derive_pamt_dir


def test_normal_paz_path_returns_parent_name():
    assert _derive_pamt_dir("E:/Crimson Desert/0009/0.paz") == "0009"
    assert _derive_pamt_dir("C:/game/0000/1.paz") == "0000"


def test_bare_filename_returns_empty_and_warns(caplog):
    caplog.set_level(logging.WARNING, logger="cdumm.engine.json_patch_handler")
    result = _derive_pamt_dir("0.paz")
    assert result == "", "matches Path.parent.name fallback"
    assert any("empty pamt_dir" in r.getMessage().lower() or
               "0.paz" in r.getMessage()
               for r in caplog.records), (
        f"expected warning about empty pamt_dir; got "
        f"{[r.getMessage() for r in caplog.records]}")


def test_pathlib_path_input_works():
    from pathlib import Path
    assert _derive_pamt_dir(Path("E:/Crimson Desert/0002/0.paz")) == "0002"
