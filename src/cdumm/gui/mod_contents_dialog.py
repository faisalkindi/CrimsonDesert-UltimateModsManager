"""Dialog showing which game files a mod touches."""
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBoxBase,
    PushButton,
    SubtitleLabel,
)

from cdumm.engine.mod_manager import ModManager


class ModContentsDialog(MessageBoxBase):
    def __init__(self, mod: dict, mod_manager: ModManager, parent=None) -> None:
        super().__init__(parent)

        self.titleLabel = SubtitleLabel(f"Mod Contents: {mod['name']}")
        self.viewLayout.addWidget(self.titleLabel)

        # Mod info
        info = f"Name: {mod['name']}"
        if mod.get("author"):
            info += f"  |  Author: {mod['author']}"
        if mod.get("version"):
            info += f"  |  Version: {mod['version']}"
        self.viewLayout.addWidget(BodyLabel(info))

        if mod.get("description"):
            self.viewLayout.addWidget(BodyLabel(mod["description"]))

        if mod.get("notes"):
            notes_label = CaptionLabel(f"Notes: {mod['notes']}")
            notes_label.setWordWrap(True)
            self.viewLayout.addWidget(notes_label)

        # File tree
        details = mod_manager.get_mod_details(mod["id"])
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["File", "Byte Range", "Type"])
        self._tree.setColumnCount(3)
        self._tree.setMinimumHeight(250)

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

        self.viewLayout.addWidget(self._tree)

        # Copy button
        btn_row = QHBoxLayout()
        copy_btn = PushButton("Copy to Clipboard")
        copy_btn.clicked.connect(self._copy)
        btn_row.addWidget(copy_btn)
        btn_row.addStretch()
        self.viewLayout.addLayout(btn_row)

        # Override default buttons
        self.yesButton.setText("Close")
        self.cancelButton.hide()

        self.widget.setMinimumWidth(600)

    def _copy(self) -> None:
        lines = [self.titleLabel.text(), ""]
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            lines.append(f"{item.text(0)}/")
            for j in range(item.childCount()):
                child = item.child(j)
                lines.append(f"  {child.text(0)}  {child.text(1)}  {child.text(2)}")
        QApplication.clipboard().setText("\n".join(lines))
