"""Game State Verification dialog.

Shows a clear, trustworthy report of exactly which game files are vanilla
and which are modded. The user can verify this independently.
"""

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTextBrowser, QPushButton,
    QHBoxLayout, QProgressDialog,
)

from cdumm.engine.snapshot_manager import hash_file
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class VerifyWorker(QObject):
    """Background worker that checks every game file against the snapshot."""

    progress_updated = Signal(int, str)
    finished = Signal(dict)  # results dict
    error_occurred = Signal(str)

    def __init__(self, game_dir: Path, db_path: Path):
        super().__init__()
        self._game_dir = game_dir
        self._db_path = db_path

    def run(self):
        try:
            db = Database(self._db_path)
            db.initialize()

            cursor = db.connection.execute(
                "SELECT file_path, file_hash, file_size FROM snapshots")
            snap_entries = cursor.fetchall()

            if not snap_entries:
                self.error_occurred.emit("No snapshot found. Scan game files first.")
                db.close()
                return

            results = {
                "vanilla": [],      # files matching snapshot
                "modded": [],       # files differing from snapshot
                "missing": [],      # snapshot files not on disk
                "extra_dirs": [],   # directories >= 0036 not in snapshot
                "total": len(snap_entries),
            }

            for i, (file_path, snap_hash, snap_size) in enumerate(snap_entries):
                pct = int((i / len(snap_entries)) * 100)
                self.progress_updated.emit(pct, f"Checking {file_path}...")

                game_file = self._game_dir / file_path.replace("/", os.sep)
                if not game_file.exists():
                    results["missing"].append(file_path)
                    continue

                actual_size = game_file.stat().st_size
                if actual_size != snap_size:
                    results["modded"].append({
                        "path": file_path,
                        "reason": f"size {actual_size} != vanilla {snap_size}",
                    })
                    continue

                # Hash check for small files (<50MB), size-only for large
                if snap_size < 50 * 1024 * 1024:
                    actual_hash, _ = hash_file(game_file)
                    if actual_hash != snap_hash:
                        results["modded"].append({
                            "path": file_path,
                            "reason": "content differs (same size)",
                        })
                        continue

                results["vanilla"].append(file_path)

            # Check for extra mod directories
            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) >= 36:
                    files = [f.name for f in d.iterdir() if f.is_file()]
                    if files:
                        results["extra_dirs"].append({
                            "name": d.name,
                            "files": files,
                        })

            db.close()
            self.progress_updated.emit(100, "Done")
            self.finished.emit(results)

        except Exception as e:
            logger.error("Verify failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class VerifyDialog(QDialog):
    """Shows game state verification results."""

    def __init__(self, results: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Game State Verification")
        self.setMinimumSize(600, 500)
        self.resize(650, 550)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Summary
        vanilla_count = len(results["vanilla"])
        modded_count = len(results["modded"])
        missing_count = len(results["missing"])
        extra_count = len(results["extra_dirs"])
        total = results["total"]

        if modded_count == 0 and missing_count == 0 and extra_count == 0:
            status = "ALL CLEAN"
            color = "#A3BE8C"
            summary = f"All {vanilla_count} game files match vanilla. Your game is clean."
        else:
            status = "MODDED"
            color = "#BF616A"
            summary = (f"{modded_count} modded, {missing_count} missing, "
                       f"{extra_count} extra directories. "
                       f"{vanilla_count}/{total} files are vanilla.")

        header = QLabel(f'Game State: <span style="color:{color};">{status}</span>')
        header.setStyleSheet("font-size: 16px; font-weight: bold; color: #ECEFF4;")
        layout.addWidget(header)

        sub = QLabel(summary)
        sub.setStyleSheet("font-size: 12px; color: #D8DEE9;")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # Detail report
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setStyleSheet(
            "QTextBrowser { background: #1A1D23; border: 1px solid #2E3440; "
            "border-radius: 6px; padding: 8px; color: #D8DEE9; }"
        )

        html = []

        if results["modded"]:
            html.append('<h3 style="color:#BF616A;">Modded Files</h3><ul>')
            for m in results["modded"]:
                html.append(f'<li><b>{m["path"]}</b> — {m["reason"]}</li>')
            html.append('</ul>')

        if results["extra_dirs"]:
            html.append('<h3 style="color:#D08770;">Extra Mod Directories</h3><ul>')
            for d in results["extra_dirs"]:
                html.append(f'<li><b>{d["name"]}/</b> — {", ".join(d["files"])}</li>')
            html.append('</ul>')

        if results["missing"]:
            html.append('<h3 style="color:#EBCB8B;">Missing Files</h3><ul>')
            for m in results["missing"]:
                html.append(f'<li>{m}</li>')
            html.append('</ul>')

        if results["vanilla"]:
            html.append(f'<h3 style="color:#A3BE8C;">Vanilla Files ({vanilla_count})</h3>')
            html.append('<details><summary>Click to expand</summary><ul>')
            for v in results["vanilla"]:
                html.append(f'<li>{v}</li>')
            html.append('</ul></details>')

        browser.setHtml("\n".join(html))
        layout.addWidget(browser)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        if modded_count > 0 or extra_count > 0:
            steam_btn = QPushButton("I'll Verify Through Steam")
            steam_btn.setFixedWidth(200)
            steam_btn.clicked.connect(self.reject)
            btn_row.addWidget(steam_btn)

        close_btn = QPushButton("OK")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)
