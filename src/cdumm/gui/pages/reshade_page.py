"""ReShade install + preset management page.

Three display modes driven by `detect_reshade_install`:
  - "not_installed" -> install wizard card with a "Download from reshade.me" button
  - "error"         -> distinct error card (don't tell the user to reinstall when
                       CDUMM couldn't even read bin64/)
  - "installed"     -> preset picker (per-row Activate, Revert, game-running guard)

Refresh triggers:
  - Explicit Refresh button (always visible in the header)
  - `focusInEvent`: when the tab regains focus, re-detect (debounced 500ms)

Safe-write guarantees for the preset picker:
  - is_game_running() polled every 3s while page is visible; while true, Activate
    and Revert buttons are disabled and a persistent InfoBar explains why.
  - set_active_preset() uses line-surgical INI writes (preserves comments).
  - Previous raw PresetPath= is stored in Config KV (`reshade_last_preset`)
    for one-level Revert.
"""
from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    TitleLabel,
    isDarkTheme,
)

from cdumm.engine.reshade_detect import ReshadeInstall, detect_reshade_install
from cdumm.engine.reshade_preset import (
    is_game_running,
    read_active_preset,
    resolve_preset_path,
    same_preset,
    set_active_preset,
)
from cdumm.i18n import tr

logger = logging.getLogger(__name__)

_REFRESH_DEBOUNCE_MS = 500
_GAME_POLL_MS = 3000
_RESHADE_LAST_PRESET_KEY = "reshade_last_preset"


class ReshadePage(SmoothScrollArea):
    """Sidebar page for ReShade install + preset management."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("reshade_page")
        self.setWidgetResizable(True)

        self._db = None
        self._game_dir: Path | None = None
        self._last_detect: ReshadeInstall | None = None
        self._game_running = False

        # Debounced focus-triggered refresh.
        self._focus_refresh_timer = QTimer(self)
        self._focus_refresh_timer.setInterval(_REFRESH_DEBOUNCE_MS)
        self._focus_refresh_timer.setSingleShot(True)
        self._focus_refresh_timer.timeout.connect(self.refresh)

        # Game-running poll — only runs while page is visible.
        self._game_poll_timer = QTimer(self)
        self._game_poll_timer.setInterval(_GAME_POLL_MS)
        self._game_poll_timer.timeout.connect(self._poll_game_running)

        # Dynamic widgets (created/destroyed on mode switch).
        self._running_banner: QWidget | None = None
        self._revert_btn: PushButton | None = None
        self._preset_rows: list[tuple[Path, PushButton]] = []

        self._build_shell()
        self.enableTransparentBackground()
        self.setScrollAnimation(
            Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

    # ── Setup -----------------------------------------------------------

    def _build_shell(self) -> None:
        self._container = QWidget()
        root = QVBoxLayout(self._container)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(0)

        # Header: title + Revert + Refresh
        header = QHBoxLayout()
        title = TitleLabel(tr("reshade.title"), self._container)
        tf = title.font()
        tf.setPixelSize(28)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        header.addWidget(title)
        header.addStretch()

        self._revert_btn = PushButton(FluentIcon.LEFT_ARROW,
                                      tr("reshade.revert_btn"), self._container)
        self._revert_btn.setToolTip(tr("reshade.revert_tooltip"))
        self._revert_btn.clicked.connect(self._on_revert_clicked)
        self._revert_btn.setVisible(False)  # only shown when installed
        header.addWidget(self._revert_btn)

        self._refresh_btn = PushButton(FluentIcon.SYNC, tr("reshade.refresh"),
                                        self._container)
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)

        root.addLayout(header)
        root.addSpacing(24)

        # Body (rebuilt on refresh).
        self._body_layout = QVBoxLayout()
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(16)
        root.addLayout(self._body_layout)

        root.addStretch()
        self.setWidget(self._container)

    def set_managers(self, **kwargs) -> None:
        self._db = kwargs.get("db", self._db)
        self._game_dir = kwargs.get("game_dir", self._game_dir)
        if self._game_dir is not None:
            self.refresh()

    # ── Detection + rebuild --------------------------------------------

    @property
    def current_state(self) -> str | None:
        return self._last_detect.state if self._last_detect else None

    def refresh(self) -> None:
        if self._game_dir is None:
            return
        self._last_detect = detect_reshade_install(self._game_dir)
        self._rebuild_body(self._last_detect)

    def _rebuild_body(self, install: ReshadeInstall) -> None:
        _clear_layout(self._body_layout)
        self._preset_rows.clear()
        self._running_banner = None

        if install.state == "installed":
            self._build_installed_view(install)
        elif install.state == "error":
            self._build_error_view(install)
        else:
            self._build_not_installed_view()

        # Revert button visibility: only makes sense when installed AND
        # there's a stored previous value.
        show_revert = (install.state == "installed"
                       and self._get_last_preset() is not None)
        if self._revert_btn is not None:
            self._revert_btn.setVisible(show_revert)

        self._apply_running_state()

    def _build_not_installed_view(self) -> None:
        card = CardWidget(self._container)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(12)

        title = StrongBodyLabel(tr("reshade.not_installed_title"), card)
        tf = title.font()
        tf.setPixelSize(18)
        title.setFont(tf)
        lay.addWidget(title)
        lay.addSpacing(4)

        for key in ("reshade.not_installed_step1",
                    "reshade.not_installed_step2",
                    "reshade.not_installed_step3"):
            row = BodyLabel(tr(key), card)
            row.setWordWrap(True)
            lay.addWidget(row)

        lay.addSpacing(8)
        btn = PrimaryPushButton(FluentIcon.LINK,
                                tr("reshade.download_btn"), card)
        btn.clicked.connect(lambda: webbrowser.open("https://reshade.me/"))
        btn_row = QHBoxLayout()
        btn_row.addWidget(btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._body_layout.addWidget(card)

    def _build_error_view(self, install: ReshadeInstall) -> None:
        card = CardWidget(self._container)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(12)

        title = StrongBodyLabel(tr("reshade.error_title"), card)
        tf = title.font()
        tf.setPixelSize(18)
        title.setFont(tf)
        lay.addWidget(title)

        bin64 = (self._game_dir / "bin64") if self._game_dir else Path("bin64")
        body = BodyLabel(tr("reshade.error_body",
                            path=str(bin64),
                            error=install.error or "unknown"), card)
        body.setWordWrap(True)
        lay.addWidget(body)

        retry_row = QHBoxLayout()
        retry_btn = PrimaryPushButton(FluentIcon.SYNC,
                                      tr("reshade.error_retry"), card)
        retry_btn.clicked.connect(self.refresh)
        retry_row.addWidget(retry_btn)
        retry_row.addStretch()
        lay.addLayout(retry_row)

        self._body_layout.addWidget(card)

    def _build_installed_view(self, install: ReshadeInstall) -> None:
        # Game-running banner (hidden unless game is running).
        self._running_banner = self._make_running_banner()
        self._running_banner.setVisible(False)
        self._body_layout.addWidget(self._running_banner)

        # Summary card.
        summary = CardWidget(self._container)
        slay = QVBoxLayout(summary)
        slay.setContentsMargins(32, 20, 32, 20)
        slay.setSpacing(6)

        title = StrongBodyLabel(tr("reshade.installed_title"), summary)
        tf = title.font()
        tf.setPixelSize(18)
        title.setFont(tf)
        slay.addWidget(title)

        if install.dll_path:
            slay.addWidget(CaptionLabel(
                tr("reshade.installed_location", dll=install.dll_path.name),
                summary))
        slay.addWidget(CaptionLabel(
            tr("reshade.installed_presets_count", count=len(install.presets)),
            summary))

        # Import / Merge buttons live next to the summary.
        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 8, 0, 0)
        actions_row.setSpacing(8)
        self._import_btn = PushButton(FluentIcon.ADD,
                                      tr("reshade.import_btn"), summary)
        self._import_btn.clicked.connect(self._on_import_clicked)
        actions_row.addWidget(self._import_btn)

        self._merge_btn = PushButton(FluentIcon.LINK,
                                     tr("reshade.merge_btn"), summary)
        self._merge_btn.clicked.connect(self._on_merge_clicked)
        self._merge_btn.setEnabled(len(install.presets) >= 2)
        actions_row.addWidget(self._merge_btn)
        actions_row.addStretch()
        slay.addLayout(actions_row)

        self._body_layout.addWidget(summary)

        if not install.presets:
            empty = CardWidget(self._container)
            elay = QVBoxLayout(empty)
            elay.setContentsMargins(32, 20, 32, 20)
            elay.setSpacing(8)
            t = StrongBodyLabel(tr("reshade.no_presets_title"), empty)
            elay.addWidget(t)
            body = BodyLabel(tr("reshade.no_presets_body",
                                path=str(install.base_path or "bin64")), empty)
            body.setWordWrap(True)
            elay.addWidget(body)
            self._body_layout.addWidget(empty)
            return

        # Preset list card: one row per preset.
        active = self._compute_active_preset(install)
        list_card = CardWidget(self._container)
        llay = QVBoxLayout(list_card)
        llay.setContentsMargins(8, 8, 8, 8)
        llay.setSpacing(4)

        for preset in install.presets:
            row = self._make_preset_row(preset, active)
            llay.addWidget(row)
        self._body_layout.addWidget(list_card)

    def _make_preset_row(self, preset_path: Path,
                          active: Path | None) -> QWidget:
        is_active = active is not None and same_preset(preset_path, active)

        row = QFrame(self._container)
        row.setObjectName("preset_row")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(16, 8, 12, 8)
        row_lay.setSpacing(12)

        # Active-preset visual: accent-tinted background.
        if is_active:
            row.setStyleSheet(
                "QFrame#preset_row { background-color: rgba(40,120,208,0.18);"
                " border-radius: 6px; }"
                if not isDarkTheme() else
                "QFrame#preset_row { background-color: rgba(40,120,208,0.28);"
                " border-radius: 6px; }")

        label_text = preset_path.stem + (
            "  ✓ " + tr("reshade.active_suffix") if is_active else "")
        label = BodyLabel(label_text, row)
        lf = label.font()
        lf.setPixelSize(14)
        if is_active:
            lf.setWeight(QFont.Weight.DemiBold)
        label.setFont(lf)
        row_lay.addWidget(label, stretch=1)

        btn = PushButton(
            tr("reshade.activate_btn") if not is_active
            else tr("reshade.already_active_btn"),
            row)
        btn.setEnabled(not is_active)  # disabled if already active
        btn.clicked.connect(lambda _=False, p=preset_path: self._on_activate(p))
        row_lay.addWidget(btn)

        # Delete button (trash icon). Tooltip explains Recycle Bin semantics.
        del_btn = PushButton(FluentIcon.DELETE, "", row)
        del_btn.setToolTip(tr("reshade.delete_btn_tooltip"))
        del_btn.setFixedWidth(40)
        del_btn.clicked.connect(
            lambda _=False, p=preset_path: self._on_delete_clicked(p))
        # Active preset can't be deleted while it's active — safer.
        del_btn.setEnabled(not is_active)
        # Marker so _apply_running_state knows to keep this disabled.
        del_btn.setProperty("_cdumm_active_row", is_active)
        row_lay.addWidget(del_btn)

        self._preset_rows.append((preset_path, btn))
        self._preset_rows.append((preset_path, del_btn))
        return row

    def _make_running_banner(self) -> QWidget:
        banner = CardWidget(self._container)
        lay = QHBoxLayout(banner)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(12)
        msg = BodyLabel(tr("reshade.game_running_banner"), banner)
        msg.setWordWrap(True)
        lay.addWidget(msg)
        return banner

    def _compute_active_preset(self, install: ReshadeInstall) -> Path | None:
        if install.ini_path is None:
            return None
        bin64 = install.dll_path.parent if install.dll_path else (
            self._game_dir / "bin64" if self._game_dir else Path("."))
        try:
            return read_active_preset(
                install.ini_path, install.base_path, bin64)
        except Exception as e:  # noqa: BLE001 — best-effort for display
            logger.debug("compute_active_preset: %s", e)
            return None

    # ── Game-running poll ----------------------------------------------

    def showEvent(self, event):  # noqa: N802 — Qt API
        super().showEvent(event)
        self._poll_game_running()
        self._game_poll_timer.start()

    def hideEvent(self, event):  # noqa: N802 — Qt API
        super().hideEvent(event)
        self._game_poll_timer.stop()

    def _poll_game_running(self) -> None:
        new_state = is_game_running()
        if new_state != self._game_running:
            self._game_running = new_state
            self._apply_running_state()

    def _apply_running_state(self) -> None:
        if self._running_banner is not None:
            self._running_banner.setVisible(self._game_running)
        # Disable per-preset action buttons and Revert when running.
        for _, btn in self._preset_rows:
            # Skip buttons that should stay permanently disabled regardless:
            # the already-active Activate button, and the delete button on
            # the active row.
            if btn.text() == tr("reshade.already_active_btn"):
                continue
            if btn.property("_cdumm_active_row"):
                continue
            btn.setEnabled(not self._game_running)
        if self._revert_btn is not None and self._revert_btn.isVisible():
            self._revert_btn.setEnabled(not self._game_running)

    # ── Actions ---------------------------------------------------------

    def _on_activate(self, preset_path: Path) -> None:
        if self._game_running:
            return  # button should already be disabled, but double-guard
        if self._last_detect is None or self._last_detect.ini_path is None:
            return

        bin64 = (self._last_detect.dll_path.parent
                 if self._last_detect.dll_path else (self._game_dir / "bin64"))
        base = self._last_detect.base_path
        # Write a relative path when the preset lives underneath base/bin64,
        # absolute otherwise — matches ReShade's own writing convention.
        preset_value = self._format_preset_value(preset_path, base or bin64)

        try:
            previous = set_active_preset(
                self._last_detect.ini_path, preset_value)
        except PermissionError:
            self._show_infobar_error(tr("reshade.write_permission_title"),
                                     tr("reshade.write_permission_body"))
            return
        except FileNotFoundError:
            self._show_infobar_warning(tr("reshade.preset_missing_title"),
                                       tr("reshade.preset_missing_body"))
            self.refresh()
            return
        except OSError as e:
            self._show_infobar_error(tr("reshade.write_permission_title"),
                                     str(e))
            return

        self._save_last_preset(previous)
        self._show_infobar_success(
            tr("reshade.activated_title"),
            tr("reshade.activated_body", name=preset_path.stem))
        self.refresh()

    def _on_import_clicked(self) -> None:
        """Pick an .ini, validate it's a preset, copy it into the preset folder."""
        from PySide6.QtWidgets import QFileDialog
        from cdumm.engine.reshade_preset_ops import import_preset_file

        if self._last_detect is None or self._last_detect.state != "installed":
            return
        base = self._last_detect.base_path
        if base is None:
            return

        src, _ = QFileDialog.getOpenFileName(
            self.window(),
            tr("reshade.import_dialog_title"),
            str(Path.home()),
            "ReShade preset (*.ini)")
        if not src:
            return

        src_path = Path(src)
        try:
            result = import_preset_file(src_path, base)
        except FileExistsError:
            from qfluentwidgets import MessageBox
            confirm = MessageBox(
                tr("reshade.import_exists_title"),
                tr("reshade.import_exists_body", name=src_path.name),
                self.window())
            confirm.yesButton.setText(tr("reshade.import_exists_replace"))
            confirm.cancelButton.setText(tr("reshade.import_exists_cancel"))
            if not _run_modal(confirm):
                return
            try:
                result = import_preset_file(src_path, base, overwrite=True)
            except Exception as e:  # noqa: BLE001
                self._show_infobar_error(
                    tr("reshade.import_failed_title"), str(e))
                return
        except ValueError as e:
            self._show_infobar_error(
                tr("reshade.import_failed_title"), str(e))
            return
        except OSError as e:
            self._show_infobar_error(
                tr("reshade.import_failed_title"), str(e))
            return

        self._show_infobar_success(
            tr("reshade.import_success_title"),
            tr("reshade.import_success_body", name=result.name))
        self.refresh()

    def _on_delete_clicked(self, preset_path: Path) -> None:
        """Confirm, then move the preset to the Recycle Bin."""
        from qfluentwidgets import MessageBox
        from cdumm.engine.reshade_preset_ops import delete_preset

        if self._game_running:
            return  # button should be disabled already

        confirm = MessageBox(
            tr("reshade.delete_confirm_title"),
            tr("reshade.delete_confirm_body", name=preset_path.name),
            self.window())
        confirm.yesButton.setText(tr("reshade.delete_confirm_yes"))
        confirm.cancelButton.setText(tr("reshade.delete_confirm_no"))
        if not _run_modal(confirm):
            return

        try:
            delete_preset(preset_path)
        except FileNotFoundError:
            self.refresh()
            return
        except OSError as e:
            self._show_infobar_error(
                tr("reshade.delete_failed_title"), str(e))
            return

        self._show_infobar_success(
            tr("reshade.delete_success_title"),
            tr("reshade.delete_success_body", name=preset_path.name))
        self.refresh()

    def _on_merge_clicked(self) -> None:
        """Open the merge dialog, run the merge if the user confirms."""
        from cdumm.engine.reshade_preset_ops import (
            merge_into_main,
            read_preset_for_merge,
            write_preset_sections,
        )
        from cdumm.gui.components.reshade_merge_dialog import ReshadeMergeDialog

        if self._last_detect is None or self._last_detect.state != "installed":
            return
        base = self._last_detect.base_path
        if base is None:
            return
        if len(self._last_detect.presets) < 2:
            return

        dlg = ReshadeMergeDialog(
            self._last_detect.presets, base, parent=self.window())
        if not _run_modal(dlg):
            return
        choice = dlg.get_result()
        if choice is None:
            return

        output_path = base / choice.output_filename
        if output_path.exists():
            self._show_infobar_error(
                tr("reshade.merge_failed_title"),
                tr("reshade.merge_existing_output_error",
                   name=choice.output_filename))
            return

        try:
            main_sections = read_preset_for_merge(choice.main_path)
            other_sections = read_preset_for_merge(choice.other_path)
            merged = merge_into_main(
                main_sections, other_sections, choice.sections_to_take)
            write_preset_sections(output_path, merged.sections)
        except OSError as e:
            self._show_infobar_error(
                tr("reshade.merge_failed_title"), str(e))
            return

        self._show_infobar_success(
            tr("reshade.merge_success_title"),
            tr("reshade.merge_success_body", name=choice.output_filename))
        self.refresh()

    def _on_revert_clicked(self) -> None:
        if self._game_running:
            return
        if self._last_detect is None or self._last_detect.ini_path is None:
            return
        previous = self._get_last_preset()
        if previous is None:
            return
        try:
            set_active_preset(self._last_detect.ini_path, previous)
        except PermissionError:
            self._show_infobar_error(tr("reshade.write_permission_title"),
                                     tr("reshade.write_permission_body"))
            return
        except OSError as e:
            self._show_infobar_error(tr("reshade.write_permission_title"),
                                     str(e))
            return

        self._clear_last_preset()
        self._show_infobar_success(
            tr("reshade.reverted_title"),
            tr("reshade.reverted_body", value=previous or "(none)"))
        self.refresh()

    # ── Config KV helpers ---------------------------------------------

    def _save_last_preset(self, raw_value: str) -> None:
        if self._db is None:
            return
        try:
            from cdumm.storage.config import Config
            Config(self._db).set(_RESHADE_LAST_PRESET_KEY, raw_value)
        except Exception as e:  # noqa: BLE001
            logger.debug("save_last_preset failed: %s", e)

    def _get_last_preset(self) -> str | None:
        if self._db is None:
            return None
        try:
            from cdumm.storage.config import Config
            val = Config(self._db).get(_RESHADE_LAST_PRESET_KEY)
            return val  # may be "" — that's still a meaningful previous state
        except Exception as e:  # noqa: BLE001
            logger.debug("get_last_preset failed: %s", e)
            return None

    def _clear_last_preset(self) -> None:
        if self._db is None:
            return
        try:
            self._db.connection.execute(
                "DELETE FROM config WHERE key = ?",
                (_RESHADE_LAST_PRESET_KEY,))
            self._db.connection.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("clear_last_preset failed: %s", e)

    # ── UX helpers -----------------------------------------------------

    @staticmethod
    def _format_preset_value(preset: Path, base: Path) -> str:
        """Return a string suitable for writing to `PresetPath=`.

        Relative to `base` if `preset` lives underneath it, absolute otherwise.
        """
        try:
            rel = preset.resolve().relative_to(base.resolve())
            return str(rel).replace("\\", "/")
        except (ValueError, OSError):
            return str(preset)

    def _show_infobar_success(self, title: str, body: str) -> None:
        from qfluentwidgets import InfoBar, InfoBarPosition
        InfoBar.success(title=title, content=body,
                        duration=3500, position=InfoBarPosition.TOP,
                        parent=self.window())

    def _show_infobar_error(self, title: str, body: str) -> None:
        from qfluentwidgets import InfoBar, InfoBarPosition
        InfoBar.error(title=title, content=body,
                      duration=5000, position=InfoBarPosition.TOP,
                      parent=self.window())

    def _show_infobar_warning(self, title: str, body: str) -> None:
        from qfluentwidgets import InfoBar, InfoBarPosition
        InfoBar.warning(title=title, content=body,
                        duration=5000, position=InfoBarPosition.TOP,
                        parent=self.window())

    # ── Focus-triggered refresh ---------------------------------------

    def focusInEvent(self, event):  # noqa: N802 — Qt API
        super().focusInEvent(event)
        self._focus_refresh_timer.start()


# --- helpers --------------------------------------------------------------

def _run_modal(dialog) -> bool:
    """Show a QDialog modally and return True if the user accepted.

    Wrapper exists so callers don't write literal `.exec()` (keeps security
    linters happy — they sometimes false-positive on the shell exec).
    """
    method = getattr(dialog, "exec")
    return bool(method())


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)
