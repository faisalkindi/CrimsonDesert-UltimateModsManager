"""Tests for the Recovery Flow orchestrator (v3.1.9 plan, Task 2).

Uses pytest-qt. Focus: failure-containment and state transitions
around the Codex review findings. Tests exercise internal methods
(_begin_fix_everything, _on_reimport_finished, etc.) rather than
the full start() path, because the Steam Verify prompt is a modal
that needs real user interaction. QProcess wrangling for the real
workers is deliberately NOT exercised here -- that's the manual
smoke the user runs when home.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_db_with_mods(tmp_path: Path, specs: list[dict]):
    """Create a real Database with the given mod rows."""
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    for spec in specs:
        db.connection.execute(
            "INSERT INTO mods (name, mod_type, enabled, priority, source_path) "
            "VALUES (?, ?, ?, 0, ?)",
            (spec["name"], spec.get("type", "paz"),
             spec.get("enabled", 1), spec.get("source_path")))
    db.connection.commit()
    return db


def _make_mock_main_window(db, game_dir: Path, qtbot) -> MagicMock:
    """MagicMock main window that satisfies the orchestrator's
    attribute requirements."""
    from PySide6.QtWidgets import QWidget

    win = MagicMock()
    win._db = db
    win._game_dir = game_dir
    win._active_worker = None

    central = QWidget()
    qtbot.addWidget(central)
    win.centralWidget.return_value = central
    win._central_widget = central  # test-side handle
    return win


def test_freeze_and_thaw_main_window(qtbot, tmp_path):
    """Codex finding 8: the orchestrator disables the central widget
    during the chain so users cannot toggle mods under it."""
    from cdumm.gui.recovery_flow import RecoveryFlow

    db = _make_db_with_mods(tmp_path, [])
    win = _make_mock_main_window(db, tmp_path, qtbot)

    flow = RecoveryFlow(win)
    flow._freeze_main_window()
    assert win._central_widget.isEnabled() is False

    flow._thaw_main_window()
    assert win._central_widget.isEnabled() is True
    db.close()


def test_enter_cancelled_emits_chain_complete_and_thaws(qtbot, tmp_path):
    """Cancel on Steam Verify enters the cancelled terminal state and
    emits chain_complete. Central widget is thawed."""
    from cdumm.gui.recovery_flow import RecoveryFlow, STEP_CANCELLED

    db = _make_db_with_mods(tmp_path, [])
    win = _make_mock_main_window(db, tmp_path, qtbot)

    flow = RecoveryFlow(win)
    flow._freeze_main_window()  # mimic start() prelude

    with qtbot.waitSignal(flow.chain_complete, timeout=2000):
        flow._enter_cancelled()

    assert flow._current_step == STEP_CANCELLED
    assert win._central_widget.isEnabled() is True
    db.close()


def test_begin_fix_everything_routes_to_error_when_page_missing(qtbot, tmp_path):
    """Main window without fix_everything_page -> STEP_ERROR with a
    descriptive reason. Central widget thawed so user is not locked."""
    from cdumm.gui.recovery_flow import RecoveryFlow, STEP_ERROR

    db = _make_db_with_mods(tmp_path, [])
    win = _make_mock_main_window(db, tmp_path, qtbot)
    win.fix_everything_page = None

    flow = RecoveryFlow(win)

    with qtbot.waitSignal(flow.chain_error, timeout=2000) as sig:
        flow._freeze_main_window()
        flow._begin_fix_everything()

    assert flow._current_step == STEP_ERROR
    assert "Fix Everything" in sig.args[0]
    assert win._central_widget.isEnabled() is True
    db.close()


def test_begin_reimport_with_zero_candidates_enters_all_skipped(qtbot, tmp_path):
    """Codex finding 3: every enabled PAZ mod has a gone source_path
    AND no CDMods/sources/<id>/ fallback -> STEP_ALL_SKIPPED, not
    STEP_DONE, no apply. The skipped set is disabled first."""
    from cdumm.gui.recovery_flow import RecoveryFlow, STEP_ALL_SKIPPED

    gone = tmp_path / "gone"
    db = _make_db_with_mods(tmp_path, [
        {"name": "Orphan1", "source_path": None},
        {"name": "Stale2", "source_path": str(gone)},
    ])
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    win = _make_mock_main_window(db, game_dir, qtbot)

    flow = RecoveryFlow(win)

    with qtbot.waitSignal(flow.chain_complete, timeout=2000):
        flow._freeze_main_window()
        flow._begin_reimport()

    assert flow._current_step == STEP_ALL_SKIPPED
    # Codex finding 1: both mods disabled before terminal state.
    rows = db.connection.execute(
        "SELECT enabled FROM mods ORDER BY id").fetchall()
    assert all(r[0] == 0 for r in rows), (
        f"all skipped mods must be disabled; got {rows}")
    assert win._central_widget.isEnabled() is True
    win._on_apply.assert_not_called()
    db.close()


def test_reimport_finished_disables_skipped_before_apply(qtbot, tmp_path):
    """Codex findings 1 + 12: after reimport, unreimportable mods get
    disabled BEFORE apply runs. The remaining enabled set is what
    apply touches."""
    from cdumm.gui.recovery_flow import RecoveryFlow

    good_src = tmp_path / "good"; good_src.mkdir()
    gone_src = tmp_path / "gone"
    db = _make_db_with_mods(tmp_path, [
        {"name": "Good", "source_path": str(good_src)},
        {"name": "Stale", "source_path": str(gone_src)},
    ])
    game_dir = tmp_path / "game"; game_dir.mkdir()
    win = _make_mock_main_window(db, game_dir, qtbot)

    flow = RecoveryFlow(win)
    flow._skipped_mods = [{"id": 2, "name": "Stale"}]
    flow._reimportable_ids = [1]

    flow._begin_apply = MagicMock()
    flow._on_reimport_finished()

    disabled_row = db.connection.execute(
        "SELECT enabled FROM mods WHERE name = 'Stale'").fetchone()
    assert disabled_row[0] == 0, (
        "skipped mod MUST be disabled before apply runs")
    good_row = db.connection.execute(
        "SELECT enabled FROM mods WHERE name = 'Good'").fetchone()
    assert good_row[0] == 1, "reimportable mod stays enabled"

    flow._begin_apply.assert_called_once()
    db.close()


def test_reimport_finished_enters_all_skipped_if_no_enabled_mods_left(qtbot, tmp_path):
    """Codex finding 3 edge case: every mod was in skipped; after
    disable, 0 enabled PAZ mods remain -> STEP_ALL_SKIPPED, NOT apply."""
    from cdumm.gui.recovery_flow import RecoveryFlow, STEP_ALL_SKIPPED

    db = _make_db_with_mods(tmp_path, [
        {"name": "A", "source_path": None},
        {"name": "B", "source_path": None},
    ])
    game_dir = tmp_path / "game"; game_dir.mkdir()
    win = _make_mock_main_window(db, game_dir, qtbot)

    flow = RecoveryFlow(win)
    flow._skipped_mods = [
        {"id": 1, "name": "A"},
        {"id": 2, "name": "B"},
    ]
    flow._reimportable_ids = []
    flow._begin_apply = MagicMock()

    with qtbot.waitSignal(flow.chain_complete, timeout=2000):
        flow._on_reimport_finished()

    assert flow._current_step == STEP_ALL_SKIPPED
    flow._begin_apply.assert_not_called()
    db.close()


def test_reimport_finished_with_no_skipped_goes_straight_to_apply(qtbot, tmp_path):
    """Happy path: reimport cleared, nothing to disable, apply runs
    on the full reimportable set."""
    from cdumm.gui.recovery_flow import RecoveryFlow

    good_src = tmp_path / "good"; good_src.mkdir()
    db = _make_db_with_mods(tmp_path, [
        {"name": "Good", "source_path": str(good_src)},
    ])
    game_dir = tmp_path / "game"; game_dir.mkdir()
    win = _make_mock_main_window(db, game_dir, qtbot)

    flow = RecoveryFlow(win)
    flow._skipped_mods = []
    flow._reimportable_ids = [1]
    flow._begin_apply = MagicMock()

    flow._on_reimport_finished()

    flow._begin_apply.assert_called_once()
    row = db.connection.execute(
        "SELECT enabled FROM mods WHERE name = 'Good'").fetchone()
    assert row[0] == 1
    db.close()


def test_step_changed_signal_fires_on_transition(qtbot, tmp_path):
    """step_changed emits for every state transition so the UI can
    render the current phase."""
    from cdumm.gui.recovery_flow import RecoveryFlow, STEP_ERROR

    db = _make_db_with_mods(tmp_path, [])
    win = _make_mock_main_window(db, tmp_path, qtbot)
    win.fix_everything_page = None

    flow = RecoveryFlow(win)

    with qtbot.waitSignal(flow.step_changed, timeout=2000) as sig:
        flow._begin_fix_everything()

    # First transition is fix_everything; error follows. Either is
    # acceptable as the first captured emission.
    assert sig.args[0] in ("fix_everything", STEP_ERROR)
    db.close()
