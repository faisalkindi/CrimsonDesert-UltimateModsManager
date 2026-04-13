"""Binary search wizard dialog for finding problem mods.

Supports two modes:
  Manual: User launches game and reports crash/ok each round.
  Auto: CDUMM launches game through Steam, monitors for crash, and
        feeds results to the ddmin algorithm automatically.
"""

import logging
import math
import time
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QStackedWidget, QTextBrowser, QWidget, QMessageBox,
    QProgressBar,
)

from cdumm.engine.apply_engine import ApplyWorker
from cdumm.engine.binary_search import DeltaDebugSession
from cdumm.engine.mod_manager import ModManager
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class BinarySearchDialog(QDialog):
    def __init__(self, mod_manager: ModManager, game_dir: Path,
                 vanilla_dir: Path, db: Database, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find Problem Mod")
        self.setMinimumSize(550, 450)
        self.resize(600, 500)

        self._mm = mod_manager
        self._game_dir = game_dir
        self._auto_running = False
        self._vanilla_dir = vanilla_dir
        self._db = db

        # Scan ASI plugins and inject them as fake mods with negative IDs
        self._asi_mods: dict[int, dict] = {}  # fake_id -> {name, plugin}
        try:
            from cdumm.asi.asi_manager import AsiManager
            bin64 = game_dir / "bin64"
            if bin64.exists():
                asi_mgr = AsiManager(bin64)
                plugins = asi_mgr.scan()
                for i, p in enumerate(plugins):
                    if p.enabled:
                        fake_id = -(i + 1)  # negative IDs for ASI mods
                        self._asi_mods[fake_id] = {
                            "id": fake_id,
                            "name": f"[ASI] {p.name}",
                            "enabled": True,
                            "mod_type": "asi",
                            "_plugin": p,
                        }
        except Exception as e:
            logger.debug("ASI scan for bisect failed: %s", e)

        self._session = DeltaDebugSession(mod_manager, extra_mods=list(self._asi_mods.values()))

        self._pages = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self._pages)

        self._build_intro_page()
        self._pages.setCurrentIndex(0)

    def closeEvent(self, event):
        if self._auto_running:
            event.ignore()  # can't close during auto bisect — use Stop button
            return
        super().closeEvent(event)

    def _has_saved_progress(self) -> bool:
        """Check if there's a saved ddmin session that matches current mods."""
        try:
            import json
            row = self._db.connection.execute(
                "SELECT data FROM ddmin_progress WHERE id = 1").fetchone()
            if not row:
                return False
            saved = json.loads(row[0])
            # Check if same mods are enabled
            saved_ids = set(saved.get("all_ids", []))
            current_ids = {m["id"] for m in self._session.enabled_mods}
            return saved_ids == current_ids
        except Exception:
            return False

    def _load_progress(self):
        """Load saved ddmin state."""
        import json
        row = self._db.connection.execute(
            "SELECT data FROM ddmin_progress WHERE id = 1").fetchone()
        if row:
            saved = json.loads(row[0])
            s = self._session
            s._changes = saved["changes"]
            s._n = saved["n"]
            s._partition_index = saved["partition_index"]
            s._testing_complement = saved["testing_complement"]
            s.round_number = saved["round_number"]
            s.history = saved["history"]
            s.phase = saved["phase"]

    def _save_progress(self):
        """Save current ddmin state to DB."""
        try:
            import json
            s = self._session
            data = json.dumps({
                "all_ids": s.all_ids,
                "changes": s._changes,
                "n": s._n,
                "partition_index": s._partition_index,
                "testing_complement": s._testing_complement,
                "round_number": s.round_number,
                "history": s.history,
                "phase": s.phase,
            })
            self._db.connection.execute(
                "CREATE TABLE IF NOT EXISTS ddmin_progress "
                "(id INTEGER PRIMARY KEY, data TEXT)")
            self._db.connection.execute(
                "INSERT OR REPLACE INTO ddmin_progress (id, data) VALUES (1, ?)",
                (data,))
            self._db.connection.commit()
        except Exception as e:
            logger.debug("Failed to save ddmin progress: %s", e)

    def _clear_progress(self):
        """Clear saved ddmin state."""
        try:
            self._db.connection.execute(
                "CREATE TABLE IF NOT EXISTS ddmin_progress "
                "(id INTEGER PRIMARY KEY, data TEXT)")
            self._db.connection.execute("DELETE FROM ddmin_progress")
            self._db.connection.commit()
        except Exception:
            pass

    def _build_intro_page(self):
        page = QVBoxLayout()
        w = QWidget()
        page.setContentsMargins(16, 16, 16, 16)
        page.setSpacing(10)
        w.setLayout(page)

        title = QLabel("Find Problem Mod")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #D4A43C;")
        page.addWidget(title)

        n = len(self._session.enabled_mods)
        best = max(1, 2 * math.ceil(math.log2(max(n, 2)))) if n > 1 else 1
        page.addWidget(QLabel(
            f"CDUMM will automatically test your {n} enabled mods to find\n"
            f"which one(s) are causing crashes.\n\n"
            f"It launches the game through Steam, monitors for crashes,\n"
            f"and repeats until all problem mods are identified.\n\n"
            f"Estimated: {best}-{best * 3} rounds per problem mod (~90s each).\n\n"
            f"Do not interact with the game during testing.\n"
            f"Your mod configuration will be restored when finished."
        ))

        mod_list = QListWidget()
        for m in self._session.enabled_mods:
            mod_list.addItem(m["name"])
        mod_list.setMaximumHeight(150)
        page.addWidget(mod_list)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        auto_btn = QPushButton("Find Problem Mods")
        auto_btn.setStyleSheet("font-weight: bold; padding: 6px 20px; background: #D4A43C;")
        auto_btn.setToolTip("CDUMM launches the game, detects crashes, and finds all problem mods automatically")
        auto_btn.clicked.connect(self._start_auto_bisect)
        btn_row.addWidget(auto_btn)

        page.addLayout(btn_row)

        if self._has_saved_progress():
            import json
            row = self._db.connection.execute(
                "SELECT data FROM ddmin_progress WHERE id = 1").fetchone()
            if row:
                saved = json.loads(row[0])
                hint = QLabel(
                    f"Previous search: Round {saved['round_number']}, "
                    f"{len(saved['changes'])} mods remaining")
                hint.setStyleSheet("color: #D4A43C; font-size: 11px;")
                page.addWidget(hint)

        self._pages.addWidget(w)

    # ── Auto Bisect Mode ──────────────────────────────────────────

    def _start_auto_bisect(self):
        """Launch fully automated bisection."""
        from cdumm.engine.game_monitor import find_game_process
        if find_game_process():
            QMessageBox.warning(self, "Game Running",
                                "Close Crimson Desert before starting auto bisection.")
            return

        n = len(self._session.enabled_mods)
        if n < 2:
            QMessageBox.information(self, "Not Enough Mods",
                                   "Need at least 2 enabled mods to bisect.")
            return

        # Build auto page if not already built
        if not hasattr(self, "_auto_page"):
            self._build_auto_page()

        self._clear_progress()
        self._session = DeltaDebugSession(self._mm, extra_mods=list(self._asi_mods.values()))
        self._auto_log_browser.setHtml("")
        self._auto_stop_btn.setEnabled(True)

        estimated = max(1, 2 * math.ceil(math.log2(n)))
        self._auto_progress.setRange(0, estimated * 3)
        self._auto_progress.setValue(0)
        self._auto_status.setText(f"Starting auto bisection ({n} mods, ~{estimated} rounds)...")
        self._pages.setCurrentWidget(self._auto_page)

        self._auto_running = True

        # Launch worker on thread pool
        worker = _AutoBisectWorker(
            self._session, self._mm, self._game_dir,
            self._vanilla_dir, self._db,
            asi_mods=self._asi_mods)
        worker.signals.log.connect(self._on_auto_log)
        worker.signals.progress.connect(self._on_auto_progress)
        worker.signals.finished.connect(self._on_auto_finished)
        worker.signals.error.connect(self._on_auto_error)
        self._auto_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _build_auto_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Auto Find Problem Mod")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #D4A43C;")
        layout.addWidget(title)

        self._auto_status = QLabel("Initializing...")
        self._auto_status.setWordWrap(True)
        layout.addWidget(self._auto_status)

        self._auto_progress = QProgressBar()
        layout.addWidget(self._auto_progress)

        hint = QLabel("Do not interact with the game — CDUMM will launch and close it automatically.")
        hint.setStyleSheet("color: #FF9800; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._auto_log_browser = QTextBrowser()
        self._auto_log_browser.setStyleSheet(
            "QTextBrowser { background: #111111; border: 1px solid #2E3440; "
            "border-radius: 6px; padding: 6px; font-size: 11px; }")
        layout.addWidget(self._auto_log_browser)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._auto_stop_btn = QPushButton("Stop")
        self._auto_stop_btn.setStyleSheet("padding: 6px 20px; background: #BF616A;")
        self._auto_stop_btn.clicked.connect(self._stop_auto_bisect)
        btn_row.addWidget(self._auto_stop_btn)
        layout.addLayout(btn_row)

        self._auto_page = page
        self._pages.addWidget(page)

    @Slot(str)
    def _on_auto_log(self, msg):
        self._auto_log_browser.append(msg)

    @Slot(int, int)
    def _on_auto_progress(self, current, total):
        self._auto_progress.setRange(0, max(total, 1))
        self._auto_progress.setValue(current)
        self._auto_status.setText(f"Round {current} of ~{total}")

    @Slot(dict)
    def _on_auto_finished(self, result):
        self._auto_running = False
        self._auto_stop_btn.setEnabled(False)
        self._clear_progress()

        minimal = result.get("minimal_set", [])
        if not minimal:
            self._auto_status.setText("No problem mods found — all mods appear compatible.")
            self._auto_log_browser.append("\n✓ All mods passed. No crash detected.")
        elif len(minimal) == 1:
            name = minimal[0]["name"]
            self._auto_status.setText(f"Found it: {name}")
            self._auto_log_browser.append(f"\n★ CULPRIT: {name}")
            self._auto_log_browser.append(
                f"Found in {result['rounds']} rounds. "
                f"Culprit disabled. Other mods restored.")
        else:
            names = ", ".join(m["name"] for m in minimal)
            self._auto_status.setText(f"Found {len(minimal)} problem mods: {names}")
            self._auto_log_browser.append(f"\n★ PROBLEM MODS: {names}")
            self._auto_log_browser.append(
                f"All {len(minimal)} culprits disabled. Other mods restored.")

        # Show action buttons — clean up any previous dynamic buttons first
        self._auto_stop_btn.hide()
        for old_btn in getattr(self, "_dynamic_buttons", []):
            old_btn.deleteLater()
        self._dynamic_buttons = []

        btn_row = self._auto_stop_btn.parent().layout() if self._auto_stop_btn.parent() else None

        copy_btn = QPushButton("Copy Report")
        copy_btn.setStyleSheet("padding: 6px 20px;")
        copy_btn.clicked.connect(self._copy_crash_report)
        if btn_row:
            btn_row.addWidget(copy_btn)
        self._dynamic_buttons.append(copy_btn)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet("padding: 6px 20px; background: #D4A43C;")
        close_btn.clicked.connect(self.accept)
        if btn_row:
            btn_row.addWidget(close_btn)
        self._dynamic_buttons.append(close_btn)

    def _copy_crash_report(self):
        """Copy the crash registry report to clipboard."""
        from PySide6.QtWidgets import QApplication
        report = self._mm.get_crash_report()
        QApplication.clipboard().setText(report)
        self._auto_status.setText("Report copied to clipboard!")

    @Slot(str)
    def _on_auto_error(self, msg):
        self._auto_running = False
        self._auto_stop_btn.setEnabled(False)
        self._auto_status.setText(f"Error: {msg}")
        self._auto_log_browser.append(f"\n✗ ERROR: {msg}")

    def _stop_auto_bisect(self):
        if hasattr(self, "_auto_worker"):
            self._auto_worker.cancel()
            self._auto_status.setText("Stopping... killing game and restoring mods...")
            self._auto_stop_btn.setText("Stopping...")
            self._auto_stop_btn.setEnabled(False)
            # Kill game immediately so the worker unblocks from wait_for_exit
            import threading
            def _kill_game():
                try:
                    from cdumm.engine.game_monitor import find_game_process, kill_process
                    pid = find_game_process()
                    if pid:
                        kill_process(pid)
                except Exception:
                    pass
            threading.Thread(target=_kill_game, daemon=True).start()


class _AutoBisectSignals(QObject):
    """Thread-safe signals for the auto bisect worker."""
    log = Signal(str)
    progress = Signal(int, int)
    finished = Signal(dict)
    error = Signal(str)


class _AutoBisectWorker(QRunnable):
    """Runs the full automated bisection loop on a background thread."""

    def __init__(self, session: DeltaDebugSession, mm: ModManager,
                 game_dir: Path, vanilla_dir: Path, db: Database,
                 asi_mods: dict[int, dict] | None = None):
        super().__init__()
        self.signals = _AutoBisectSignals()
        self._session = session
        self._mm = mm
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir
        self._db = db
        self._cancelled = False
        self._asi_mods = asi_mods or {}  # fake_id -> {name, _plugin}
        self._asi_manager = None
        if self._asi_mods:
            try:
                from cdumm.asi.asi_manager import AsiManager
                self._asi_manager = AsiManager(game_dir / "bin64")
            except Exception:
                pass

    def cancel(self):
        self._cancelled = True

    def _set_mod_enabled(self, mod_id: int, enabled: bool, thread_mm: ModManager) -> None:
        """Toggle a mod — handles both PAZ mods (DB) and ASI mods (file rename)."""
        if mod_id < 0 and mod_id in self._asi_mods and self._asi_manager:
            plugin = self._asi_mods[mod_id].get("_plugin")
            if plugin:
                try:
                    # Re-scan to get current file state (path may have changed)
                    for p in self._asi_manager.scan():
                        if p.name == plugin.name:
                            plugin = p
                            break
                    if enabled:
                        self._asi_manager.enable(plugin)
                    else:
                        self._asi_manager.disable(plugin)
                except OSError as e:
                    self.signals.log.emit(f"  Warning: failed to toggle ASI {plugin.name}: {e}")
        else:
            thread_mm.set_enabled(mod_id, enabled)

    @Slot()
    def run(self):
        from cdumm.engine.game_monitor import launch_and_test

        # Open own DB connection — SQLite objects can't cross threads
        db_path = self._game_dir / "CDMods" / "cdumm.db"
        thread_db = Database(db_path)
        thread_db.initialize()
        thread_mm = ModManager(thread_db, self._game_dir / "CDMods" / "deltas")

        all_culprits = []  # accumulate (mod_id, mod_name) tuples
        original_state = dict(self._session.original_state)
        total_rounds = 0

        try:
            # Outer loop: find culprit → disable → re-test → repeat until stable
            while not self._cancelled:
                enabled_mods = [m for m in thread_mm.list_mods() if m["enabled"]]
                n = len(enabled_mods)

                if n < 2:
                    self.signals.log.emit("Less than 2 mods remaining — done.")
                    break

                estimated = max(1, 2 * math.ceil(math.log2(n)))
                self.signals.log.emit(
                    f"\n{'═' * 40}")
                if all_culprits:
                    self.signals.log.emit(
                        f"Re-testing {n} remaining mods (already found: "
                        f"{', '.join(name for _, name in all_culprits)})...")
                else:
                    self.signals.log.emit(
                        f"Testing {n} enabled mods...")
                self.signals.log.emit(f"{'═' * 40}")

                # First: verify crash still happens with current mod set
                self.signals.log.emit("\nVerifying crash reproduces...")
                # Enable all remaining, apply, test
                for m in enabled_mods:
                    self._set_mod_enabled(m["id"], True, thread_mm)
                verify_errors = []
                worker = ApplyWorker(self._game_dir, self._vanilla_dir,
                                     db_path, force_outdated=True)
                worker.error_occurred.connect(lambda e: verify_errors.append(e))
                worker.run()

                if verify_errors:
                    self.signals.log.emit(f"  ✗ Apply failed during verification: {verify_errors[0]}")
                    self.signals.log.emit("  Continuing anyway — this may be a transient error.")
                    # Don't break — try launching anyway, the game might still work

                if self._cancelled:
                    break

                crashed = launch_and_test(
                    self._game_dir, stable_seconds=90, launch_timeout=60,
                    log_cb=lambda msg: self.signals.log.emit(f"  {msg}"),
                    cancel_check=lambda: self._cancelled)

                if self._cancelled:
                    break

                if not crashed:
                    self.signals.log.emit("✓ Game is stable — no more crashes!")
                    break

                self.signals.log.emit("✗ Crash confirmed. Starting bisection...\n")

                # Create fresh session for this round (include ASI mods)
                session = DeltaDebugSession(thread_mm, extra_mods=list(self._asi_mods.values()))
                round_num = 0

                while not session.is_done() and not self._cancelled:
                    round_num += 1
                    total_rounds += 1
                    config = session.start_round()
                    test_count = len(session.current_group)

                    self.signals.log.emit(f"\n─── Round {round_num} ───")
                    self.signals.log.emit(
                        f"Testing {test_count} of {len(session._changes)} suspects")
                    self.signals.progress.emit(total_rounds, total_rounds + estimated)

                    self.signals.log.emit("Applying mod configuration...")
                    for mod_id, enabled in config.items():
                        self._set_mod_enabled(mod_id, enabled, thread_mm)

                    apply_errors = []
                    worker = ApplyWorker(self._game_dir, self._vanilla_dir,
                                         db_path, force_outdated=True)
                    worker.error_occurred.connect(lambda e: apply_errors.append(e))
                    worker.run()

                    if apply_errors:
                        self.signals.log.emit(f"  ✗ Apply failed: {apply_errors[0]}")
                        self.signals.log.emit("  Treating as crash for this round.")
                        session.report_crash(True)
                        self._save_progress(thread_db, session)
                        continue

                    if self._cancelled:
                        break

                    self.signals.log.emit("Launching game through Steam...")
                    crashed = launch_and_test(
                        self._game_dir, stable_seconds=90, launch_timeout=60,
                        log_cb=lambda msg: self.signals.log.emit(f"  {msg}"),
                        cancel_check=lambda: self._cancelled)

                    if self._cancelled:
                        break

                    result_str = "CRASHED" if crashed else "OK"
                    self.signals.log.emit(f"Result: {result_str}")
                    session.report_crash(crashed)
                    self._save_progress(thread_db, session)

                if self._cancelled:
                    break

                # Got a result from this bisection pass
                result = session.get_result()
                minimal = result.get("minimal_set", [])
                if minimal:
                    # Get context: what other mods were enabled during this test
                    context_names = [em["name"] for em in enabled_mods
                                     if em["name"] not in [m["name"] for m in minimal]]
                    for m in minimal:
                        name = m["name"]
                        all_culprits.append((m["id"], name))
                        self._set_mod_enabled(m["id"], False, thread_mm)
                        # Flag in crash registry (PAZ mods only — ASI mods use negative IDs)
                        if m["id"] > 0:
                            try:
                                thread_mm.flag_crash(
                                    m["id"], crashes_alone=len(minimal) == 1,
                                    context_mods=context_names[:10],
                                    rounds=round_num)
                            except Exception:
                                pass
                        self.signals.log.emit(f"\n★ CULPRIT FOUND: {name}")
                        self.signals.log.emit(f"  Flagged in crash registry. Checking for more...")
                else:
                    self.signals.log.emit("No single culprit found in this pass.")
                    break

            # Restore original state (except culprits stay disabled)
            self.signals.log.emit("\nRestoring mod state...")
            culprit_ids = {cid for cid, _ in all_culprits}
            for mod_id, enabled in original_state.items():
                if mod_id in culprit_ids:
                    self._set_mod_enabled(mod_id, False, thread_mm)  # keep culprits disabled
                else:
                    self._set_mod_enabled(mod_id, enabled, thread_mm)

            worker = ApplyWorker(self._game_dir, self._vanilla_dir,
                                 db_path, force_outdated=True)
            worker.run()
            thread_db.close()

            if self._cancelled:
                self.signals.log.emit("Bisection cancelled. Mods restored.")
                self.signals.error.emit("Cancelled by user")
            else:
                # Build combined result
                final_result = {
                    "minimal_set": [{"id": cid, "name": n} for cid, n in all_culprits],
                    "rounds": total_rounds,
                    "is_single": len(all_culprits) == 1,
                    "is_combination": False,
                }
                self.signals.log.emit(
                    f"\nDone! Found {len(all_culprits)} problem mod(s) in {total_rounds} rounds.")
                self.signals.finished.emit(final_result)

        except Exception as e:
            logger.error("Auto bisect failed: %s", e, exc_info=True)
            try:
                for mod_id, enabled in original_state.items():
                    self._set_mod_enabled(mod_id, enabled, thread_mm)
                thread_db.close()
            except Exception:
                pass
            self.signals.error.emit(str(e))

    def _save_progress(self, db, session=None):
        try:
            import json
            s = session or self._session
            data = json.dumps({
                "all_ids": s.all_ids,
                "changes": s._changes,
                "n": s._n,
                "partition_index": s._partition_index,
                "testing_complement": s._testing_complement,
                "round_number": s.round_number,
                "history": s.history,
                "phase": s.phase,
            })
            db.connection.execute(
                "CREATE TABLE IF NOT EXISTS ddmin_progress "
                "(id INTEGER PRIMARY KEY, data TEXT)")
            db.connection.execute(
                "INSERT OR REPLACE INTO ddmin_progress (id, data) VALUES (1, ?)",
                (data,))
            db.connection.commit()
        except Exception:
            pass

