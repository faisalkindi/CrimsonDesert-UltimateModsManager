"""ASI plugin management panel widget."""
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cdumm.asi.asi_manager import AsiManager

logger = logging.getLogger(__name__)


class AsiPanel(QWidget):
    """Panel for viewing and managing ASI plugins."""

    def __init__(self, bin64_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._asi_mgr = AsiManager(bin64_dir)
        self._plugins = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)

        # Header
        header = QHBoxLayout()
        title = QLabel("ASI Plugins")
        title.setStyleSheet("font-size: 14px; font-weight: 600; padding-left: 8px;")
        header.addWidget(title)
        self._loader_label = QLabel()
        header.addWidget(self._loader_label)
        header.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        # Table — 3 columns, no inline buttons
        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Plugin", "Status", "Conflicts"])
        from PySide6.QtWidgets import QHeaderView
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        from PySide6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence.StandardKey.SelectAll, self._table, self._table.selectAll)
        layout.addWidget(self._table)

        # Hint
        hint = QLabel("Right-click a plugin for actions")
        hint.setStyleSheet("color: #4E5564; font-size: 11px; padding: 4px 8px;")
        layout.addWidget(hint)

        self.refresh()

    def refresh(self) -> None:
        # Always check for updates (even if loader exists)
        self._install_bundled_loader()

        if self._asi_mgr.has_loader():
            self._loader_label.setText("ASI Loader: Installed")
            self._loader_label.setStyleSheet("color: #48A858; font-weight: 600;")
        else:
            self._loader_label.setText("ASI Loader: Missing")
            self._loader_label.setStyleSheet("color: #D04848; font-weight: 600;")

        self._plugins = self._asi_mgr.scan()
        conflicts = self._asi_mgr.detect_conflicts(self._plugins)

        # Populate table

        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._plugins))

        for row, plugin in enumerate(self._plugins):
            # Name
            name_item = QTableWidgetItem(plugin.name)
            name_item.setData(Qt.ItemDataRole.UserRole, row)  # store index
            self._table.setItem(row, 0, name_item)

            # Status with color
            status = "Enabled" if plugin.enabled else "Disabled"
            status_item = QTableWidgetItem(status)
            if plugin.enabled:
                status_item.setForeground(QColor("#48A858"))
            else:
                status_item.setForeground(QColor("#788090"))
            self._table.setItem(row, 1, status_item)

            # Conflicts
            plugin_conflicts = [c for c in conflicts
                                if c.plugin_a == plugin.name or c.plugin_b == plugin.name]
            if plugin_conflicts:
                text = "; ".join(c.reason for c in plugin_conflicts)
                item = QTableWidgetItem(text)
                item.setForeground(QColor("#D04848"))
            else:
                item = QTableWidgetItem("None")
                item.setForeground(QColor("#4E5564"))
            self._table.setItem(row, 2, item)

        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()

    def _install_bundled_loader(self) -> None:
        """Install or update the bundled ASI loader (winmm.dll) to bin64."""
        import sys, shutil
        if getattr(sys, 'frozen', False):
            bundled = Path(sys._MEIPASS) / "asi_loader" / "winmm.dll"
        else:
            bundled = Path(__file__).resolve().parents[3] / "asi_loader" / "winmm.dll"
        if not bundled.exists():
            return
        dst = self._asi_mgr._bin64 / "winmm.dll"
        # SHA-512 of the current bundled ASI loader
        _BUNDLED_HASH = (
            "d257f4639a831e31e10e2d912032604ae088cdefd2c2da5fe6f06ba49616f16a"
            "bc5795b010687e62b88bcb38508f561e5d61ffa4bb79211fe35bda1e1c4c4efa"
        )
        if dst.exists():
            import hashlib
            dst_hash = hashlib.sha512(dst.read_bytes()).hexdigest()
            if dst_hash == _BUNDLED_HASH:
                return  # already up to date
            logger.info("Updating ASI loader: %s (hash mismatch)", dst)
            updating = True
        else:
            updating = False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, dst)
            if updating:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(
                    self, "ASI Loader Updated",
                    "The ASI Loader (winmm.dll) has been updated to a newer version.")
                logger.info("Updated ASI loader: %s", dst)
            else:
                logger.info("Installed bundled ASI loader: %s", dst)
        except Exception as e:
            logger.warning("Failed to install ASI loader: %s", e)

    def _get_plugin_at_row(self, row: int):
        item = self._table.item(row, 0)
        if item is None:
            return None
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is not None and idx < len(self._plugins):
            return self._plugins[idx]
        return None

    def _show_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        plugin = self._get_plugin_at_row(index.row())
        if not plugin:
            return

        menu = QMenu(self)

        # Enable/Disable — supports multi-select
        selected_rows = set(item.row() for item in self._table.selectedItems())
        if len(selected_rows) > 1:
            plugins = [self._get_plugin_at_row(r) for r in selected_rows]
            plugins = [p for p in plugins if p]
            enabled_count = sum(1 for p in plugins if p.enabled)
            disabled_count = len(plugins) - enabled_count
            if disabled_count > 0:
                enable_action = QAction(f"Enable {disabled_count} plugins", self)
                enable_action.triggered.connect(lambda: self._toggle_selected_plugins(True))
                menu.addAction(enable_action)
            if enabled_count > 0:
                disable_action = QAction(f"Disable {enabled_count} plugins", self)
                disable_action.triggered.connect(lambda: self._toggle_selected_plugins(False))
                menu.addAction(disable_action)
        else:
            if plugin.enabled:
                toggle = QAction("Disable", self)
            else:
                toggle = QAction("Enable", self)
            toggle.triggered.connect(lambda: self._toggle_plugin(plugin))
            menu.addAction(toggle)

            # Config (if .ini exists) — single plugin only
            if plugin.ini_path:
                config = QAction("Edit Config", self)
                config.triggered.connect(lambda: self._asi_mgr.open_config(plugin))
                menu.addAction(config)

            menu.addSeparator()

            # Update — single plugin only
            update = QAction("Update", self)
            update.triggered.connect(lambda: self._update_plugin(plugin))
            menu.addAction(update)

            # Uninstall — single plugin only
            uninstall = QAction("Uninstall", self)
            uninstall.triggered.connect(lambda: self._uninstall_plugin(plugin))
            menu.addAction(uninstall)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _toggle_plugin(self, plugin) -> None:
        if plugin.enabled:
            self._asi_mgr.disable(plugin)
        else:
            self._asi_mgr.enable(plugin)

    def _toggle_selected_plugins(self, enabled: bool) -> None:
        rows = set(item.row() for item in self._table.selectedItems())
        for row in rows:
            plugin = self._get_plugin_at_row(row)
            if plugin:
                if enabled:
                    self._asi_mgr.enable(plugin)
                else:
                    self._asi_mgr.disable(plugin)
        self.refresh()

    def _update_plugin(self, plugin) -> None:
        path_str = QFileDialog.getExistingDirectory(
            self, f"Update {plugin.name} — Select folder containing the new .asi")
        if not path_str:
            return
        folder = Path(path_str)
        # Find .asi files in the folder
        asi_files = list(folder.glob("*.asi"))
        if not asi_files:
            # Check one level deep
            asi_files = list(folder.rglob("*.asi"))
        if not asi_files:
            QMessageBox.warning(self, "No ASI Found", "No .asi files found in that folder.")
            return
        # Pick the one matching the plugin name, or the first one
        match = next((f for f in asi_files if plugin.name.lower() in f.stem.lower()), asi_files[0])
        updated = self._asi_mgr.update(plugin, match)
        if updated:
            QMessageBox.information(
                self, "Updated",
                f"Updated {plugin.name}:\n" + "\n".join(f"  {f}" for f in updated))
            self.refresh()

    def _uninstall_plugin(self, plugin) -> None:
        reply = QMessageBox.question(
            self, "Uninstall ASI Plugin",
            f"Delete {plugin.name} from bin64?\n\n"
            f"Files: {plugin.path.name}"
            f"{', ' + plugin.ini_path.name if plugin.ini_path else ''}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = self._asi_mgr.uninstall(plugin)
            if deleted:
                self.refresh()
