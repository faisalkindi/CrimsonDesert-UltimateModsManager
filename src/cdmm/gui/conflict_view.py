import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QHeaderView, QMenu, QTreeView, QVBoxLayout, QWidget

from cdmm.engine.conflict_detector import Conflict

logger = logging.getLogger(__name__)

LEVEL_COLORS = {
    "papgt": QColor("#4CAF50"),     # green — auto-handled
    "paz": QColor("#FFC107"),       # yellow — warning
    "byte_range": QColor("#FF9800"),  # orange — resolved via load order
}

LEVEL_LABELS = {
    "papgt": "Auto-handled (PAPGT)",
    "paz": "Compatible (different byte ranges)",
    "byte_range": "Resolved (load order)",
}

# Data role for storing mod IDs on tree items
MOD_A_ID_ROLE = Qt.ItemDataRole.UserRole + 1
MOD_B_ID_ROLE = Qt.ItemDataRole.UserRole + 2
WINNER_ID_ROLE = Qt.ItemDataRole.UserRole + 3


class ConflictView(QWidget):
    """Tree view displaying mod conflicts grouped by mod pair → file → details."""

    winner_changed = Signal(int)  # emits mod_id that was set as winner

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tree = QTreeView()
        self._tree.setHeaderHidden(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(["Conflict", "Level", "Resolution"])
        self._tree.setModel(self._model)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        layout.addWidget(self._tree)

    def update_conflicts(self, conflicts: list[Conflict]) -> None:
        """Rebuild the tree with the current conflict list."""
        self._model.removeRows(0, self._model.rowCount())

        if not conflicts:
            empty = QStandardItem("No conflicts detected")
            empty.setForeground(QColor("#4CAF50"))
            self._model.appendRow([empty, QStandardItem(""), QStandardItem("")])
            return

        # Group by mod pair
        pairs: dict[tuple[int, int], list[Conflict]] = {}
        for c in conflicts:
            key = (min(c.mod_a_id, c.mod_b_id), max(c.mod_a_id, c.mod_b_id))
            pairs.setdefault(key, []).append(c)

        for (_, _), pair_conflicts in pairs.items():
            first = pair_conflicts[0]
            # Determine worst level for this pair
            worst = "papgt"
            for c in pair_conflicts:
                if c.level == "byte_range":
                    worst = "byte_range"
                    break
                if c.level == "paz":
                    worst = "paz"

            pair_item = QStandardItem(f"{first.mod_a_name} ↔ {first.mod_b_name}")
            pair_item.setForeground(LEVEL_COLORS.get(worst, QColor("#999")))
            pair_item.setData(first.mod_a_id, MOD_A_ID_ROLE)
            pair_item.setData(first.mod_b_id, MOD_B_ID_ROLE)
            level_item = QStandardItem(LEVEL_LABELS.get(worst, worst))

            # Show winner in the detail column for byte_range conflicts
            winner = first.winner_name if worst == "byte_range" and first.winner_name else ""
            detail_text = f"Winner: {winner}" if winner else f"{len(pair_conflicts)} issue(s)"
            detail_item = QStandardItem(detail_text)
            if winner:
                detail_item.setForeground(QColor("#4CAF50"))

            for c in pair_conflicts:
                file_item = QStandardItem(c.file_path)
                file_item.setData(c.mod_a_id, MOD_A_ID_ROLE)
                file_item.setData(c.mod_b_id, MOD_B_ID_ROLE)
                file_item.setData(c.winner_id, WINNER_ID_ROLE)
                file_level = QStandardItem(LEVEL_LABELS.get(c.level, c.level))
                file_level.setForeground(LEVEL_COLORS.get(c.level, QColor("#999")))
                file_detail = QStandardItem(c.explanation)
                pair_item.appendRow([file_item, file_level, file_detail])

            self._model.appendRow([pair_item, level_item, detail_item])

        self._tree.expandAll()

    def _show_context_menu(self, pos) -> None:
        """Show right-click menu with Set Winner options."""
        index = self._tree.indexAt(pos)
        if not index.isValid():
            return

        # Get the first column item (where mod IDs are stored)
        item = self._model.itemFromIndex(index.siblingAtColumn(0))
        if not item:
            return

        mod_a_id = item.data(MOD_A_ID_ROLE)
        mod_b_id = item.data(MOD_B_ID_ROLE)
        if mod_a_id is None or mod_b_id is None:
            return

        # Look up mod names from the tree
        mod_a_name = None
        mod_b_name = None
        # Walk up to pair level to get names
        parent = item.parent() or item
        text = parent.text()
        if " ↔ " in text:
            parts = text.split(" ↔ ")
            mod_a_name = parts[0]
            mod_b_name = parts[1] if len(parts) > 1 else None

        menu = QMenu(self)
        if mod_a_name:
            action_a = QAction(f"Set \"{mod_a_name}\" as winner", self)
            action_a.triggered.connect(lambda: self.winner_changed.emit(mod_a_id))
            menu.addAction(action_a)
        if mod_b_name:
            action_b = QAction(f"Set \"{mod_b_name}\" as winner", self)
            action_b.triggered.connect(lambda: self.winner_changed.emit(mod_b_id))
            menu.addAction(action_b)

        if not menu.isEmpty():
            menu.exec(self._tree.viewport().mapToGlobal(pos))
