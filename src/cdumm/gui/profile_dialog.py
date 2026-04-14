"""Mod profile management dialog."""
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QListWidget, QListWidgetItem,
    QVBoxLayout,
)

from qfluentwidgets import (
    BodyLabel,
    MessageBox,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from cdumm.engine.profile_manager import ProfileManager
from cdumm.storage.database import Database


class ProfileDialog(MessageBoxBase):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._pm = ProfileManager(db)
        self._profile_loaded = False

        self.titleLabel = SubtitleLabel("Mod Profiles")
        self.viewLayout.addWidget(self.titleLabel)

        body = QHBoxLayout()

        # Left: profile list
        left = QVBoxLayout()
        left.addWidget(BodyLabel("Saved Profiles:"))
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_selection_changed)
        left.addWidget(self._list)

        btn_row = QHBoxLayout()
        save_btn = PushButton("Save Current")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        delete_btn = PushButton("Delete")
        delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(delete_btn)
        rename_btn = PushButton("Rename")
        rename_btn.clicked.connect(self._on_rename)
        btn_row.addWidget(rename_btn)
        left.addLayout(btn_row)

        load_btn = PrimaryPushButton("Load Selected Profile")
        load_btn.clicked.connect(self._on_load)
        left.addWidget(load_btn)

        body.addLayout(left, 2)

        # Right: preview
        right = QVBoxLayout()
        right.addWidget(BodyLabel("Mods in profile:"))
        self._preview = QListWidget()
        right.addWidget(self._preview)
        body.addLayout(right, 3)

        self.viewLayout.addLayout(body)

        # Override default buttons
        self.yesButton.setText("Close")
        self.cancelButton.hide()

        self.widget.setMinimumWidth(550)

        self._refresh()

    def _refresh(self) -> None:
        self._list.clear()
        for p in self._pm.list_profiles():
            item = QListWidgetItem(p["name"])
            item.setData(256, p["id"])  # Qt.UserRole
            self._list.addItem(item)

    def _on_selection_changed(self, row: int) -> None:
        self._preview.clear()
        item = self._list.item(row)
        if not item:
            return
        pid = item.data(256)
        for mod in self._pm.get_profile_mods(pid):
            status = "ON" if mod["enabled"] else "off"
            self._preview.addItem(f"[{status}] {mod['name']}")

    def _on_save(self) -> None:
        name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
        if ok and name.strip():
            self._pm.save_profile(name.strip())
            self._refresh()

    def _on_load(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        pid = item.data(256)
        name = item.text()
        w = MessageBox(
            "Load Profile",
            f"Load profile '{name}'?\n\nThis will change which mods are enabled/disabled.",
            self,
        )
        if w.exec():
            self._pm.load_profile(pid)
            self._profile_loaded = True
            self.accept()

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        w = MessageBox(
            "Delete Profile",
            f"Delete profile '{item.text()}'?",
            self,
        )
        if w.exec():
            self._pm.delete_profile(item.data(256))
            self._refresh()

    def _on_rename(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        name, ok = QInputDialog.getText(self, "Rename Profile", "New name:", text=item.text())
        if ok and name.strip():
            self._pm.rename_profile(item.data(256), name.strip())
            self._refresh()

    @property
    def was_profile_loaded(self) -> bool:
        return self._profile_loaded
