"""Binary search wizard dialog for finding problem mods."""

import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QStackedWidget, QTextBrowser, QWidget, QMessageBox,
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
        self._vanilla_dir = vanilla_dir
        self._db = db
        self._session = DeltaDebugSession(mod_manager)

        self._pages = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self._pages)

        self._build_intro_page()
        self._build_test_page()
        self._build_result_page()
        self._pages.setCurrentIndex(0)

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

        import math
        n = len(self._session.enabled_mods)
        best = max(1, 2 * math.ceil(math.log2(n))) if n > 1 else 1
        page.addWidget(QLabel(
            f"This will test your {n} enabled mods using the Delta Debugging\n"
            f"algorithm to find the minimal set causing the crash.\n\n"
            f"It finds single bad mods, conflict pairs, and multi-mod issues.\n\n"
            f"Estimated: {best}-{best * 3} rounds (each round you launch the\n"
            f"game and report if it crashed).\n\n"
            f"Your mod configuration will be restored when finished."
        ))

        mod_list = QListWidget()
        for m in self._session.enabled_mods:
            mod_list.addItem(m["name"])
        mod_list.setMaximumHeight(150)
        page.addWidget(mod_list)

        has_saved = self._has_saved_progress()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        if has_saved:
            fresh_btn = QPushButton("Start Fresh")
            fresh_btn.clicked.connect(self._start_fresh)
            btn_row.addWidget(fresh_btn)

            resume_btn = QPushButton("Resume")
            resume_btn.setStyleSheet("font-weight: bold; padding: 6px 20px; background: #D4A43C;")
            resume_btn.clicked.connect(self._resume_search)
            btn_row.addWidget(resume_btn)
        else:
            start_btn = QPushButton("Start")
            start_btn.setStyleSheet("font-weight: bold; padding: 6px 20px;")
            start_btn.clicked.connect(self._start_search)
            btn_row.addWidget(start_btn)

        page.addLayout(btn_row)

        if has_saved:
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

    def _build_test_page(self):
        page = QVBoxLayout()
        w = QWidget()
        page.setContentsMargins(16, 16, 16, 16)
        page.setSpacing(10)
        w.setLayout(page)

        self._phase_label = QLabel("Round 1")
        self._phase_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #D4A43C;")
        page.addWidget(self._phase_label)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #D8DEE9; font-size: 12px;")
        page.addWidget(self._status_label)

        self._test_list = QListWidget()
        self._test_list.setMaximumHeight(150)
        page.addWidget(self._test_list)

        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color: #788090; font-size: 11px;")
        page.addWidget(self._info_label)

        launch_btn = QPushButton("Launch Game")
        launch_btn.setStyleSheet("background: #48A858; color: white; font-weight: bold; padding: 8px;")
        launch_btn.clicked.connect(self._launch_game)
        page.addWidget(launch_btn)

        page.addWidget(QLabel("Did the game crash?"))

        btn_row = QHBoxLayout()
        crash_btn = QPushButton("Yes — Crashed")
        crash_btn.setStyleSheet(
            "background: #D04848; color: white; font-weight: bold; padding: 10px; font-size: 13px;")
        crash_btn.clicked.connect(lambda: self._report(True))
        btn_row.addWidget(crash_btn)

        ok_btn = QPushButton("No — Worked Fine")
        ok_btn.setStyleSheet(
            "background: #48A858; color: white; font-weight: bold; padding: 10px; font-size: 13px;")
        ok_btn.clicked.connect(lambda: self._report(False))
        btn_row.addWidget(ok_btn)
        page.addLayout(btn_row)

        cancel_btn = QPushButton("Cancel Search")
        cancel_btn.setStyleSheet("color: #788090;")
        cancel_btn.clicked.connect(self._cancel)
        page.addWidget(cancel_btn)

        self._pages.addWidget(w)

    def _build_result_page(self):
        page = QVBoxLayout()
        w = QWidget()
        page.setContentsMargins(16, 16, 16, 16)
        page.setSpacing(10)
        w.setLayout(page)

        self._result_title = QLabel("")
        self._result_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self._result_title.setWordWrap(True)
        page.addWidget(self._result_title)

        self._result_detail = QLabel("")
        self._result_detail.setWordWrap(True)
        self._result_detail.setStyleSheet("color: #D8DEE9; font-size: 12px;")
        page.addWidget(self._result_detail)

        self._history_browser = QTextBrowser()
        self._history_browser.setMaximumHeight(200)
        self._history_browser.setStyleSheet(
            "QTextBrowser { background: #1A1D23; border: 1px solid #2E3440; "
            "border-radius: 6px; padding: 6px; color: #D8DEE9; font-size: 11px; }")
        page.addWidget(self._history_browser)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._disable_btn = QPushButton("Disable Problem Mod(s)")
        self._disable_btn.setStyleSheet("background: #D04848; color: white; font-weight: bold;")
        self._disable_btn.clicked.connect(self._disable_culprits)
        btn_row.addWidget(self._disable_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self._restore_and_close)
        btn_row.addWidget(close_btn)
        page.addLayout(btn_row)

        self._pages.addWidget(w)

    def _log(self, msg, detail=None):
        parent = self.parent()
        if parent and hasattr(parent, '_log_activity'):
            parent._log_activity("verify", msg, detail)

    # --- Flow ---

    def _start_search(self):
        self._clear_progress()
        self._log(f"Delta debug started with {len(self._session.enabled_mods)} mods")
        self._run_next_round()

    def _start_fresh(self):
        self._clear_progress()
        self._session = DeltaDebugSession(self._mm)
        self._log(f"Delta debug restarted fresh with {len(self._session.enabled_mods)} mods")
        self._run_next_round()

    def _resume_search(self):
        self._load_progress()
        self._log(f"Delta debug resumed at round {self._session.round_number}")
        self._run_next_round()

    def _run_next_round(self):
        if self._session.is_done():
            self._show_results()
            return

        changes = self._session.start_round()
        for mod_id, enabled in changes.items():
            self._mm.set_enabled(mod_id, enabled)

        # Update UI
        self._phase_label.setText(self._session.get_phase_description())
        self._status_label.setText("These mods are enabled for this test:")

        self._test_list.clear()
        for mid in self._session.current_group:
            self._test_list.addItem(self._session.get_mod_name(mid))

        self._info_label.setText(
            f"Round {self._session.round_number} | "
            f"Testing {len(self._session.current_group)} of "
            f"{len(self._session._changes)} remaining mods")

        # Apply
        self._apply_and_show_test()

    def _apply_and_show_test(self):
        from cdumm.gui.progress_dialog import ProgressDialog
        main_win = self.parent()
        if not main_win._check_game_running():
            return
        self.hide()
        progress = ProgressDialog(
            f"Round {self._session.round_number} — {self._session.get_phase_description()}",
            main_win)
        worker = ApplyWorker(self._game_dir, self._vanilla_dir, self._db.db_path)
        thread = QThread()
        main_win._run_worker(worker, thread, progress,
                             on_finished=self._on_apply_complete)

    def _on_apply_complete(self):
        self.show()
        self.raise_()
        self._pages.setCurrentIndex(1)

    def _launch_game(self):
        exe = self._game_dir / "bin64" / "CrimsonDesert.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(exe.parent))

    def _report(self, crashed: bool):
        names = ", ".join(self._session.get_mod_name(m) for m in self._session.current_group)
        self._log(
            f"Round {self._session.round_number} [{self._session.phase}]: "
            f"{'CRASHED' if crashed else 'OK'} — {names}")

        next_desc = self._session.report_crash(crashed)
        logger.info("Next: %s", next_desc)

        self._save_progress()

        if self._session.is_done():
            self._clear_progress()
            self._show_results()
        else:
            self._run_next_round()

    def _show_results(self):
        result = self._session.get_result()
        minimal = result["minimal_set"]

        if not minimal:
            self._result_title.setText("No Problems Found")
            self._result_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #A3BE8C;")
            self._result_detail.setText("All enabled mods appear to work together.")
            self._disable_btn.setVisible(False)
        elif result["is_single"]:
            name = minimal[0]["name"]
            self._result_title.setText(f"Problem Mod: {name}")
            self._result_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #BF616A;")
            self._result_detail.setText(
                f"This mod causes the crash by itself.\n"
                f"Found in {result['rounds']} rounds.")
        else:
            names = "\n  ".join(m["name"] for m in minimal)
            self._result_title.setText(f"Minimal Crash Set ({len(minimal)} mods)")
            self._result_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #BF616A;")
            self._result_detail.setText(
                f"These mods crash when used together:\n  {names}\n\n"
                f"Each works alone, but the combination crashes.\n"
                f"Found in {result['rounds']} rounds.")

        # History
        html = []
        for h in result["history"]:
            color = "#BF616A" if h["crashed"] else "#A3BE8C"
            status = "CRASHED" if h["crashed"] else "OK"
            mods = ", ".join(h["tested"][:4])
            if len(h["tested"]) > 4:
                mods += f" +{len(h['tested'])-4} more"
            html.append(f'<span style="color:{color};">Round {h["round"]}: {status}</span>'
                        f' ({h["count"]} mods) — {mods}')
        self._history_browser.setHtml("<br>".join(html))

        names = ", ".join(m["name"] for m in minimal) if minimal else "none"
        self._log(f"Delta debug complete: minimal set = [{names}] in {result['rounds']} rounds")

        self._pages.setCurrentIndex(2)

    def _disable_culprits(self):
        result = self._session.get_result()
        to_disable = {m["id"] for m in result["minimal_set"]}

        for mod_id, was_enabled in self._session.original_state.items():
            if mod_id in to_disable:
                self._mm.set_enabled(mod_id, False)
            else:
                self._mm.set_enabled(mod_id, was_enabled)

        names = ", ".join(m["name"] for m in result["minimal_set"])
        self._log(f"Disabled problem mod(s): {names}")
        self.accept()

    def _restore_and_close(self):
        for mod_id, enabled in self._session.get_restore_changes().items():
            self._mm.set_enabled(mod_id, enabled)
        self._log("Restored original mod state after binary search")
        self.accept()

    def _cancel(self):
        self._restore_and_close()

    def closeEvent(self, event):
        if not self._session.is_done() and self._session.round_number > 0:
            reply = QMessageBox.question(
                self, "Cancel Search?",
                "Cancel the search and restore your original mod configuration?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._restore_and_close()
        super().closeEvent(event)
