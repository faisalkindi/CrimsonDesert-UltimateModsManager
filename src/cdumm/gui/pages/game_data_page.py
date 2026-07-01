"""Game Data page — build a searchable catalog of the installed game.

A thin tool page over ``cdumm.engine.game_index``: one button builds (or
refreshes) a SQLite index of every archive asset + the keyed game-data tables,
on a background thread, then shows a summary. Search/browse UI is a planned
follow-up; this first version reuses only ToolPageBase's built-in widgets
(button, progress, stat cards, result cards) to keep it low-risk.

Strings are intentionally literal (not tr() keys) for this first version, so it
adds no new localization keys; localization is a follow-up.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from PySide6.QtCore import QObject, QThread, Signal

from cdumm.engine import game_index
from cdumm.gui.pages.tool_page import ToolPageBase


class _GameIndexWorker(QObject):
    """Runs game_index.build_index off the UI thread."""

    progress = Signal(int, str)
    done = Signal(dict)
    error = Signal(str)

    def __init__(self, game_dir: str, out_path: str) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._out = out_path

    def run(self) -> None:
        try:
            total = max(1, len(game_index.archive_dirs(self._game_dir)))
            state = {"i": 0}

            def cb(archive: str, n: int) -> None:
                state["i"] += 1
                pct = min(99, int(state["i"] * 100 / total))
                self.progress.emit(pct, f"Indexed {archive}  ({n:,} entries)")

            stats = game_index.build_index(
                self._game_dir, self._out, progress=cb)
            self.done.emit(stats)
        except Exception as ex:  # noqa: BLE001 — surface any failure to the UI
            self.error.emit(str(ex))


class GameDataPage(ToolPageBase):
    """Build + summarize a searchable index of the installed game's data."""

    def __init__(self, parent=None) -> None:
        super().__init__(
            object_name="GameDataPage",
            title="Game Data",
            description=(
                "Build a searchable catalog of this Crimson Desert install — "
                "every asset the game ships plus the keyed game-data tables "
                "(items, NPCs, quests, skills, drops, ...). Metadata only; no "
                "asset files are extracted."),
            run_label="Build / refresh game-data index",
            parent=parent,
        )
        # Persisted next to the OS temp dir for this first version; a future
        # revision can move it under CDMods and add a search UI over it.
        self._index_path = os.path.join(
            tempfile.gettempdir(), "cdumm_game_index.sqlite")
        self._thread: QThread | None = None
        self._worker: _GameIndexWorker | None = None

        self._stat_assets = self._add_stat_card("--", "Assets", "#2878D0")
        self._stat_archives = self._add_stat_card("--", "Archives", "#8B5CF6")
        self._stat_tables = self._add_stat_card("--", "Data tables", "#0EA5E9")

    # ── run ─────────────────────────────────────────────────────────
    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if not self._game_dir:
            self._set_status("No game folder configured.", "#BF616A")
            return

        self._clear_results()
        self._set_running(True)

        self._thread = QThread(self)
        self._worker = _GameIndexWorker(str(self._game_dir), self._index_path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        # Teardown: quit the loop + delete the worker once it reports back.
        self._worker.done.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(lambda: setattr(self, "_thread", None))
        self._thread.start()

    # ── worker callbacks (main thread, via queued signals) ──────────
    def _on_progress(self, pct: int, msg: str) -> None:
        self._set_progress(pct, msg)

    def _on_done(self, stats: dict) -> None:
        self._set_running(False)
        try:
            self._stat_assets.set_value(
                f"{int(stats.get('assets_total', 0)):,}")
            self._stat_archives.set_value(str(stats.get("archives", "--")))
            self._stat_tables.set_value(
                str(stats.get("data_table_distinct", "--")))
        except Exception:  # noqa: BLE001
            pass
        self._set_status("Index built.", "#2E7D32")

        detail = ""
        try:
            con = sqlite3.connect(self._index_path)
            try:
                tables = game_index.list_data_tables(con)[:12]
            finally:
                con.close()
            detail = "\n".join(
                f"• {t['name']}  ({t['orig_size']:,} bytes)"
                for t in tables)
        except Exception as ex:  # noqa: BLE001
            detail = f"(could not read tables: {ex})"
        self._add_result_card("Largest keyed game-data tables", detail)
        self._add_result_card("Index file", self._index_path)

    def _on_error(self, msg: str) -> None:
        self._set_running(False)
        self._set_status("Index failed.", "#BF616A")
        self._add_result_card("Error", msg, "#BF616A")
