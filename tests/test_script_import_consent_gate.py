"""Audit finding C1/import (2026-06-10): dropping an archive whose
content is a single .bat/.py used to auto-EXECUTE the script with the
user's full privileges and no consent prompt anywhere. Execution now
requires explicit consent (the GUI confirm dialog sets the worker's
--allow-scripts flag); without it the import returns a recognizable
refusal and the script must never run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.import_handler import (
    import_from_script,
    import_script_live,
    set_allow_scripts,
)
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.storage.database import Database


@pytest.fixture(autouse=True)
def _reset_consent_flag():
    set_allow_scripts(False)
    yield
    set_allow_scripts(False)


@pytest.fixture
def env(tmp_path: Path):
    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    deltas = tmp_path / "deltas"
    deltas.mkdir()
    db = Database(tmp_path / "t.db")
    db.initialize()
    snap = SnapshotManager(db)
    yield game_dir, db, snap, deltas
    db.close()


def _sentinel_script(tmp_path: Path) -> tuple[Path, Path]:
    sentinel = tmp_path / "executed.flag"
    script = tmp_path / "evil_mod.py"
    script.write_text(
        f"open(r'{sentinel}', 'w').write('ran')\n", encoding="utf-8")
    return script, sentinel


def test_script_import_refused_without_consent(env, tmp_path: Path):
    game_dir, db, snap, deltas = env
    script, sentinel = _sentinel_script(tmp_path)

    result = import_from_script(script, game_dir, db, snap, deltas)
    assert result.error, "refusal must carry an error message"
    assert result.needs_script_consent == "evil_mod.py"
    assert not sentinel.exists(), (
        "the script EXECUTED without consent")


def test_live_script_import_refused_without_consent(env, tmp_path: Path):
    game_dir, db, snap, deltas = env
    script, sentinel = _sentinel_script(tmp_path)

    result = import_script_live(script, game_dir, db, snap, deltas)
    assert result.error
    assert result.needs_script_consent == "evil_mod.py"
    assert not sentinel.exists(), (
        "the live script EXECUTED without consent")


def test_script_import_runs_with_consent(env, tmp_path: Path):
    """With the worker-level consent flag set (the GUI confirm path),
    the script actually executes in the sandbox."""
    game_dir, db, snap, deltas = env
    script, sentinel = _sentinel_script(tmp_path)

    set_allow_scripts(True)
    result = import_from_script(script, game_dir, db, snap, deltas)
    assert result.needs_script_consent is None
    assert sentinel.exists(), (
        "consented script did not run (gate too strict)")
