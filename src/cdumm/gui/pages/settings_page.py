"""Settings page for CDUMM v3 — SQLite-backed setting cards."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    PushSettingCard,
    SettingCard,
    SettingCardGroup,
    SmoothScrollArea,
    SubtitleLabel,
)

from cdumm.i18n import tr

logger = logging.getLogger(__name__)


class SettingsPage(SmoothScrollArea):
    """Settings page with SQLite-backed setting cards."""

    # Signals to parent window for actions that need engine access
    game_dir_changed = Signal(Path)
    profile_manage_requested = Signal()
    export_list_requested = Signal()
    import_list_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsPage")
        self.setWidgetResizable(True)

        self._db = None
        self._config = None
        self._game_dir = None

        # Content container
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(36, 20, 36, 20)
        self._layout.setSpacing(20)

        self._title_label = SubtitleLabel(tr("settings.title"))
        self._layout.addWidget(self._title_label)

        # ── Personalization group ─────────────────────────────────
        self._personal_group = SettingCardGroup(tr("settings.personalization"), self._container)

        # Theme card — manual SettingCard with ComboBox
        self._theme_card = SettingCard(
            FluentIcon.BRUSH, tr("settings.theme"),
            tr("settings.theme_desc"),
            self._personal_group,
        )
        self._theme_combo = ComboBox()
        self._theme_combo.addItems([
            tr("settings.theme_light"), tr("settings.theme_dark"), tr("settings.theme_auto"),
        ])
        self._theme_combo.setFixedWidth(140)
        self._theme_combo.setStyleSheet("ComboBox { text-align: center; }")
        self._theme_card.hBoxLayout.addWidget(self._theme_combo, 0, Qt.AlignmentFlag.AlignRight)
        self._theme_card.hBoxLayout.addSpacing(16)
        self._personal_group.addSettingCard(self._theme_card)

        # Language card — manual SettingCard with ComboBox
        self._lang_card = SettingCard(
            FluentIcon.LANGUAGE, tr("settings.language"),
            tr("settings.language_desc"),
            self._personal_group,
        )
        self._lang_combo = ComboBox()
        self._lang_combo.setFixedWidth(140)
        self._lang_combo.setStyleSheet("ComboBox { text-align: center; }")
        self._lang_card.hBoxLayout.addWidget(self._lang_combo, 0, Qt.AlignmentFlag.AlignRight)
        self._lang_card.hBoxLayout.addSpacing(16)
        self._personal_group.addSettingCard(self._lang_card)

        self._layout.addWidget(self._personal_group)

        # ── Game group ────────────────────────────────────────────
        self._game_group = SettingCardGroup(tr("settings.game"), self._container)

        self._game_dir_card = PushSettingCard(
            tr("settings.browse"), FluentIcon.FOLDER, tr("settings.game_dir"),
            tr("settings.game_dir_not_configured"),
            self._game_group,
        )
        self._game_dir_card.button.setMinimumWidth(140)
        self._game_group.addSettingCard(self._game_dir_card)

        self._layout.addWidget(self._game_group)

        # ── Profiles group ────────────────────────────────────────
        self._profiles_group = SettingCardGroup(tr("settings.profiles"), self._container)

        self._manage_profiles_card = PushSettingCard(
            tr("settings.manage"), FluentIcon.LIBRARY, tr("settings.manage_profiles"),
            tr("settings.manage_profiles_desc"),
            self._profiles_group,
        )
        self._manage_profiles_card.button.setMinimumWidth(140)
        self._profiles_group.addSettingCard(self._manage_profiles_card)

        self._export_list_card = PushSettingCard(
            tr("settings.export"), FluentIcon.SHARE, tr("settings.export_list"),
            tr("settings.export_list_desc"),
            self._profiles_group,
        )
        self._export_list_card.button.setMinimumWidth(140)
        self._profiles_group.addSettingCard(self._export_list_card)

        self._import_list_card = PushSettingCard(
            tr("settings.import"), FluentIcon.DOWNLOAD, tr("settings.import_list"),
            tr("settings.import_list_desc"),
            self._profiles_group,
        )
        self._import_list_card.button.setMinimumWidth(140)
        self._profiles_group.addSettingCard(self._import_list_card)

        self._layout.addWidget(self._profiles_group)

        # ── NexusMods API (testing only — personal key) ──────────
        self._nexus_group = SettingCardGroup(tr("settings.nexus_title"), self._container)

        # API key input card
        nexus_card = QWidget()
        nexus_layout = QVBoxLayout(nexus_card)
        nexus_layout.setContentsMargins(16, 12, 16, 12)
        nexus_layout.setSpacing(8)

        key_row = QHBoxLayout()
        key_label = CaptionLabel(tr("settings.nexus_key"))
        key_label.setFixedWidth(100)
        key_row.addWidget(key_label)
        self._nexus_key_input = QLineEdit()
        self._nexus_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._nexus_key_input.setPlaceholderText(tr("settings.nexus_key_placeholder"))
        self._nexus_key_input.setFixedHeight(34)
        key_row.addWidget(self._nexus_key_input)
        save_key_btn = PushButton(tr("settings.nexus_save"))
        save_key_btn.setMinimumWidth(80)
        save_key_btn.clicked.connect(self._on_save_nexus_key)
        key_row.addWidget(save_key_btn)
        nexus_layout.addLayout(key_row)

        check_row = QHBoxLayout()
        self._nexus_status = CaptionLabel("")
        check_row.addWidget(self._nexus_status, 1)
        self._nexus_check_btn = PrimaryPushButton(tr("settings.nexus_check"))
        self._nexus_check_btn.setMinimumWidth(180)
        self._nexus_check_btn.clicked.connect(self._on_check_nexus_updates)
        check_row.addWidget(self._nexus_check_btn)
        nexus_layout.addLayout(check_row)

        self._nexus_group.addSettingCard(nexus_card)
        self._layout.addWidget(self._nexus_group)

        # ── Bug Report (PrivateBin) ───────────────────────────────
        self._privatebin_group = SettingCardGroup(tr("settings.privatebin_title"), self._container)

        pb_card = QWidget()
        pb_layout = QVBoxLayout(pb_card)
        pb_layout.setContentsMargins(16, 12, 16, 12)
        pb_layout.setSpacing(8)

        # Instance URL row
        inst_row = QHBoxLayout()
        inst_label = CaptionLabel(tr("settings.privatebin_instance"))
        inst_label.setFixedWidth(100)
        inst_row.addWidget(inst_label)
        self._privatebin_instance_input = QLineEdit()
        self._privatebin_instance_input.setPlaceholderText("https://privatebin.net")
        self._privatebin_instance_input.setFixedHeight(34)
        inst_row.addWidget(self._privatebin_instance_input)
        pb_layout.addLayout(inst_row)

        # Expiration row
        exp_row = QHBoxLayout()
        exp_label = CaptionLabel(tr("settings.privatebin_expire"))
        exp_label.setFixedWidth(100)
        exp_row.addWidget(exp_label)
        self._privatebin_expire_combo = ComboBox()
        self._privatebin_expire_combo.addItems([
            "10 minutes", "1 hour", "1 day", "1 week", "1 month", "1 year",
        ])
        self._privatebin_expire_combo.setFixedWidth(180)
        self._privatebin_expire_combo.setStyleSheet("ComboBox { text-align: center; }")
        self._privatebin_expire_codes = ["10min", "1hour", "1day", "1week", "1month", "1year"]
        exp_row.addWidget(self._privatebin_expire_combo)
        exp_row.addStretch()
        pb_save = PushButton(tr("settings.nexus_save"))
        pb_save.setMinimumWidth(80)
        pb_save.clicked.connect(self._on_save_privatebin_settings)
        exp_row.addWidget(pb_save)
        pb_layout.addLayout(exp_row)

        hint = CaptionLabel(tr("settings.privatebin_hint"))
        hint.setWordWrap(True)
        pb_layout.addWidget(hint)

        self._privatebin_group.addSettingCard(pb_card)
        self._layout.addWidget(self._privatebin_group)

        self._layout.addStretch()

        self.setWidget(self._container)
        self.enableTransparentBackground()
        self.setScrollAnimation(Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

        # ── Connect signals ───────────────────────────────────────
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._game_dir_card.clicked.connect(self._on_game_dir_browse)
        self._manage_profiles_card.clicked.connect(self.profile_manage_requested.emit)
        self._export_list_card.clicked.connect(self.export_list_requested.emit)
        self._import_list_card.clicked.connect(self.import_list_requested.emit)

        # Populate language combobox
        self._populate_languages()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_managers(self, **kwargs) -> None:
        """Receive engine references from the parent window."""
        db = kwargs.get("db")
        game_dir = kwargs.get("game_dir")

        if db is not None:
            self._db = db
            from cdumm.storage.config import Config
            self._config = Config(db)

        if game_dir is not None:
            self._game_dir = game_dir

        # Apply stored values to UI (block signals to prevent feedback loops)
        self._sync_ui_from_db()

    def refresh(self) -> None:
        """Re-read settings from DB and update UI."""
        self._sync_ui_from_db()

    def retranslate_ui(self) -> None:
        """Update all visible text with current translations."""
        self._title_label.setText(tr("settings.title"))
        self._personal_group.titleLabel.setText(tr("settings.personalization"))
        self._theme_card.setTitle(tr("settings.theme"))
        self._theme_card.setContent(tr("settings.theme_desc"))
        # Re-label theme combo items
        self._theme_combo.blockSignals(True)
        idx = self._theme_combo.currentIndex()
        self._theme_combo.clear()
        self._theme_combo.addItems([
            tr("settings.theme_light"), tr("settings.theme_dark"), tr("settings.theme_auto"),
        ])
        if 0 <= idx < self._theme_combo.count():
            self._theme_combo.setCurrentIndex(idx)
        self._theme_combo.blockSignals(False)
        self._lang_card.setTitle(tr("settings.language"))
        self._lang_card.setContent(tr("settings.language_desc"))
        self._game_group.titleLabel.setText(tr("settings.game"))
        self._game_dir_card.setTitle(tr("settings.game_dir"))
        self._game_dir_card.button.setText(tr("settings.browse"))
        self._profiles_group.titleLabel.setText(tr("settings.profiles"))
        self._manage_profiles_card.setTitle(tr("settings.manage_profiles"))
        self._manage_profiles_card.setContent(tr("settings.manage_profiles_desc"))
        self._manage_profiles_card.button.setText(tr("settings.manage"))
        self._export_list_card.setTitle(tr("settings.export_list"))
        self._export_list_card.setContent(tr("settings.export_list_desc"))
        self._export_list_card.button.setText(tr("settings.export"))
        self._import_list_card.setTitle(tr("settings.import_list"))
        self._import_list_card.setContent(tr("settings.import_list_desc"))
        self._import_list_card.button.setText(tr("settings.import"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sync_ui_from_db(self) -> None:
        """Read all settings from SQLite and update UI controls."""
        if not self._config:
            return

        # Theme
        saved_theme = self._config.get("theme") or "light"
        self._theme_combo.blockSignals(True)
        theme_index = {"light": 0, "dark": 1, "auto": 2}.get(saved_theme, 0)
        self._theme_combo.setCurrentIndex(theme_index)
        self._theme_combo.blockSignals(False)

        # Language — match by code list
        saved_lang = self._config.get("language") or "en"
        self._lang_combo.blockSignals(True)
        codes = getattr(self, '_lang_codes', [])
        if saved_lang in codes:
            self._lang_combo.setCurrentIndex(codes.index(saved_lang))
        self._lang_combo.blockSignals(False)

        # Game directory
        game_dir = self._config.get("game_directory") or ""
        if game_dir:
            self._game_dir_card.setContent(game_dir)
            self._game_dir_card.setToolTip(game_dir)
        elif self._game_dir:
            self._game_dir_card.setContent(str(self._game_dir))
            self._game_dir_card.setToolTip(str(self._game_dir))

        # NexusMods API key
        saved_key = self._config.get("nexus_api_key") or ""
        if saved_key and hasattr(self, '_nexus_key_input'):
            self._nexus_key_input.setText(saved_key)

        # PrivateBin
        if hasattr(self, '_privatebin_instance_input'):
            inst = self._config.get("privatebin_instance") or "https://privatebin.net"
            self._privatebin_instance_input.setText(inst)
            code = self._config.get("privatebin_expire") or "1week"
            if code in self._privatebin_expire_codes:
                self._privatebin_expire_combo.blockSignals(True)
                self._privatebin_expire_combo.setCurrentIndex(
                    self._privatebin_expire_codes.index(code))
                self._privatebin_expire_combo.blockSignals(False)

    def _populate_languages(self) -> None:
        """Fill the language combo from available translation files."""
        from cdumm.i18n import available_languages
        self._lang_combo.blockSignals(True)
        self._lang_combo.clear()
        self._lang_codes = []  # qfluentwidgets ComboBox doesn't support itemData
        for code, name in available_languages():
            self._lang_combo.addItem(name)
            self._lang_codes.append(code)
        self._lang_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_theme_changed(self, index: int) -> None:
        """Apply theme change immediately and persist to SQLite."""
        theme = {0: "light", 1: "dark", 2: "auto"}.get(index, "light")

        # Persist
        if self._config:
            self._config.set("theme", theme)

        # Apply via qfluentwidgets
        from qfluentwidgets import setTheme, Theme
        if theme == "auto":
            setTheme(Theme.AUTO)
        elif theme == "dark":
            setTheme(Theme.DARK)
        else:
            setTheme(Theme.LIGHT)

        # Re-apply custom styles wiped by qfluentwidgets updateStyleSheet()
        self._reapply_custom_styles()
        logger.info("Theme changed to %s", theme)

    def _reapply_custom_styles(self) -> None:
        """Re-apply custom widget styles after qfluentwidgets theme update wipes them."""
        self._theme_combo.setStyleSheet("ComboBox { text-align: center; }")
        self._lang_combo.setStyleSheet("ComboBox { text-align: center; }")

    def _on_language_changed(self, index: int) -> None:
        """Persist language choice and apply at runtime."""
        if index < 0 or index >= len(getattr(self, '_lang_codes', [])):
            return
        code = self._lang_codes[index]

        if self._config:
            self._config.set("language", code)

        # Reload translations immediately
        from cdumm.i18n import load as load_i18n, is_rtl
        load_i18n(code)

        # Update layout direction for RTL/LTR languages
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt as _Qt
        if is_rtl():
            QApplication.instance().setLayoutDirection(_Qt.LayoutDirection.RightToLeft)
        else:
            QApplication.instance().setLayoutDirection(_Qt.LayoutDirection.LeftToRight)

        # Update all visible text in the window
        window = self.window()
        if hasattr(window, '_retranslate_ui'):
            window._retranslate_ui()

        InfoBar.success(
            title=tr("settings.language_applied_title"),
            content=tr("settings.language_applied_body"),
            duration=3000, position=InfoBarPosition.TOP, parent=self.window(),
        )
        logger.info("Language changed to %s", code)

    def _on_game_dir_browse(self) -> None:
        """Open a folder browser to change the game directory."""
        current = str(self._game_dir) if self._game_dir else ""
        new_dir = QFileDialog.getExistingDirectory(
            self.window(), "Select Crimson Desert Game Directory", current)
        if not new_dir:
            return

        new_path = Path(new_dir)

        # Validate — check for expected game files
        from cdumm.storage.game_finder import validate_game_directory
        if not validate_game_directory(new_path):
            InfoBar.warning(
                title=tr("settings.invalid_dir_title"),
                content=tr("settings.invalid_dir_body"),
                duration=5000, position=InfoBarPosition.TOP, parent=self.window(),
            )
            return

        # Persist
        if self._config:
            self._config.set("game_directory", str(new_path))

        # Update pointer file
        try:
            pointer_file = Path.home() / "AppData" / "Local" / "cdumm" / "game_dir.txt"
            pointer_file.parent.mkdir(parents=True, exist_ok=True)
            pointer_file.write_text(str(new_path), encoding="utf-8")
        except Exception:
            pass

        self._game_dir = new_path
        self._game_dir_card.setContent(str(new_path))
        self._game_dir_card.setToolTip(str(new_path))

        # Notify parent
        self.game_dir_changed.emit(new_path)

        InfoBar.success(
            title=tr("settings.game_dir_changed_title"),
            content=tr("settings.game_dir_changed_body", path=str(new_path)),
            duration=5000, position=InfoBarPosition.TOP, parent=self.window(),
        )
        logger.info("Game directory changed to %s", new_path)

    def _on_save_nexus_key(self) -> None:
        key = self._nexus_key_input.text().strip()
        if not self._db:
            return
        from cdumm.storage.config import Config
        Config(self._db).set("nexus_api_key", key)
        if key:
            from cdumm.engine.nexus_api import validate_api_key
            user = validate_api_key(key)
            if user:
                name = user.get("name", "Unknown")
                self._nexus_status.setText(tr("settings.nexus_logged_in", name=name))
                self._nexus_status.setStyleSheet("color: #16A34A;")
            else:
                self._nexus_status.setText(tr("settings.nexus_invalid_key"))
                self._nexus_status.setStyleSheet("color: #DC2626;")
        else:
            self._nexus_status.setText(tr("settings.nexus_key_cleared"))

    def _on_save_privatebin_settings(self) -> None:
        if not self._config:
            return
        inst = self._privatebin_instance_input.text().strip() or "https://privatebin.net"
        # Normalise: drop trailing slash is fine either way for httpx, but keep one.
        if not inst.endswith("/"):
            inst = inst + "/"
        idx = self._privatebin_expire_combo.currentIndex()
        code = self._privatebin_expire_codes[idx] if 0 <= idx < len(self._privatebin_expire_codes) else "1week"
        self._config.set("privatebin_instance", inst)
        self._config.set("privatebin_expire", code)
        InfoBar.success(
            title=tr("main.saved"),
            content=tr("settings.privatebin_saved"),
            duration=2500, position=InfoBarPosition.TOP, parent=self.window(),
        )

    def _on_check_nexus_updates(self) -> None:
        if not self._db:
            return
        from cdumm.storage.config import Config
        api_key = Config(self._db).get("nexus_api_key")
        if not api_key:
            self._nexus_status.setText(tr("settings.nexus_need_key"))
            self._nexus_status.setStyleSheet("color: #DC2626;")
            return
        self._nexus_status.setText(tr("settings.nexus_checking"))
        self._nexus_status.setStyleSheet("")
        self._nexus_check_btn.setEnabled(False)
        # Read mods on main thread (SQLite connections can't cross threads)
        try:
            cursor = self._db.connection.execute(
                "SELECT id, name, version, nexus_mod_id FROM mods WHERE mod_type = 'paz'")
            mods = [{"id": r[0], "name": r[1], "version": r[2], "nexus_mod_id": r[3]}
                    for r in cursor.fetchall()]
        except Exception as e:
            self._nexus_status.setText(tr("settings.nexus_db_error", error=str(e)))
            self._nexus_status.setStyleSheet("color: #DC2626;")
            self._nexus_check_btn.setEnabled(True)
            return

        import threading
        def _check():
            try:
                from cdumm.engine.nexus_api import check_mod_updates
                self._pending_updates = check_mod_updates(mods, api_key)
                self._pending_error = None
            except Exception as e:
                self._pending_updates = []
                self._pending_error = str(e)
            from PySide6.QtCore import QMetaObject, Qt as _Qt
            QMetaObject.invokeMethod(
                self, "_show_nexus_results", _Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=_check, daemon=True).start()

    @Slot()
    def _show_nexus_results(self) -> None:
        self._nexus_check_btn.setEnabled(True)
        error = getattr(self, "_pending_error", None)
        if error:
            self._nexus_status.setText(tr("settings.nexus_error", error=error[:80]))
            self._nexus_status.setStyleSheet("color: #DC2626;")
            return
        updates = getattr(self, "_pending_updates", [])
        if not updates:
            self._nexus_status.setText(tr("settings.nexus_all_up_to_date"))
            self._nexus_status.setStyleSheet("color: #16A34A;")
            return
        lines = [f"{u.local_name}: {u.local_version} -> {u.latest_version}" for u in updates]
        self._nexus_status.setText(tr("settings.nexus_updates_available", count=len(updates)))
        self._nexus_status.setStyleSheet("color: #2878D0;")
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox(self.window())
        msg.setWindowTitle(tr("settings.nexus_updates_title", count=len(updates)))
        msg.setText("Newer versions on NexusMods:\n\n" + "\n".join(lines))
        msg.exec()
