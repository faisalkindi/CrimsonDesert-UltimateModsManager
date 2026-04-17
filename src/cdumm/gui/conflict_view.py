import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QHeaderView, QMenu, QTreeView, QVBoxLayout, QWidget,
)
from qfluentwidgets import SmoothScrollDelegate, getFont, isDarkTheme

from cdumm.engine.conflict_detector import Conflict

logger = logging.getLogger(__name__)

LEVEL_COLORS = {
    "papgt": QColor("#4CAF50"),       # green — auto-rebuilt
    "paz": QColor("#4CAF50"),         # green — both apply (compatible)
    "byte_range": QColor("#FF9800"),  # orange — priority decides winner
    "semantic": QColor("#FF9800"),    # orange — priority decides winner
}

LEVEL_LABELS = {
    "papgt": "Auto-rebuilt (metadata)",
    "paz": "Both apply (different parts)",
    "byte_range": "Load order decides winner",
    "semantic": "Field-level merge",
}

# Levels where load-order / priority actually changes the outcome.
# Used by the conflicts dialog to split "needs attention" from "auto".
ACTIONABLE_LEVELS = frozenset({"byte_range", "semantic"})

# Data role for storing mod IDs on tree items
MOD_A_ID_ROLE = Qt.ItemDataRole.UserRole + 1
MOD_B_ID_ROLE = Qt.ItemDataRole.UserRole + 2
WINNER_ID_ROLE = Qt.ItemDataRole.UserRole + 3


class ConflictView(QWidget):
    """Tree view displaying mod conflicts grouped by mod pair → file → details."""

    winner_changed = Signal(int)  # emits mod_id that was set as winner

    @staticmethod
    def _tree_qss_for_theme() -> str:
        """Body-of-tree stylesheet (frame, rows, hover, selection).

        Deliberately NOT setting ``color:`` on ``QTreeView::item`` — QSS
        cell colour beats ``QStandardItem.setForeground()`` via Qt's
        styling cascade, which would wipe out the semantic hue each
        conflict row carries (yellow for paz, orange for byte_range).
        Per-item ``body_color`` in ``update_conflicts`` paints the rest.
        """
        return """
        QTreeView {
            background: transparent;
            border: 1px solid rgba(128, 128, 128, 50);
            border-radius: 8px;
            outline: 0;
            padding: 4px 0;
        }
        QTreeView::item {
            min-height: 28px;
            padding: 4px 6px;
            border: none;
        }
        QTreeView::item:hover {
            background: rgba(128, 128, 128, 28);
            border-radius: 4px;
        }
        QTreeView::item:selected {
            background: rgba(40, 120, 208, 48);
            border-radius: 4px;
        }
        """

    @staticmethod
    def _header_qss_for_theme() -> str:
        """Header-only stylesheet, applied to the header widget itself.

        The Fluent global stylesheet paints ``QHeaderView::section`` dark
        via an ancestor rule. Widget-level stylesheets win over ancestor
        stylesheets regardless of CSS specificity, so painting this at
        ``self._tree.header()`` is the only reliable way to override it.
        """
        if isDarkTheme():
            bg = "#2B2B2B"
            text = "#E8E8E8"
            bottom = "rgba(255, 255, 255, 30)"
        else:
            bg = "#FAFAFA"
            text = "#1F1F1F"
            bottom = "rgba(0, 0, 0, 30)"
        return f"""
        QHeaderView {{
            background: {bg};
        }}
        QHeaderView::section {{
            background: {bg};
            color: {text};
            padding: 8px 10px;
            border: none;
            border-bottom: 1px solid {bottom};
            font-weight: 600;
        }}
        """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tree = QTreeView()
        self._tree.setHeaderHidden(False)
        # Drop alternating bands — the custom QSS already gives rhythm
        # without banding that clashes with Fluent surfaces.
        self._tree.setAlternatingRowColors(False)
        self._tree.setAnimated(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.setStyleSheet(self._tree_qss_for_theme())
        # Widget-level stylesheet on the header itself — beats the Fluent
        # global ``QHeaderView::section`` rule that would otherwise paint
        # the header dark on light theme (and vice versa).
        self._tree.header().setStyleSheet(self._header_qss_for_theme())

        # Force Oxanium via qfluentwidgets' ``getFont`` helper — plain
        # ``QApplication.font()`` still returns the Qt default because
        # ``setFontFamilies()`` stores the family list in qfluentwidgets
        # qconfig without touching the application font.
        self._tree.setFont(getFont(14))
        self._tree.header().setFont(getFont(14, QFont.Weight.DemiBold))

        # Fluent smooth-scroll + themed scrollbars on the tree's internal
        # scroll area. Without this the tree falls back to raw Qt scroll
        # chrome that clashes with the rest of the app.
        self._tree_scroll_delegate = SmoothScrollDelegate(self._tree, useAni=True)
        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(["Conflict", "Level", "Resolution"])
        self._tree.setModel(self._model)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._tree.setColumnWidth(1, 220)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        layout.addWidget(self._tree)

    def update_conflicts(self, conflicts: list[Conflict]) -> None:
        """Rebuild the tree with the current conflict list."""
        self._tree.setUpdatesEnabled(False)
        self._model.blockSignals(True)
        self._model.removeRows(0, self._model.rowCount())

        # Theme-aware default for cells without a semantic colour.
        # QPalette.Text on a Fluent-themed tree can resolve to a muted
        # role that disappears against the light surface, so pick an
        # explicit high-contrast colour per theme instead.
        body_color = QColor("#E8E8E8") if isDarkTheme() else QColor("#1F1F1F")

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

        # Severity ladder — higher index = more user-impact, so a pair
        # with even a single actionable child is surfaced at its colour.
        severity_rank = {"papgt": 0, "paz": 1, "semantic": 2, "byte_range": 3}

        for (_, _), pair_conflicts in pairs.items():
            first = pair_conflicts[0]
            # Determine worst level for this pair (highest severity rank)
            worst = max(pair_conflicts,
                        key=lambda c: severity_rank.get(c.level, 0)).level

            pair_item = QStandardItem(f"{first.mod_a_name} ↔ {first.mod_b_name}")
            pair_item.setForeground(LEVEL_COLORS.get(worst, QColor("#999")))
            pair_item.setData(first.mod_a_id, MOD_A_ID_ROLE)
            pair_item.setData(first.mod_b_id, MOD_B_ID_ROLE)
            level_item = QStandardItem(LEVEL_LABELS.get(worst, worst))
            level_item.setForeground(body_color)

            # Show winner in the detail column for byte_range conflicts
            winner = first.winner_name if worst == "byte_range" and first.winner_name else ""
            detail_text = f"Winner: {winner}" if winner else f"{len(pair_conflicts)} issue(s)"
            detail_item = QStandardItem(detail_text)
            detail_item.setForeground(
                QColor("#4CAF50") if winner else body_color)

            # Cap child items to prevent Qt crash on large conflict sets.
            # Show first 10 + summary if there are more.
            MAX_CHILDREN = 10
            shown = pair_conflicts[:MAX_CHILDREN]
            for c in shown:
                file_item = QStandardItem(c.file_path)
                file_item.setForeground(body_color)
                file_item.setData(c.mod_a_id, MOD_A_ID_ROLE)
                file_item.setData(c.mod_b_id, MOD_B_ID_ROLE)
                file_item.setData(c.winner_id, WINNER_ID_ROLE)
                file_level = QStandardItem(LEVEL_LABELS.get(c.level, c.level))
                file_level.setForeground(LEVEL_COLORS.get(c.level, body_color))
                file_detail = QStandardItem(c.explanation)
                file_detail.setForeground(body_color)
                pair_item.appendRow([file_item, file_level, file_detail])

            if len(pair_conflicts) > MAX_CHILDREN:
                more = QStandardItem(f"... and {len(pair_conflicts) - MAX_CHILDREN} more")
                more.setForeground(body_color)
                pair_item.appendRow([more, QStandardItem(""), QStandardItem("")])

            self._model.appendRow([pair_item, level_item, detail_item])

        self._model.blockSignals(False)
        self._model.layoutChanged.emit()
        self._tree.setUpdatesEnabled(True)
        if len(conflicts) <= 50:
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
