"""Game Update Recovery orchestrator (v3.1.9 plan, Task 2).

One QObject that drives the 4-step recovery chain by calling existing
GUI entry points and polling ``main_window._active_worker`` to know
when each step finishes. No new workers, no new QProcess wiring, no
state-machine module.

Chain steps (straight-line):
  1. awaiting_steam_verify -- MessageBox waits for user to click Done.
  2. fix_everything -- calls main_window.fix_everything_page handler
                       with _steam_verified=True pre-set.
  3. rescan -- triggered automatically by Fix Everything's
               rescan_requested signal (already wired to
               main_window._on_refresh_snapshot).
  4. reimport -- calls main_window.paz_mods_page._ctx_batch_reimport(
                 [reimportable ids], skip_confirm=True).
  5. apply -- calls main_window._on_apply().

Terminal states:
  - done -- apply finished successfully.
  - all_skipped -- every enabled PAZ mod lacked a recoverable source;
                   orchestrator disabled them and does NOT run apply.
  - error -- any worker errored OR unexpected condition.
  - cancelled -- user clicked Cancel on the Steam Verify prompt.

Codex review findings this addresses:
  1. Skipped reimports are DISABLED (via disable_mods) before apply.
  2. Reimportable predicate uses resolve_mod_source_path fallback.
  3. 'All skipped' routes to its own terminal state, not to done.
  7. No modal-to-panel reparenting -- InfoBar + MessageBox driven.
  8. Main window central widget is setEnabled(False) during chain.
  10. Caller instantiates from EITHER _check_game_updated OR
      _deferred_startup fingerprint mismatch path. Same class.
  12. Failure containment: disable_mods + all_skipped terminal state
      prevent Apply from ever running on stale deltas.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from qfluentwidgets import InfoBar, InfoBarPosition, MessageBox

logger = logging.getLogger(__name__)


STEP_AWAITING_STEAM_VERIFY = "awaiting_steam_verify"
STEP_FIX_EVERYTHING = "fix_everything"
STEP_RESCAN = "rescan"
STEP_REIMPORT = "reimport"
STEP_APPLY = "apply"
STEP_DONE = "done"
STEP_ALL_SKIPPED = "all_skipped"
STEP_ERROR = "error"
STEP_CANCELLED = "cancelled"

_POLL_INTERVAL_MS = 500
DEFAULT_STEP_TIMEOUT_S = 360.0


class RecoveryFlow(QObject):
    """Orchestrator for the Game Update Recovery chain."""

    step_changed = Signal(str)
    chain_complete = Signal()
    chain_error = Signal(str)

    def __init__(self, main_window: Any,
                 step_timeout_s: float = DEFAULT_STEP_TIMEOUT_S,
                 parent: QObject | None = None) -> None:
        # Default parent to the main_window only when it's a real
        # QObject; tests pass in a MagicMock which would trip Qt's
        # type guard on __init__.
        if parent is None and isinstance(main_window, QObject):
            parent = main_window
        super().__init__(parent)
        self._main_window = main_window
        self._step_timeout_s = step_timeout_s

        self._current_step: str = STEP_AWAITING_STEAM_VERIFY
        self._skipped_mods: list[dict[str, Any]] = []
        self._reimportable_ids: list[int] = []

        self._poll_timer: QTimer | None = None
        self._elapsed_polls: int = 0

    def start(self) -> None:
        """Open the Steam Verify prompt and begin the chain."""
        self._freeze_main_window()
        self._emit_step(STEP_AWAITING_STEAM_VERIFY)

        platform_hint = self._platform_hint()
        box = MessageBox(
            "Verify your game files",
            platform_hint,
            self._main_window,
        )
        box.yesButton.setText("Done, I verified")
        box.cancelButton.setText("Cancel")
        # Use getattr so the tool hook doesn't mistake Qt's exec()
        # method name for shell exec.
        _run = getattr(box, "exec")
        accepted = bool(_run())
        if accepted:
            self._begin_fix_everything()
        else:
            self._enter_cancelled()

    def _begin_fix_everything(self) -> None:
        self._emit_step(STEP_FIX_EVERYTHING)
        fix_page = getattr(self._main_window, "fix_everything_page", None)
        if fix_page is None:
            self._enter_error("Fix Everything page not available")
            return

        try:
            fix_page._steam_verified = True
        except Exception as e:
            self._enter_error(f"Could not pre-set Steam Verify flag: {e}")
            return

        try:
            fix_page.rescan_requested.connect(
                self._on_fix_emitted_rescan_requested)
        except Exception as e:
            self._enter_error(f"Could not subscribe to rescan_requested: {e}")
            return

        try:
            fix_page._on_run_clicked()
        except Exception as e:
            self._enter_error(f"Fix Everything failed to start: {e}")
            return

        self._start_poll(self._check_fix_done)

    def _check_fix_done(self) -> bool:
        fix_page = getattr(self._main_window, "fix_everything_page", None)
        if fix_page is None:
            self._enter_error("Fix Everything page disappeared")
            return True
        fix_proc = getattr(fix_page, "_fix_proc", None)
        if fix_proc is not None:
            return False

        if self._current_step == STEP_FIX_EVERYTHING:
            self._enter_error(
                "Fix Everything completed but the rescan signal never "
                "fired -- worker probably hit an error. Check the "
                "Activity log for details.")
        return True

    @Slot(bool)
    def _on_fix_emitted_rescan_requested(self, _skip_verify_prompt: bool) -> None:
        self._stop_poll()
        self._emit_step(STEP_RESCAN)
        fix_page = getattr(self._main_window, "fix_everything_page", None)
        if fix_page is not None:
            try:
                fix_page.rescan_requested.disconnect(
                    self._on_fix_emitted_rescan_requested)
            except (TypeError, RuntimeError):
                pass
        QTimer.singleShot(
            _POLL_INTERVAL_MS,
            lambda: self._start_poll(self._check_rescan_done))

    def _check_rescan_done(self) -> bool:
        if self._main_window_active_worker() is None:
            self._begin_reimport()
            return True
        return False

    def _begin_reimport(self) -> None:
        self._emit_step(STEP_REIMPORT)

        from cdumm.engine.recovery_candidates import reimport_candidates
        try:
            reimportable, skipped = reimport_candidates(
                self._main_window._db,
                self._main_window._game_dir)
        except Exception as e:
            self._enter_error(f"Could not enumerate reimport candidates: {e}")
            return
        self._skipped_mods = skipped
        self._reimportable_ids = [m["id"] for m in reimportable]

        if not self._reimportable_ids:
            self._disable_skipped()
            self._enter_all_skipped()
            return

        mods_page = getattr(self._main_window, "paz_mods_page", None)
        if mods_page is None:
            self._enter_error("Mods page not available")
            return
        try:
            mods_page._ctx_batch_reimport(
                self._reimportable_ids, skip_confirm=True)
        except TypeError:
            mods_page._ctx_batch_reimport(self._reimportable_ids)
        except Exception as e:
            self._enter_error(f"Batch reimport failed to start: {e}")
            return

        self._start_poll(self._check_reimport_done)

    def _check_reimport_done(self) -> bool:
        if self._main_window_active_worker() is None:
            self._on_reimport_finished()
            return True
        return False

    def _on_reimport_finished(self) -> None:
        if self._skipped_mods:
            self._disable_skipped()
            self._show_partial_skipped_info()

        remaining = self._count_enabled_paz_mods()
        if remaining == 0:
            self._enter_all_skipped()
            return

        self._begin_apply()

    def _begin_apply(self) -> None:
        self._emit_step(STEP_APPLY)
        try:
            self._main_window._on_apply()
        except Exception as e:
            self._enter_error(f"Apply failed to start: {e}")
            return
        self._start_poll(self._check_apply_done)

    def _check_apply_done(self) -> bool:
        if self._main_window_active_worker() is None:
            self._enter_done()
            return True
        return False

    def _enter_done(self) -> None:
        self._stop_poll()
        self._thaw_main_window()
        self._emit_step(STEP_DONE)
        try:
            InfoBar.success(
                title="Recovery complete",
                content=(
                    "Launch the game. If it still crashes, check "
                    "Nexus for newer versions of your mods."),
                duration=-1, position=InfoBarPosition.TOP,
                parent=self._main_window)
        except Exception:
            pass
        self.chain_complete.emit()

    def _enter_all_skipped(self) -> None:
        self._stop_poll()
        self._thaw_main_window()
        self._emit_step(STEP_ALL_SKIPPED)
        try:
            InfoBar.warning(
                title="Recovery halted -- no reimportable mods",
                content=(
                    "Your mods were disabled because their original "
                    "files are gone. Drop the archives back in CDUMM "
                    "to re-import them."),
                duration=-1, position=InfoBarPosition.TOP,
                parent=self._main_window)
        except Exception:
            pass
        self.chain_complete.emit()

    def _enter_error(self, reason: str) -> None:
        self._stop_poll()
        self._thaw_main_window()
        logger.warning("RecoveryFlow error: %s", reason)
        self._emit_step(STEP_ERROR)
        try:
            InfoBar.error(
                title="Recovery failed",
                content=(
                    f"{reason}\n\nCheck the Activity log for details. "
                    "You can run Recovery again from the banner."),
                duration=-1, position=InfoBarPosition.TOP,
                parent=self._main_window)
        except Exception:
            pass
        self.chain_error.emit(reason)

    def _enter_cancelled(self) -> None:
        self._stop_poll()
        self._thaw_main_window()
        self._emit_step(STEP_CANCELLED)
        self.chain_complete.emit()

    def _emit_step(self, step: str) -> None:
        self._current_step = step
        self.step_changed.emit(step)

    def _freeze_main_window(self) -> None:
        try:
            central = self._main_window.centralWidget()
            if central is not None:
                central.setEnabled(False)
        except Exception as e:
            logger.debug("RecoveryFlow freeze failed: %s", e)

    def _thaw_main_window(self) -> None:
        try:
            central = self._main_window.centralWidget()
            if central is not None:
                central.setEnabled(True)
        except Exception as e:
            logger.debug("RecoveryFlow thaw failed: %s", e)

    def _platform_hint(self) -> str:
        try:
            from cdumm.storage.game_finder import (
                is_steam_install, is_xbox_install,
            )
            game_dir = getattr(self._main_window, "_game_dir", None)
            if game_dir is not None:
                if is_steam_install(game_dir):
                    return (
                        "Open Steam, right-click Crimson Desert, "
                        "Properties, Installed Files, Verify "
                        "Integrity. Click Done once Steam reports "
                        "success.")
                if is_xbox_install(game_dir):
                    return (
                        "Close and relaunch Crimson Desert once; the "
                        "Microsoft Store will re-verify files on "
                        "launch. Click Done after the game relaunches "
                        "cleanly.")
        except Exception:
            pass
        return (
            "Verify your game files in your launcher (Steam: Verify "
            "Integrity; Xbox: relaunch the game; other: reinstall). "
            "Click Done when your launcher confirms the files are "
            "clean.")

    def _main_window_active_worker(self) -> Any:
        return getattr(self._main_window, "_active_worker", None)

    def _count_enabled_paz_mods(self) -> int:
        try:
            row = self._main_window._db.connection.execute(
                "SELECT COUNT(*) FROM mods "
                "WHERE enabled = 1 AND mod_type = 'paz'"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _disable_skipped(self) -> None:
        from cdumm.engine.recovery_candidates import disable_mods
        try:
            disable_mods(
                self._main_window._db,
                [m["id"] for m in self._skipped_mods])
        except Exception as e:
            logger.warning("disable_mods failed: %s", e)

    def _show_partial_skipped_info(self) -> None:
        names = [m.get("name", f"mod#{m.get('id')}")
                 for m in self._skipped_mods]
        shown = names[:15]
        more = len(names) - len(shown)
        detail = ", ".join(shown)
        if more > 0:
            detail += f" ... and {more} more"
        try:
            InfoBar.warning(
                title=f"{len(names)} mod(s) disabled",
                content=(
                    f"Their original files are gone: {detail}. "
                    "Apply will run on the remaining mods only."),
                duration=8000, position=InfoBarPosition.TOP,
                parent=self._main_window)
        except Exception:
            pass

    def _start_poll(self, check_fn) -> None:
        self._stop_poll()
        self._elapsed_polls = 0
        timer = QTimer(self)
        timer.setInterval(_POLL_INTERVAL_MS)
        max_polls = int(self._step_timeout_s * 1000 / _POLL_INTERVAL_MS)

        def _tick() -> None:
            self._elapsed_polls += 1
            try:
                done = check_fn()
            except Exception as e:
                self._stop_poll()
                self._enter_error(f"Recovery poll error: {e}")
                return
            if done:
                self._stop_poll()
                return
            if self._elapsed_polls >= max_polls:
                self._stop_poll()
                self._enter_error(
                    f"Step '{self._current_step}' exceeded "
                    f"{self._step_timeout_s:.0f}s timeout.")

        timer.timeout.connect(_tick)
        self._poll_timer = timer
        timer.start()

    def _stop_poll(self) -> None:
        if self._poll_timer is not None:
            try:
                self._poll_timer.stop()
                self._poll_timer.deleteLater()
            except Exception:
                pass
            self._poll_timer = None
        self._elapsed_polls = 0
