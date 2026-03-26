"""Qt Model for the mod list table view."""
import logging

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from cdmm.engine.conflict_detector import ConflictDetector
from cdmm.engine.mod_manager import ModManager

logger = logging.getLogger(__name__)

COLUMNS = ["", "#", "Name", "Type", "Status", "Files", "Import Date"]
COL_ENABLED = 0
COL_ORDER = 1
COL_NAME = 2
COL_TYPE = 3
COL_STATUS = 4
COL_FILES = 5
COL_DATE = 6


class ModListModel(QAbstractTableModel):
    """Table model backed by SQLite mod registry."""

    mod_toggled = Signal()  # emitted when a mod is enabled/disabled via checkbox

    def __init__(self, mod_manager: ModManager, conflict_detector: ConflictDetector,
                 parent=None) -> None:
        super().__init__(parent)
        self._mod_manager = mod_manager
        self._conflict_detector = conflict_detector
        self._mods: list[dict] = []
        self.refresh()

    def refresh(self) -> None:
        self.beginResetModel()
        self._mods = self._mod_manager.list_mods()
        self.endResetModel()

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
                return mod["name"]
            if col == COL_TYPE:
                return mod["mod_type"].upper()
            if col == COL_STATUS:
                return self._conflict_detector.get_mod_status(mod["id"])
            if col == COL_FILES:
                details = self._mod_manager.get_mod_details(mod["id"])
                return str(len(details["changed_files"])) if details else "0"
            if col == COL_DATE:
                return mod["import_date"][:10] if mod["import_date"] else ""

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
        return flags

    def get_mod_at_row(self, row: int) -> dict | None:
        if 0 <= row < len(self._mods):
            return self._mods[row]
        return None
