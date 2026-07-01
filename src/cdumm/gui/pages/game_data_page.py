"""Game Data page — build + browse a searchable catalog of the installed game.

A tool page over ``cdumm.engine.game_index``: one button builds (or refreshes)
a SQLite index of every archive asset + the keyed game-data tables, on a
background thread. You can choose where the index file is saved, open its
folder, and search the indexed assets in a table right in the app.

Strings are literal (not tr() keys) for this first version, so it adds no new
localization keys; localization is a follow-up.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QSplitter,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from cdumm.engine import game_index
from cdumm.gui.pages.tool_page import ToolPageBase
from cdumm.platform import IS_MACOS, IS_WINDOWS


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
    """Build, locate, and search an index of the installed game's data."""

    def __init__(self, parent=None) -> None:
        super().__init__(
            object_name="GameDataPage",
            title="Game Data",
            description=(
                "Build a searchable catalog of this Crimson Desert install — "
                "every asset the game ships plus the keyed game-data tables "
                "(items, NPCs, quests, skills, drops, ...). Click any result "
                "to preview it on the right — text formats show as text, "
                "everything else as hex + metadata — or extract it to disk."),
            run_label="Build / refresh game-data index",
            parent=parent,
        )
        self._app_data_dir = None
        self._index_path = os.path.join(
            tempfile.gettempdir(), "cdumm_game_index.sqlite")
        self._thread: QThread | None = None
        self._worker: _GameIndexWorker | None = None

        self._stat_assets = self._add_stat_card("--", "Assets", "#2878D0")
        self._stat_archives = self._add_stat_card("--", "Archives", "#8B5CF6")
        self._stat_tables = self._add_stat_card("--", "Data tables", "#0EA5E9")

        self._build_controls()

    # ── extra controls (persistent — not wiped by _clear_results) ────
    def _build_controls(self) -> None:
        from qfluentwidgets import (BodyLabel, CaptionLabel, LineEdit,
                                     PlainTextEdit, PushButton,
                                     StrongBodyLabel, TableWidget)
        root = self._container.layout()

        # Save-location row: path label + Change + Open folder
        loc_row = QHBoxLayout()
        loc_row.setSpacing(8)
        self._path_label = CaptionLabel(
            f"Save location:  {self._index_path}", self._container)
        self._path_label.setWordWrap(True)
        loc_row.addWidget(self._path_label, 1)
        self._change_btn = PushButton("Change…", self._container)
        self._change_btn.clicked.connect(self._choose_location)
        loc_row.addWidget(self._change_btn)
        self._open_btn = PushButton("Open folder", self._container)
        self._open_btn.clicked.connect(self._open_location)
        loc_row.addWidget(self._open_btn)
        root.insertLayout(root.count() - 1, loc_row)

        # Search + results table
        self._search = LineEdit(self._container)
        self._search.setPlaceholderText(
            "Search assets by path (build the index first)…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(38)
        _sf = self._search.font()
        _sf.setPixelSize(15)
        self._search.setFont(_sf)
        self._search.textChanged.connect(self._on_search)
        root.insertWidget(root.count() - 1, self._search)

        self._hits = BodyLabel("", self._container)
        self._hits.setContentsMargins(2, 4, 0, 0)
        _hitf = self._hits.font()
        _hitf.setPixelSize(15)
        self._hits.setFont(_hitf)
        root.insertWidget(root.count() - 1, self._hits)

        # Results table (left) + live preview pane (right), in a draggable
        # splitter so the user can trade list width for preview width.
        split = QSplitter(Qt.Horizontal, self._container)
        split.setChildrenCollapsible(False)
        split.setMinimumHeight(420)

        self._table = TableWidget(split)
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            ["Path", "Archive", "Type", "Size (bytes)"])
        self._table.verticalHeader().hide()
        self._table.setEditTriggers(self._table.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            self._table.SelectionBehavior.SelectRows)
        # Larger, more readable text + roomier rows.
        _tf = self._table.font()
        _tf.setPixelSize(15)
        self._table.setFont(_tf)
        _hf = self._table.horizontalHeader().font()
        _hf.setPixelSize(15)
        self._table.horizontalHeader().setFont(_hf)
        self._table.verticalHeader().setDefaultSectionSize(36)
        try:
            self._table.setColumnWidth(0, 360)
        except Exception:  # noqa: BLE001
            pass
        self._table.itemSelectionChanged.connect(self._on_asset_selected)
        split.addWidget(self._table)

        # Preview pane: title + metadata + monospace text/hex view + extract.
        pane = QWidget(split)
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(10, 0, 0, 0)
        pv.setSpacing(6)
        self._pv_title = StrongBodyLabel("Select an asset to preview", pane)
        _ptf = self._pv_title.font()
        _ptf.setPixelSize(16)
        self._pv_title.setFont(_ptf)
        pv.addWidget(self._pv_title)
        self._pv_meta = CaptionLabel("", pane)
        self._pv_meta.setWordWrap(True)
        pv.addWidget(self._pv_meta)
        self._pv_text = PlainTextEdit(pane)
        self._pv_text.setReadOnly(True)
        self._pv_text.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        _mf = self._pv_text.font()
        _mf.setFamily("Consolas")
        _mf.setStyleHint(_mf.StyleHint.Monospace)
        _mf.setPixelSize(13)
        self._pv_text.setFont(_mf)
        self._pv_text.setPlaceholderText(
            "Click any row on the left to view that asset.\n\nText formats "
            "(XML, JSON, JS, CSS) show as text; everything else shows a hex "
            "+ metadata view. Textures, audio and models need format "
            "converters, which are a later addition.")
        pv.addWidget(self._pv_text, 1)
        self._pv_extract = PushButton("Extract raw file…", pane)
        self._pv_extract.setEnabled(False)
        self._pv_extract.clicked.connect(self._extract_raw)
        pv.addWidget(self._pv_extract)
        split.addWidget(pane)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self._preview_bytes: bytes | None = None
        self._preview_name: str | None = None
        root.insertWidget(root.count() - 1, split)

    # ── engine wiring ────────────────────────────────────────────────
    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        # Preserve across re-wire calls that don't pass app_data_dir.
        self._app_data_dir = kwargs.get("app_data_dir") or self._app_data_dir
        saved = self._load_pref()
        if saved:
            self._index_path = saved
        elif self._app_data_dir:
            self._index_path = os.path.join(
                str(self._app_data_dir), "game_index.sqlite")
        if hasattr(self, "_path_label"):
            self._path_label.setText(f"Save location:  {self._index_path}")
        if hasattr(self, "_open_btn"):
            self._open_btn.setEnabled(os.path.exists(self._index_path))

    def _load_pref(self):
        try:
            if self._db:
                from cdumm.storage.config import Config
                return Config(self._db).get("game_index_path") or None
        except Exception:  # noqa: BLE001
            pass
        return None

    def _save_pref(self, path: str) -> None:
        try:
            if self._db:
                from cdumm.storage.config import Config
                Config(self._db).set("game_index_path", path)
        except Exception:  # noqa: BLE001
            pass

    # ── location actions ─────────────────────────────────────────────
    def _choose_location(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose where to save the game-data index",
            self._index_path, "SQLite database (*.sqlite)")
        if path:
            self._index_path = path
            self._save_pref(path)
            self._path_label.setText(f"Save location:  {path}")
            self._open_btn.setEnabled(os.path.exists(path))

    def _open_location(self) -> None:
        p = self._index_path
        folder = os.path.dirname(p) or "."
        try:
            if IS_WINDOWS:
                if os.path.exists(p):
                    subprocess.Popen(["explorer", "/select,",
                                      os.path.normpath(p)])
                else:
                    os.startfile(folder)  # noqa: S606
            elif IS_MACOS:
                args = ["open", "-R", p] if os.path.exists(p) else ["open",
                                                                     folder]
                subprocess.Popen(args)
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Could not open folder: {ex}", "#BF616A")

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
        self._open_btn.setEnabled(os.path.exists(self._index_path))

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
        # refresh the viewer if a search is active
        self._on_search(self._search.text())

    def _on_error(self, msg: str) -> None:
        self._set_running(False)
        self._set_status("Index failed.", "#BF616A")
        self._add_result_card("Error", msg, "#BF616A")

    # ── search / viewer ──────────────────────────────────────────────
    def _on_search(self, text: str) -> None:
        text = (text or "").strip()
        if not os.path.exists(self._index_path):
            self._hits.setText("Build the index first to search.")
            self._table.setRowCount(0)
            return
        if len(text) < 2:
            self._hits.setText("Type at least 2 characters to search.")
            self._table.setRowCount(0)
            return
        try:
            con = sqlite3.connect(self._index_path)
            try:
                rows = game_index.search_assets(con, query=text, limit=300)
            finally:
                con.close()
        except Exception as ex:  # noqa: BLE001
            self._hits.setText(f"Search failed: {ex}")
            return
        self._hits.setText(
            f"{len(rows)} match(es)" + (" (showing first 300)"
                                        if len(rows) == 300 else ""))
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._table.setItem(i, 0, QTableWidgetItem(str(r["path"])))
            self._table.setItem(i, 1, QTableWidgetItem(str(r["archive"])))
            self._table.setItem(i, 2, QTableWidgetItem(str(r["ext"])))
            self._table.setItem(
                i, 3, QTableWidgetItem(f"{int(r['orig_size']):,}"))

    # ── preview pane ─────────────────────────────────────────────────
    _PREVIEW_TEXT_CAP = 200_000            # chars shown for text assets
    _PREVIEW_HEX_CAP = 4096               # bytes shown for the hex view
    _PREVIEW_SIZE_LIMIT = 32 * 1024 * 1024  # don't inline-decode above this

    def _selected_path(self) -> str | None:
        items = self._table.selectedItems()
        if not items:
            return None
        cell = self._table.item(items[0].row(), 0)
        return cell.text() if cell else None

    def _on_asset_selected(self) -> None:
        """Read + preview the clicked asset (metadata always; text or hex
        when the bytes are reachable and within the inline size limit)."""
        path = self._selected_path()
        if not path:
            return
        self._preview_bytes = None
        self._preview_name = os.path.basename(path)
        self._pv_extract.setEnabled(False)
        self._pv_title.setText(self._preview_name)

        row = data = err = None
        try:
            con = sqlite3.connect(self._index_path)
            try:
                row = game_index.get_asset(con, path)
                if (row and self._game_dir
                        and int(row["orig_size"]) <= self._PREVIEW_SIZE_LIMIT):
                    data = game_index.extract_asset(
                        con, path, str(self._game_dir))
            finally:
                con.close()
        except Exception as ex:  # noqa: BLE001 — surface to the pane
            err = str(ex)

        if row is None:
            self._pv_meta.setText(
                f"Could not read: {err}" if err else "Asset not in index.")
            self._pv_text.setPlainText("")
            return

        flags = []
        if row["compressed"]:
            flags.append("LZ4")
        if row["encrypted"]:
            flags.append("encrypted")
        self._pv_meta.setText(
            f"{row['ext']}  ·  {int(row['orig_size']):,} bytes  ·  archive "
            f"{row['archive']}  ·  {', '.join(flags) or 'stored raw'}\n{path}")

        if not self._game_dir:
            self._pv_text.setPlainText(
                "No game folder configured — can't read asset bytes.")
            return
        if int(row["orig_size"]) > self._PREVIEW_SIZE_LIMIT:
            self._pv_text.setPlainText(
                f"Asset is {int(row['orig_size']):,} bytes — too large to "
                "preview inline.\nUse “Extract raw file…” to save it to disk.")
            self._pv_extract.setEnabled(True)
            return
        if err is not None:
            self._pv_text.setPlainText(f"Could not read asset bytes:\n{err}")
            return
        if data is None:
            self._pv_text.setPlainText("(no data)")
            return

        self._preview_bytes = data
        self._pv_extract.setEnabled(True)
        text = game_index.decode_text(data, limit=self._PREVIEW_TEXT_CAP)
        if text is not None:
            if len(text) >= self._PREVIEW_TEXT_CAP:
                text += ("\n\n… (truncated preview — use Extract raw for the "
                         "full file)")
            self._pv_text.setLineWrapMode(
                self._pv_text.LineWrapMode.WidgetWidth)
            self._pv_text.setPlainText(text)
        else:
            self._pv_text.setLineWrapMode(self._pv_text.LineWrapMode.NoWrap)
            self._pv_text.setPlainText(
                "Binary asset — no visual decoder for this format yet "
                "(textures, audio and models need converters). Hex view of "
                "the first bytes:\n\n"
                + game_index.hexdump(data, limit=self._PREVIEW_HEX_CAP))

    def _extract_raw(self) -> None:
        """Save the selected asset's real (decoded) bytes to a file."""
        path = self._selected_path()
        if not path:
            return
        data = self._preview_bytes
        if data is None:                       # large asset — extract on demand
            try:
                con = sqlite3.connect(self._index_path)
                try:
                    data = game_index.extract_asset(
                        con, path, str(self._game_dir))
                finally:
                    con.close()
            except Exception as ex:  # noqa: BLE001
                self._set_status(f"Extract failed: {ex}", "#BF616A")
                return
        default = os.path.join(
            os.path.expanduser("~"),
            self._preview_name or os.path.basename(path))
        out, _ = QFileDialog.getSaveFileName(
            self, "Save extracted asset", default, "All files (*.*)")
        if not out:
            return
        try:
            with open(out, "wb") as f:
                f.write(data)
            self._set_status(f"Saved {len(data):,} bytes to {out}.", "#2E7D32")
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Save failed: {ex}", "#BF616A")
