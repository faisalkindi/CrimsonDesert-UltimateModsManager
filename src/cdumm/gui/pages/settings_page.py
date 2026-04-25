"""Settings page for CDUMM v3 — SQLite-backed setting cards."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    GroupHeaderCardWidget,
    HyperlinkButton,
    IconInfoBadge,
    InfoBadge,
    InfoBar,
    InfoBarPosition,
    InfoLevel,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    PushSettingCard,
    SettingCard,
    SettingCardGroup,
    SmoothScrollArea,
    SubtitleLabel,
    SwitchButton,
)

from cdumm.i18n import tr

logger = logging.getLogger(__name__)


def _classify_settings_error(error: str | None) -> tuple[str, str]:
    """Split the prefixed error string produced by the check thread
    into ``(kind, detail)``. Bug 46: without this, raw
    ``"auth:Nexus rejected the key"`` leaked into the user-facing
    message.

    Recognised kinds: ``"auth"``, ``"rate_limited"``, ``"generic"``
    (unknown prefix — raw detail), ``"none"`` (empty / None input).
    """
    if not error:
        return "none", ""
    for prefix in ("auth", "rate_limited"):
        token = prefix + ":"
        if error.startswith(token):
            return prefix, error[len(token):]
    return "generic", error


def _persist_nexus_key_if_valid(key: str, cfg) -> dict | None:
    """Validate a Nexus API key and persist it ONLY on success.

    Bug #15 fix: the previous flow called ``cfg.set("nexus_api_key",
    key)`` BEFORE validation, so a bad key stayed in the DB forever
    and the next app launch silently used it.

    ``key == ""`` is treated as "clear the saved key" and skips
    validation entirely.

    Returns the user dict from ``validate_api_key`` on success,
    ``None`` otherwise. ``cfg`` is any object with a ``.set(key,
    value)`` method (a ``Config`` in production, a fake in tests).
    """
    if not key:
        cfg.set("nexus_api_key", "")
        return None
    from cdumm.engine.nexus_api import validate_api_key
    user = validate_api_key(key)
    if user:
        cfg.set("nexus_api_key", key)
    return user


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

        # ASI loader auto-install toggle — lets users disable the default
        # behaviour where CDUMM ships its own winmm.dll proxy and keeps
        # it current on every ASI page refresh. Users running OptiScaler
        # (or another tool) with its own winmm loader turn this OFF so
        # CDUMM stops stomping on it. Reported by UNIVERSE69 on Nexus.
        self._asi_loader_card = SettingCard(
            FluentIcon.DEVELOPER_TOOLS,
            tr("settings.asi_auto_install_loader"),
            tr("settings.asi_auto_install_loader_desc"),
            self._game_group,
        )
        self._asi_loader_switch = SwitchButton()
        self._asi_loader_switch.setOnText("")
        self._asi_loader_switch.setOffText("")
        self._asi_loader_card.hBoxLayout.addWidget(
            self._asi_loader_switch, 0, Qt.AlignmentFlag.AlignRight)
        self._asi_loader_card.hBoxLayout.addSpacing(16)
        self._game_group.addSettingCard(self._asi_loader_card)

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

        # ── NexusMods API ────────────────────────────────────────
        self._nexus_card = GroupHeaderCardWidget(self._container)
        self._nexus_card.setTitle(tr("settings.nexus_title"))
        self._nexus_card.setBorderRadius(8)

        # Row 1 — primary check-for-updates action. SSO is the
        # recommended way to authenticate and lives below the card;
        # manual API key paste is now Advanced-only (collapsed by
        # default) to avoid the "why both?" UX confusion.
        self._nexus_check_btn = PrimaryPushButton(tr("settings.nexus_check"))
        self._nexus_check_btn.setIcon(FluentIcon.SYNC)
        self._nexus_check_btn.setMinimumWidth(200)
        self._nexus_check_btn.clicked.connect(self._on_check_nexus_updates)
        self._nexus_card.addGroup(
            FluentIcon.SYNC,
            tr("settings.nexus_check_row_title"),
            tr("settings.nexus_check_row_desc"),
            self._nexus_check_btn,
        )

        # Row 2 — Advanced: manual API key paste (collapsed by default)
        # ``key_widget`` holds the actual input + Save button. It's
        # added to the card always, but hidden until the user clicks
        # the "Advanced" toggle below. If a saved key already exists,
        # _sync_ui_from_db reveals the row automatically so the user
        # can edit/clear it.
        key_widget = QWidget()
        key_row = QHBoxLayout(key_widget)
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(8)
        self._nexus_key_input = PasswordLineEdit()
        self._nexus_key_input.setPlaceholderText(tr("settings.nexus_key_placeholder"))
        self._nexus_key_input.setMinimumWidth(280)
        self._nexus_key_input.setClearButtonEnabled(True)
        key_row.addWidget(self._nexus_key_input, 1)
        save_key_btn = PushButton(tr("settings.nexus_save"))
        save_key_btn.setMinimumWidth(84)
        save_key_btn.clicked.connect(self._on_save_nexus_key)
        key_row.addWidget(save_key_btn)
        self._nexus_advanced_group = self._nexus_card.addGroup(
            FluentIcon.VPN,
            tr("settings.nexus_key_row_title"),
            tr("settings.nexus_key_row_desc"),
            key_widget,
        )
        # Hide the whole group row by default. _sync_ui_from_db keeps
        # it hidden even when a key is saved — the user reveals it via
        # the Advanced toggle below if they want to edit/paste a key
        # manually. Showing both Login + a populated key field at the
        # same time was the "why is the toggle below the visible field"
        # confusion the user reported.
        if self._nexus_advanced_group is not None:
            self._nexus_advanced_group.setVisible(False)

        self._layout.addWidget(self._nexus_card)

        # ── Status strip (badge + text) — handler + signed-in status ─
        status_row = QWidget()
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(6, 0, 6, 0)
        status_layout.setSpacing(8)
        self._nexus_status_badge = IconInfoBadge.info(FluentIcon.INFO, self._container)
        self._nexus_status_badge.setFixedSize(18, 18)
        self._nexus_status_badge.hide()
        status_layout.addWidget(self._nexus_status_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self._nexus_status = BodyLabel("")
        status_layout.addWidget(self._nexus_status, 1, Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(status_row)

        # ── Account row (Login / Sign out — state-aware) ───────────
        # One button that toggles between 'Login with Nexus' (when no
        # key is saved) and 'Sign out' (when a key is saved). The
        # description text on the left mirrors the state so the user
        # never sees contradictory copy.
        sso_row = QWidget()
        sso_layout = QHBoxLayout(sso_row)
        sso_layout.setContentsMargins(6, 4, 6, 4)
        sso_layout.setSpacing(8)
        self._sso_label = BodyLabel("")
        self._sso_label.setWordWrap(True)
        sso_layout.addWidget(self._sso_label, 1)
        self._sso_login_btn = PushButton("Login with Nexus")
        self._sso_login_btn.setMinimumWidth(160)
        self._sso_login_btn.clicked.connect(self._on_sso_button_clicked)
        sso_layout.addWidget(self._sso_login_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(sso_row)
        # Populate label/button state for the current key.
        self._refresh_sso_button()

        nxm_row = QWidget()
        nxm_layout = QHBoxLayout(nxm_row)
        nxm_layout.setContentsMargins(6, 4, 6, 4)
        nxm_layout.setSpacing(8)
        nxm_label = BodyLabel(
            "Handle nxm:// links — when you click 'Mod Manager "
            "Download' on a Nexus mod page, the file is sent to CDUMM "
            "automatically (premium users download directly; free "
            "users still route through the website's button).")
        nxm_label.setWordWrap(True)
        nxm_layout.addWidget(nxm_label, 1)
        self._nxm_toggle_btn = PushButton("Register")
        self._nxm_toggle_btn.setMinimumWidth(160)
        self._nxm_toggle_btn.clicked.connect(self._on_nxm_toggle)
        nxm_layout.addWidget(self._nxm_toggle_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(nxm_row)
        # Populate the initial state of the button
        self._refresh_nxm_button()

        # ── Advanced toggle row (last) ─────────────────────────────
        # Hyperlink-style toggle that reveals/hides the manual API
        # Key paste row inside the card above. Label is dynamic so
        # 'paste' never reads as 'hide'. The 'Get your API key' link
        # also lives here, next to the toggle, so the manual-paste
        # path is self-contained.
        advanced_row = QWidget()
        advanced_layout = QHBoxLayout(advanced_row)
        advanced_layout.setContentsMargins(6, 0, 6, 0)
        advanced_layout.setSpacing(6)
        self._nexus_advanced_toggle = HyperlinkButton(
            "", "Show manual API key field")
        self._nexus_advanced_toggle.setIcon(FluentIcon.SETTING)

        def _toggle_advanced_key_row() -> None:
            if self._nexus_advanced_group is None:
                return
            new_visible = not self._nexus_advanced_group.isVisible()
            self._nexus_advanced_group.setVisible(new_visible)
            self._nexus_advanced_toggle.setText(
                "Hide manual API key field" if new_visible
                else "Show manual API key field")

        self._nexus_advanced_toggle.clicked.connect(_toggle_advanced_key_row)
        advanced_layout.addWidget(self._nexus_advanced_toggle)
        self._nexus_get_key_link = HyperlinkButton(
            "https://next.nexusmods.com/settings/api-keys",
            tr("settings.nexus_get_key"))
        self._nexus_get_key_link.setIcon(FluentIcon.LINK)
        advanced_layout.addWidget(self._nexus_get_key_link)
        advanced_layout.addStretch(1)
        self._layout.addWidget(advanced_row)

        # ── Bug Report (PrivateBin) ───────────────────────────────
        self._privatebin_card = GroupHeaderCardWidget(self._container)
        self._privatebin_card.setTitle(tr("settings.privatebin_title"))
        self._privatebin_card.setBorderRadius(8)

        # Row 1 — instance URL
        self._privatebin_instance_input = LineEdit()
        self._privatebin_instance_input.setPlaceholderText("https://privatebin.net")
        self._privatebin_instance_input.setMinimumWidth(320)
        self._privatebin_instance_input.setClearButtonEnabled(True)
        self._privatebin_card.addGroup(
            FluentIcon.LINK,
            tr("settings.privatebin_instance_row_title"),
            tr("settings.privatebin_instance_row_desc"),
            self._privatebin_instance_input,
        )

        # Row 2 — expiration combo + save
        exp_widget = QWidget()
        exp_row = QHBoxLayout(exp_widget)
        exp_row.setContentsMargins(0, 0, 0, 0)
        exp_row.setSpacing(8)
        self._privatebin_expire_combo = ComboBox()
        self._privatebin_expire_combo.addItems([
            "10 minutes", "1 hour", "1 day", "1 week", "1 month", "1 year",
        ])
        self._privatebin_expire_combo.setFixedWidth(160)
        self._privatebin_expire_codes = ["10min", "1hour", "1day", "1week", "1month", "1year"]
        exp_row.addWidget(self._privatebin_expire_combo)
        pb_save = PushButton(tr("settings.nexus_save"))
        pb_save.setMinimumWidth(84)
        pb_save.clicked.connect(self._on_save_privatebin_settings)
        exp_row.addWidget(pb_save)
        self._privatebin_card.addGroup(
            FluentIcon.HISTORY,
            tr("settings.privatebin_expire_row_title"),
            tr("settings.privatebin_expire_row_desc"),
            exp_widget,
        )

        self._layout.addWidget(self._privatebin_card)

        # Footer hint — caption tone, word-wrapped, sits under the card
        hint = CaptionLabel(tr("settings.privatebin_hint"))
        hint.setWordWrap(True)
        hint.setContentsMargins(6, 0, 6, 0)
        self._layout.addWidget(hint)

        self._layout.addStretch()

        self.setWidget(self._container)
        self.enableTransparentBackground()
        self.setScrollAnimation(Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

        # ── Connect signals ───────────────────────────────────────
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._game_dir_card.clicked.connect(self._on_game_dir_browse)
        self._asi_loader_switch.checkedChanged.connect(
            self._on_asi_loader_toggle_changed)
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
        self._asi_loader_card.setTitle(tr("settings.asi_auto_install_loader"))
        self._asi_loader_card.setContent(
            tr("settings.asi_auto_install_loader_desc"))
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

        # ASI loader auto-install — unset defaults to "true" to preserve
        # existing behaviour for users who never touch the toggle.
        auto_install = self._config.get("asi_auto_install_loader")
        checked = auto_install != "false"
        self._asi_loader_switch.blockSignals(True)
        self._asi_loader_switch.setChecked(checked)
        self._asi_loader_switch.blockSignals(False)

        # NexusMods API key. Pre-fill the Advanced field so it's there
        # if/when the user clicks the toggle, but keep the row hidden
        # by default — the primary path is "Login with Nexus", and
        # showing both at once is the UX clutter the user reported.
        saved_key = self._config.get("nexus_api_key") or ""
        if saved_key and hasattr(self, '_nexus_key_input'):
            self._nexus_key_input.setText(saved_key)
        # Sync the Login/Sign-out button label to the saved-key state.
        if hasattr(self, "_refresh_sso_button"):
            self._refresh_sso_button()

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

    def _on_asi_loader_toggle_changed(self, checked: bool) -> None:
        """Persist the ASI-loader auto-install preference."""
        if self._config is None:
            return
        self._config.set(
            "asi_auto_install_loader", "true" if checked else "false")
        logger.info(
            "ASI loader auto-install %s", "enabled" if checked else "disabled")

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

    def _refresh_nxm_button(self) -> None:
        """Sync nxm:// register/unregister button label with current state."""
        from cdumm.engine.nxm_handler import is_handler_registered
        if is_handler_registered():
            self._nxm_toggle_btn.setText("Unregister")
        else:
            self._nxm_toggle_btn.setText("Register")

    def _on_nxm_toggle(self) -> None:
        """Register/unregister CDUMM as the nxm:// protocol handler.

        Safety: when another mod manager (Vortex/MO2) already owns the
        scheme, we refuse to overwrite silently. The user gets a
        confirmation dialog that shows the existing command string so
        they can decide whether to take over. Codex P1 — previous code
        auto-retried with ``force=True`` in the same click, defeating
        the explicit-opt-in contract documented in ``nxm_handler.py``.
        """
        from cdumm.engine.nxm_handler import (
            is_handler_registered, register_windows_handler,
            unregister_windows_handler, _read_command_string,
        )
        if is_handler_registered():
            ok = unregister_windows_handler()
            msg = ("nxm:// handler unregistered." if ok
                   else "Could not fully unregister nxm:// handler — check the log.")
            self._set_nexus_status(msg, InfoLevel.SUCCESS if ok else InfoLevel.ERROR)
            self._refresh_nxm_button()
            return

        # Try non-destructive register first.
        ok = register_windows_handler(force=False)
        if ok:
            self._set_nexus_status(
                "CDUMM is now the nxm:// handler for your user account.",
                InfoLevel.SUCCESS)
            self._refresh_nxm_button()
            return

        # Another mod manager owns the scheme — ask before stomping.
        try:
            import winreg
            existing_cmd = _read_command_string(winreg) or "(unknown)"
        except Exception:
            existing_cmd = "(unknown)"
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Another nxm:// handler is registered")
        box.setText(
            "Another application is currently registered as the "
            "nxm:// handler — usually Vortex or Mod Organizer 2.")
        box.setInformativeText(
            f"Current handler:\n{existing_cmd}\n\n"
            "If you replace it, 'Mod Manager Download' clicks on "
            "Nexus will route to CDUMM instead. You can switch back "
            "by running the other manager's setup again.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        choice = box.exec_() if hasattr(box, "exec_") else box.exec()
        if choice != QMessageBox.StandardButton.Yes:
            self._set_nexus_status(
                "nxm:// registration cancelled — existing handler kept.",
                InfoLevel.INFOAMTION)
            self._refresh_nxm_button()
            return

        ok = register_windows_handler(force=True)
        msg = ("CDUMM is now the nxm:// handler for your user "
               "account." if ok else "Registration failed — CDUMM "
               "must be running as the packaged .exe, not the dev "
               "source, to register as a handler.")
        self._set_nexus_status(msg, InfoLevel.SUCCESS if ok else InfoLevel.ERROR)
        self._refresh_nxm_button()

    def _refresh_sso_button(self) -> None:
        """Sync the SSO button + label to the current saved-key state.

        - No key  → label = recommended-flow copy, button = "Login with Nexus".
        - Has key → label = "Signed in. Click Sign out to revoke.",
                    button = "Sign out".
        - Slug not yet approved (rare) → button disabled, tooltip explains.
        """
        if not self._db:
            return
        from cdumm.engine.nexus_sso import slug_placeholder
        from cdumm.storage.config import Config
        sso_ready = not slug_placeholder()
        saved_key = (Config(self._db).get("nexus_api_key") or "").strip()
        if saved_key:
            self._sso_label.setText(
                "Signed in to Nexus. Click 'Sign out' to clear the "
                "saved API key.")
            self._sso_login_btn.setText("Sign out")
            self._sso_login_btn.setEnabled(True)
            self._sso_login_btn.setToolTip(
                "Removes the saved API key. You'll need to sign in "
                "again to check for mod updates.")
        else:
            self._sso_label.setText(
                "Login with Nexus — opens your browser to sign in, "
                "no manual key paste. CDUMM's application slug is "
                "approved by Nexus; this is the recommended flow.")
            self._sso_login_btn.setText("Login with Nexus")
            self._sso_login_btn.setEnabled(sso_ready)
            if sso_ready:
                self._sso_login_btn.setToolTip(
                    "Opens your browser to sign in with your Nexus "
                    "Mods account. CDUMM never sees your password.")
            else:
                self._sso_login_btn.setToolTip(
                    "Pending Nexus approval of CDUMM's application "
                    "slug. Until approved, paste your personal API "
                    "key via the Advanced toggle below.")

    def _on_sso_button_clicked(self) -> None:
        """Dispatch the dual-purpose SSO/Sign-out button."""
        if not self._db:
            return
        from cdumm.storage.config import Config
        saved_key = (Config(self._db).get("nexus_api_key") or "").strip()
        if saved_key:
            self._on_sso_logout()
        else:
            self._on_sso_login()

    def _on_sso_logout(self) -> None:
        """Clear the saved API key, refresh the UI, and drop cached
        update results so pills go neutral immediately.

        The nxm:// handler registration is a system-level setting and
        is NOT touched here — signing out doesn't unregister the
        protocol handler.
        """
        if not self._db:
            return
        from cdumm.storage.config import Config
        cfg = Config(self._db)
        cfg.set("nexus_api_key", "")
        if hasattr(self, "_nexus_key_input"):
            self._nexus_key_input.setText("")
        # Drop the rejected-auth banner if it was up — the user
        # explicitly chose to sign out, not a transient auth error.
        try:
            from cdumm.gui.fluent_window import _clear_auth_banner_state
            _clear_auth_banner_state(self.window())
        except Exception as _e:
            logger.debug("auth banner clear (logout) failed: %s", _e)
        # Drop cached Nexus update results so pills don't lie about
        # state we can no longer verify.
        try:
            win = self.window()
            if hasattr(win, "_nexus_updates"):
                win._nexus_updates = {}
            if hasattr(win, "paz_mods_page"):
                win.paz_mods_page.set_nexus_updates({})
            if hasattr(win, "asi_plugins_page"):
                try:
                    win.asi_plugins_page.set_nexus_updates({})
                except AttributeError:
                    pass
        except Exception as _e:
            logger.debug("post-logout pill clear failed: %s", _e)
        self._set_nexus_status(
            "Signed out. Sign in again to check for mod updates.",
            InfoLevel.INFOAMTION)
        self._refresh_sso_button()

    def _on_sso_login(self) -> None:
        """Run the SSO WebSocket flow. Disabled while awaiting slug approval."""
        from PySide6.QtCore import QMetaObject, Qt as _Qt
        from cdumm.engine.nexus_sso import start_sso_flow
        self._sso_login_btn.setEnabled(False)
        self._set_nexus_status(
            "Opening browser for Nexus login…", InfoLevel.INFOAMTION)

        def _on_key(api_key: str) -> None:
            # Called from the websocket thread — marshal to GUI thread
            # by stashing the result and invoking a slot.
            self._pending_sso_key = api_key
            QMetaObject.invokeMethod(
                self, "_sso_finished_ok",
                _Qt.ConnectionType.QueuedConnection)

        def _on_error(msg: str) -> None:
            self._pending_sso_err = msg
            QMetaObject.invokeMethod(
                self, "_sso_finished_err",
                _Qt.ConnectionType.QueuedConnection)

        start_sso_flow(_on_key, _on_error)

    @Slot()
    def _sso_finished_ok(self) -> None:
        from cdumm.storage.config import Config
        key = getattr(self, "_pending_sso_key", "") or ""
        if not key:
            self._sso_finished_err()
            return
        Config(self._db).set("nexus_api_key", key)
        self._nexus_key_input.setText(key)
        # Bug 52: SSO-returned key is by definition valid; drop any
        # stale rejected-auth banner so the user doesn't see a red
        # warning hovering over a successful login.
        try:
            from cdumm.gui.fluent_window import _clear_auth_banner_state
            _clear_auth_banner_state(self.window())
        except Exception as _e:
            logger.debug("auth banner clear (SSO) failed: %s", _e)
        self._set_nexus_status(
            "Signed in via Nexus SSO. API key saved. Checking for "
            "mod updates...", InfoLevel.SUCCESS)
        # Flip the dual-purpose button to "Sign out" mode now that
        # a key is saved.
        self._refresh_sso_button()
        # User-requested: kick off a Check for Mod Updates automatically
        # 2 seconds after sign-in so users don't have to find and click
        # the button. Goes through the same handler the button uses.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, self._on_check_nexus_updates)

    @Slot()
    def _sso_finished_err(self) -> None:
        msg = getattr(self, "_pending_sso_err", "Unknown SSO error")
        self._set_nexus_status(f"SSO failed: {msg}", InfoLevel.ERROR)
        # Re-enable so the user can retry. Only disabled when the slug
        # itself is not yet approved.
        from cdumm.engine.nexus_sso import slug_placeholder
        self._sso_login_btn.setEnabled(not slug_placeholder())

    def _on_save_nexus_key(self) -> None:
        key = self._nexus_key_input.text().strip()
        if not self._db:
            return
        from cdumm.storage.config import Config
        cfg = Config(self._db)
        user = _persist_nexus_key_if_valid(key, cfg)
        # Saving a (cleared OR new) key always changes the saved-key
        # state, so resync the dual-purpose Login / Sign-out button.
        self._refresh_sso_button()
        if not key:
            # Bug 51: clearing the key should also dismiss the
            # rejected-auth banner — the user is starting over.
            try:
                from cdumm.gui.fluent_window import _clear_auth_banner_state
                _clear_auth_banner_state(self.window())
            except Exception as _e:
                logger.debug("auth banner clear failed: %s", _e)
            self._set_nexus_status(
                tr("settings.nexus_key_cleared"), InfoLevel.INFOAMTION)
            return
        if user:
            # Bug #32: proactively dismiss the auth-rejected banner so
            # the user doesn't see it hovering after they just fixed
            # the key. The flag reset also lets a LATER failure re-
            # show the banner instead of being squelched by the
            # already-shown gate.
            try:
                from cdumm.gui.fluent_window import _clear_auth_banner_state
                _clear_auth_banner_state(self.window())
            except Exception as _e:
                logger.debug("auth banner clear failed: %s", _e)
            name = user.get("name", "Unknown")
            self._set_nexus_status(
                tr("settings.nexus_logged_in", name=name), InfoLevel.SUCCESS)
            # Same auto-check as SSO: spare the user the extra click.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2000, self._on_check_nexus_updates)
        else:
            self._set_nexus_status(
                tr("settings.nexus_invalid_key"), InfoLevel.ERROR)

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
            self._set_nexus_status(
                tr("settings.nexus_need_key"), InfoLevel.ERROR)
            return
        self._set_nexus_status(
            tr("settings.nexus_checking"), InfoLevel.INFOAMTION)
        self._nexus_check_btn.setEnabled(False)
        # Read mods on main thread (SQLite connections can't cross threads).
        # Union PAZ mods and ASI plugins — the automatic background check
        # already does this, so the manual button must match it. Without
        # this union, users with linked ASI plugins got false 'all up to
        # date' results (Codex P2 regression).
        try:
            cursor = self._db.connection.execute(
                "SELECT id, name, version, nexus_mod_id, nexus_last_checked_at, "
                "nexus_real_file_id "
                "FROM mods WHERE mod_type = 'paz'")
            mods = [{"id": r[0], "name": r[1], "version": r[2],
                     "nexus_mod_id": r[3],
                     "nexus_last_checked_at": r[4],
                     "nexus_real_file_id": r[5]}
                    for r in cursor.fetchall()]
            # Bug 43: match the auto-check — read nexus_real_file_id
            # + nexus_last_checked_at so ASI plugins get chain-walk
            # + feed-skip just like PAZ mods.
            cursor = self._db.connection.execute(
                "SELECT name, version, nexus_mod_id, "
                "nexus_real_file_id, nexus_last_checked_at "
                "FROM asi_plugin_state")
            asi_mods = [{"id": None, "name": r[0], "version": r[1],
                         "nexus_mod_id": r[2],
                         "nexus_real_file_id": r[3] or 0,
                         "nexus_last_checked_at": r[4] or 0}
                        for r in cursor.fetchall()]
        except Exception as e:
            self._set_nexus_status(
                tr("settings.nexus_db_error", error=str(e)), InfoLevel.ERROR)
            self._nexus_check_btn.setEnabled(True)
            return

        combined = mods + asi_mods
        _db = self._db

        import threading
        def _check():
            try:
                from cdumm.engine.nexus_api import (
                    check_mod_updates, NexusAuthError, NexusRateLimited,
                )
                # Unpack 4-tuple: the 4th element (backfill map) is
                # owned by the main window's _apply_nexus_update_colors
                # slot. Discarding here is intentional — a manual
                # "Check for Mod Updates" click in Settings runs this
                # same pipeline, and the backfill is handled on the
                # separate auto-check path that the main window drives.
                updates, checked_ids, now_ts, _backfill = check_mod_updates(
                    combined, api_key)
                self._pending_updates = updates
                self._pending_checked_ids = checked_ids
                self._pending_checked_ts = now_ts
                self._pending_error = None
            except NexusAuthError as e:
                # Bug 37: surface the same "API key rejected" message
                # the auto-check banner uses, not a generic toast.
                self._pending_updates = []
                self._pending_checked_ids = []
                self._pending_checked_ts = 0
                self._pending_error = f"auth:{e}"
            except NexusRateLimited as e:
                # Bug 37: distinct message for rate-limit so the user
                # knows to wait for the hourly reset instead of
                # re-entering their key.
                self._pending_updates = []
                self._pending_checked_ids = []
                self._pending_checked_ts = 0
                self._pending_error = (
                    f"rate_limited:{getattr(e, 'reset_at', 0)}")
            except Exception as e:
                self._pending_updates = []
                self._pending_checked_ids = []
                self._pending_checked_ts = 0
                self._pending_error = str(e)
            from PySide6.QtCore import QMetaObject, Qt as _Qt
            QMetaObject.invokeMethod(
                self, "_show_nexus_results", _Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=_check, daemon=True).start()

    @Slot()
    def _show_nexus_results(self) -> None:
        self._nexus_check_btn.setEnabled(True)
        # Persist last-checked timestamps on the GUI thread — worker can't
        # because SQLite connections are thread-bound. Only rows that had
        # a successful file-list fetch are in checked_ids.
        checked_ids = getattr(self, "_pending_checked_ids", [])
        now_ts = getattr(self, "_pending_checked_ts", 0)
        if self._db and checked_ids and now_ts:
            try:
                placeholders = ",".join("?" * len(checked_ids))
                self._db.connection.execute(
                    f"UPDATE mods SET nexus_last_checked_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [now_ts, *checked_ids])
                self._db.connection.commit()
            except Exception as e:
                logger.debug("nexus_last_checked_at persist failed: %s", e)
        error = getattr(self, "_pending_error", None)
        kind, detail = _classify_settings_error(error)
        if kind == "auth":
            # Same wording as the auto-check banner so users hear
            # one consistent message for the same failure.
            self._set_nexus_status(
                tr("settings.nexus_auth_rejected_title"),
                InfoLevel.ERROR)
            return
        if kind == "rate_limited":
            self._set_nexus_status(
                "Nexus rate limit reached — try again after the "
                "hourly window resets.", InfoLevel.WARNING)
            return
        if kind == "generic":
            self._set_nexus_status(
                tr("settings.nexus_error", error=detail[:80]),
                InfoLevel.ERROR)
            return
        from cdumm.engine.nexus_api import filter_outdated
        # ``_pending_updates`` includes confirmed-current entries too
        # (three-state pill support). The dialog only wants truly
        # outdated mods — filter them out or we claim every matched
        # mod has a newer version on Nexus, with local == latest.
        outdated = filter_outdated(getattr(self, "_pending_updates", []))
        if not outdated:
            self._set_nexus_status(
                tr("settings.nexus_all_up_to_date"), InfoLevel.SUCCESS)
            return
        lines = [f"{u.local_name}: {u.local_version} -> {u.latest_version}" for u in outdated]
        self._set_nexus_status(
            tr("settings.nexus_updates_available", count=len(outdated)),
            InfoLevel.ATTENTION)
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox(self.window())
        msg.setWindowTitle(tr("settings.nexus_updates_title", count=len(outdated)))
        msg.setText("Newer versions on NexusMods:\n\n" + "\n".join(lines))
        # Qt 6 deprecated exec_ in favour of exec; both still work on the
        # PySide6 versions we ship. Must stay MODAL — msg.show() (the old
        # fallback) was modeless, which Codex flagged as P2.
        msg.exec_() if hasattr(msg, "exec_") else msg.exec()

    # ------------------------------------------------------------------
    # Status helper — theme-aware badge swap
    # ------------------------------------------------------------------

    def _set_nexus_status(self, text: str, level: "InfoLevel") -> None:
        """Update the inline status row with a theme-aware badge + message.

        Replaces the previous hex-coded ``setStyleSheet`` approach — those
        colours survived in light mode but crushed to unreadable in dark.
        ``IconInfoBadge`` picks its palette from the current Fluent theme.
        """
        if not text:
            self._nexus_status.setText("")
            self._nexus_status_badge.hide()
            return

        icon_for_level = {
            InfoLevel.SUCCESS: FluentIcon.ACCEPT,
            InfoLevel.ERROR: FluentIcon.CLOSE,
            InfoLevel.WARNING: FluentIcon.INFO,
            InfoLevel.ATTENTION: FluentIcon.SYNC,
            InfoLevel.INFOAMTION: FluentIcon.INFO,
        }.get(level, FluentIcon.INFO)

        old = self._nexus_status_badge
        parent = old.parentWidget()
        layout = parent.layout() if parent else None
        index = layout.indexOf(old) if layout else -1
        old.hide()
        old.deleteLater()
        new_badge = IconInfoBadge.make(icon_for_level, parent, level=level)
        new_badge.setFixedSize(18, 18)
        if layout is not None and index >= 0:
            layout.insertWidget(index, new_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self._nexus_status_badge = new_badge
        self._nexus_status.setText(text)
