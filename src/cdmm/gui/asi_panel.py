"""ASI plugin management panel widget."""
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cdmm.asi.asi_manager import AsiManager

logger = logging.getLogger(__name__)


class AsiPanel(QWidget):
    """Panel for viewing and managing ASI plugins."""

    def __init__(self, bin64_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._asi_mgr = AsiManager(bin64_dir)

        layout = QVBoxLayout(self)

        # Header with loader status
        header = QHBoxLayout()
        header.addWidget(QLabel("ASI Plugins"))
        self._loader_label = QLabel()
        header.addWidget(self._loader_label)
        header.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        # Plugin table
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Plugin", "Status", "Actions", "Conflicts"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._table)

        self.refresh()

    def refresh(self) -> None:
        """Rescan bin64 and rebuild table."""
        # Loader status
        if self._asi_mgr.has_loader():
            self._loader_label.setText("ASI Loader: Installed")
            self._loader_label.setStyleSheet("color: green;")
        else:
            self._loader_label.setText("ASI Loader: Missing (winmm.dll)")
            self._loader_label.setStyleSheet("color: red;")

        plugins = self._asi_mgr.scan()
        conflicts = self._asi_mgr.detect_conflicts(plugins)

        self._table.setRowCount(len(plugins))

        for row, plugin in enumerate(plugins):
            # Name
            self._table.setItem(row, 0, QTableWidgetItem(plugin.name))

            # Status
            status = "Enabled" if plugin.enabled else "Disabled"
            status_item = QTableWidgetItem(status)
            status_item.setForeground(
                Qt.GlobalColor.darkGreen if plugin.enabled else Qt.GlobalColor.gray
            )
            self._table.setItem(row, 1, status_item)

            # Actions
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 2, 2, 2)

            toggle_btn = QPushButton("Disable" if plugin.enabled else "Enable")
            toggle_btn.setFixedWidth(70)
            p = plugin  # capture for lambda
            toggle_btn.clicked.connect(lambda checked, pl=p: self._toggle_plugin(pl))
            actions_layout.addWidget(toggle_btn)

            if plugin.ini_path:
                config_btn = QPushButton("Config")
                config_btn.setFixedWidth(60)
                config_btn.clicked.connect(lambda checked, pl=p: self._asi_mgr.open_config(pl))
                actions_layout.addWidget(config_btn)

            update_btn = QPushButton("Update")
            update_btn.setFixedWidth(60)
            update_btn.clicked.connect(lambda checked, pl=p: self._update_plugin(pl))
            actions_layout.addWidget(update_btn)

            uninstall_btn = QPushButton("Uninstall")
            uninstall_btn.setFixedWidth(70)
            uninstall_btn.setStyleSheet("color: #FF8888;")
            uninstall_btn.clicked.connect(lambda checked, pl=p: self._uninstall_plugin(pl))
            actions_layout.addWidget(uninstall_btn)

            self._table.setCellWidget(row, 2, actions)

            # Conflicts
            plugin_conflicts = [c for c in conflicts
                                if c.plugin_a == plugin.name or c.plugin_b == plugin.name]
            if plugin_conflicts:
                conflict_text = "; ".join(c.reason for c in plugin_conflicts)
                conflict_item = QTableWidgetItem(conflict_text)
                conflict_item.setForeground(Qt.GlobalColor.red)
                self._table.setItem(row, 3, conflict_item)
            else:
                self._table.setItem(row, 3, QTableWidgetItem("None"))

        self._table.resizeColumnsToContents()

    def _toggle_plugin(self, plugin) -> None:
        if plugin.enabled:
            self._asi_mgr.disable(plugin)
        else:
            self._asi_mgr.enable(plugin)
        self.refresh()

    def _update_plugin(self, plugin) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"Update {plugin.name}",
            "", "ASI Plugins (*.asi);;All Files (*)")
        if not path:
            return
        from pathlib import Path
        updated = self._asi_mgr.update(plugin, Path(path))
        if updated:
            QMessageBox.information(
                self, "Updated",
                f"Updated {plugin.name}:\n" + "\n".join(f"  {f}" for f in updated))
            self.refresh()

    def _uninstall_plugin(self, plugin) -> None:
        reply = QMessageBox.question(
            self, "Uninstall ASI Plugin",
            f"Delete {plugin.name} and its config from bin64?\n\n"
            f"Files to remove:\n"
            f"  {plugin.path.name}"
            f"{chr(10) + '  ' + plugin.ini_path.name if plugin.ini_path else ''}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = self._asi_mgr.uninstall(plugin)
            if deleted:
                self.refresh()
