"""Dialog showing which game files a mod touches."""
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QApplication,
)

from cdumm.engine.mod_manager import ModManager


class ModContentsDialog(QDialog):
    def __init__(self, mod: dict, mod_manager: ModManager, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Mod Contents: {mod['name']}")
        self.setMinimumSize(600, 400)

        layout = QVBoxLayout(self)

        # Mod info
        info = f"Name: {mod['name']}"
        if mod.get("author"):
            info += f"  |  Author: {mod['author']}"
        if mod.get("version"):
            info += f"  |  Version: {mod['version']}"
        layout.addWidget(QLabel(info))

        if mod.get("description"):
            layout.addWidget(QLabel(mod["description"]))

        if mod.get("notes"):
            notes_label = QLabel(f"Notes: {mod['notes']}")
            notes_label.setWordWrap(True)
            notes_label.setStyleSheet("color: #B0B8C8; font-style: italic; padding: 4px 0;")
            layout.addWidget(notes_label)

        # File tree
        details = mod_manager.get_mod_details(mod["id"])
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["File", "Byte Range", "Type"])
        self._tree.setColumnCount(3)

        if details:
            # Group by directory
            dirs: dict[str, list] = {}
            for cf in details["changed_files"]:
                fp = cf["file_path"]
                d = fp.split("/")[0] if "/" in fp else ""
                dirs.setdefault(d, []).append(cf)

            for dir_name in sorted(dirs.keys()):
                dir_item = QTreeWidgetItem([dir_name or "(root)", "", ""])
                dir_item.setExpanded(True)
                for cf in dirs[dir_name]:
                    bs, be = cf.get("byte_start"), cf.get("byte_end")
                    range_str = f"{bs:,} - {be:,}" if bs is not None and be is not None else ""
                    file_name = cf["file_path"].split("/")[-1] if "/" in cf["file_path"] else cf["file_path"]
                    ftype = "new file" if cf.get("byte_start") == 0 and cf.get("byte_end") and cf.get("byte_end") > 0 else "modified"
                    child = QTreeWidgetItem([file_name, range_str, ftype])
                    dir_item.addChild(child)
                self._tree.addTopLevelItem(dir_item)

            self._tree.resizeColumnToContents(0)
            self._tree.resizeColumnToContents(1)

        layout.addWidget(self._tree)

        # Buttons
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(self._copy)
        btn_row.addWidget(copy_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _copy(self) -> None:
        lines = [self.windowTitle(), ""]
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            lines.append(f"{item.text(0)}/")
            for j in range(item.childCount()):
                child = item.child(j)
                lines.append(f"  {child.text(0)}  {child.text(1)}  {child.text(2)}")
        QApplication.clipboard().setText("\n".join(lines))
