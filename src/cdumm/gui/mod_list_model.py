"""Qt Model for the mod list table view."""
import logging
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThread, Qt, Signal, QObject
from PySide6.QtGui import QColor

from cdumm.engine.conflict_detector import ConflictDetector
from cdumm.engine.mod_manager import ModManager

logger = logging.getLogger(__name__)

COLUMNS = ["☐", "#", "Name", "Author", "Version", "Status", "Files", "Import Date"]
COL_ENABLED = 0
COL_ORDER = 1
COL_NAME = 2
COL_AUTHOR = 3
COL_VERSION = 4
COL_STATUS = 5
COL_FILES = 6
COL_DATE = 7

STATUS_COLORS = {
    "active": QColor(76, 175, 80),       # green
    "not applied": QColor(255, 152, 0),   # orange
    "no data": QColor(244, 67, 54),       # red
    "disabled": QColor(158, 158, 158),    # gray
    "checking...": QColor(158, 158, 158), # gray
}


class _StatusWorker(QObject):
    """Background worker to compute mod game statuses without blocking UI."""
    finished = Signal(object)  # {mod_id: status_str}

    def __init__(self, mod_ids: list[int], db_path: Path, game_dir: Path,
                 deltas_dir: Path) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._db_path = db_path
        self._game_dir = game_dir
        self._deltas_dir = deltas_dir

    def run(self) -> None:
        from cdumm.storage.database import Database
        db = Database(self._db_path)
        db.initialize()
        mgr = ModManager(db, self._deltas_dir)
        results = {}
        for mid in self._mod_ids:
            results[mid] = mgr.get_mod_game_status(mid, self._game_dir)
        db.close()
        self.finished.emit(results)


class ModListModel(QAbstractTableModel):
    """Table model backed by SQLite mod registry."""

    mod_toggled = Signal()  # emitted when a mod is enabled/disabled via checkbox

    def __init__(self, mod_manager: ModManager, conflict_detector: ConflictDetector,
                 game_dir: Path | None = None, db_path: Path | None = None,
                 deltas_dir: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._mod_manager = mod_manager
        self._conflict_detector = conflict_detector
        self._game_dir = game_dir
        self._db_path = db_path
        self._deltas_dir = deltas_dir
        self._mods: list[dict] = []
        self._status_cache: dict[int, str] = {}
        self._status_thread: QThread | None = None
        self.refresh()

    def refresh(self) -> None:
        self.beginResetModel()
        self._mods = self._mod_manager.list_mods()
        # Set placeholder — real status computed after window is shown
        self._status_cache = {mod["id"]: "checking..." for mod in self._mods}
        self.endResetModel()

    def refresh_statuses(self) -> None:
        """Trigger background status computation. Call after window is visible."""
        self._refresh_statuses_async()

    def _refresh_statuses_async(self) -> None:
        """Compute mod game statuses on a background thread."""
        if not self._game_dir or not self._db_path or not self._deltas_dir:
            return
        if not self._mods:
            return

        # Clean up previous thread
        old_thread = self._status_thread
        if old_thread is not None:
            try:
                if old_thread.isRunning():
                    old_thread.quit()
                    old_thread.wait(2000)
            except RuntimeError:
                pass
        self._status_thread = None
        self._status_worker = None

        mod_ids = [m["id"] for m in self._mods]
        worker = _StatusWorker(mod_ids, self._db_path, self._game_dir, self._deltas_dir)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_statuses_ready)
        worker.finished.connect(thread.quit)
        self._status_thread = thread
        self._status_worker = worker
        thread.start()

    def _on_statuses_ready(self, results: dict) -> None:
        self._status_cache.update(results)
        # Emit dataChanged for the status column
        if self._mods:
            top = self.index(0, COL_STATUS)
            bottom = self.index(len(self._mods) - 1, COL_STATUS)
            self.dataChanged.emit(top, bottom)

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._mods)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._mods):
            return None

        mod = self._mods[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_ORDER:
                return str(index.row() + 1)
            if col == COL_NAME:
                if mod.get("configurable"):
                    return f"⚙ {mod['name']}"
                return mod["name"]
            if col == COL_AUTHOR:
                return mod.get("author") or ""
            if col == COL_VERSION:
                return mod.get("version") or ""
            if col == COL_STATUS:
                status = self._status_cache.get(mod["id"], "")
                conflict = self._conflict_detector.get_mod_status(mod["id"])
                if conflict in ("conflict", "resolved"):
                    return f"{status} ({conflict})"
                return status
            if col == COL_FILES:
                details = self._mod_manager.get_mod_details(mod["id"])
                return str(len(details["changed_files"])) if details else "0"
            if col == COL_DATE:
                return mod["import_date"][:16] if mod["import_date"] else ""

        if role == Qt.ItemDataRole.ForegroundRole and col == COL_STATUS:
            status = self._status_cache.get(mod["id"], "")
            return STATUS_COLORS.get(status)

        if role == Qt.ItemDataRole.CheckStateRole and col == COL_ENABLED:
            return Qt.CheckState.Checked if mod["enabled"] else Qt.CheckState.Unchecked

        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if index.column() == COL_ENABLED and role == Qt.ItemDataRole.CheckStateRole:
            mod = self._mods[index.row()]
            enabled = value == Qt.CheckState.Checked.value
            self._mod_manager.set_enabled(mod["id"], enabled)
            mod["enabled"] = enabled
            self.dataChanged.emit(index, index)
            self.mod_toggled.emit()
            return True
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.column() == COL_ENABLED:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        if index.isValid():
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return ["application/x-cdumm-mod-row"]

    def mimeData(self, indexes):
        from PySide6.QtCore import QMimeData
        data = QMimeData()
        rows = sorted(set(idx.row() for idx in indexes if idx.isValid()))
        data.setData("application/x-cdumm-mod-row", ",".join(str(r) for r in rows).encode())
        return data

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat("application/x-cdumm-mod-row")

    def dropMimeData(self, data, action, row, column, parent):
        if not data.hasFormat("application/x-cdumm-mod-row"):
            return False
        raw = bytes(data.data("application/x-cdumm-mod-row")).decode()
        if not raw:
            return False
        source_rows = [int(r) for r in raw.split(",")]
        if not source_rows:
            return False

        # When dropping ON a row (not between), row is -1 and parent is valid
        if row < 0 and parent.isValid():
            row = parent.row()
        elif row < 0:
            row = len(self._mods)

        # Reorder: move source rows to target position
        ids = [m["id"] for m in self._mods]
        moved = [ids[r] for r in source_rows if r < len(ids)]
        remaining = [mid for mid in ids if mid not in moved]
        # Adjust target: account for removed items above the drop point
        target = row
        for r in sorted(source_rows):
            if r < row:
                target -= 1
        target = max(0, min(target, len(remaining)))
        new_order = remaining[:target] + moved + remaining[target:]

        self._mod_manager.reorder_mods(new_order)
        self.refresh()
        self.refresh_statuses()
        self.mod_toggled.emit()
        return True

    def get_mod_at_row(self, row: int) -> dict | None:
        if 0 <= row < len(self._mods):
            return self._mods[row]
        return None
