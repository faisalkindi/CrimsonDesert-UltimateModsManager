"""GitHub #65 (tbyk101 on v3.2.8, 2026-05-03): trying to uninstall
'Play Station Icons' shows the error

    Apply Failed: file not found: (unknown path) ([WinError 2] ...)

The literal '(unknown path)' is from the Bug C fix at
apply_engine.py around line 1024:

    path = getattr(e, 'filename', None) or '(unknown path)'

This means a FileNotFoundError reached the top-level handler
WITHOUT the .filename attribute populated. Python's
FileNotFoundError sets .filename only when constructed via the
3-arg form (errno, strerror, filename) — bare
``raise FileNotFoundError(message_string)`` leaves it as None.

papgt_manager.py:103 raises bare:

    raise FileNotFoundError(f"PAPGT not found: {base_path}")

That's the source of the (unknown path) message. The user can't
see WHICH PAPGT path is missing.

Fix: switch to the 3-arg form so e.filename carries the path.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_papgt_not_found_error_carries_filename(tmp_path: Path):
    """When PapgtManager.rebuild can't find any PAPGT base, the raised
    FileNotFoundError must carry .filename so the apply error message
    includes the missing path instead of '(unknown path)'."""
    from cdumm.archive.papgt_manager import PapgtManager

    game_dir = tmp_path / "game"
    vanilla_dir = tmp_path / "vanilla"
    # Both dirs exist but neither has a meta/0.papgt — same state
    # tbyk101 is in.
    (game_dir / "meta").mkdir(parents=True)
    (vanilla_dir / "meta").mkdir(parents=True)

    mgr = PapgtManager(game_dir, vanilla_dir)
    with pytest.raises(FileNotFoundError) as exc_info:
        mgr.rebuild({})

    e = exc_info.value
    assert e.filename is not None and e.filename != "", (
        f"PapgtManager raised FileNotFoundError without .filename "
        f"populated. e.filename = {e.filename!r}. The user-facing "
        f"apply error then renders '(unknown path)' instead of the "
        f"missing PAPGT path."
    )
    # The filename must point at a real path string, not be empty
    assert "papgt" in str(e.filename).lower() or "0.papgt" in str(e.filename), (
        f"Expected the missing PAPGT path in e.filename, got "
        f"{e.filename!r}"
    )
