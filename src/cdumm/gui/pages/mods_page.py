"""ModsPage -- main PAZ Mods page for CDUMM v3.

Card-based mod list with summary bar, conflict cards, config panel,
drag-drop import overlay, and search/filter controls.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CheckBox,
    FluentIcon,
    PushButton,
    SearchLineEdit,
    SmoothScrollArea,
)

from cdumm.gui.components.config_panel import ConfigPanel
from cdumm.gui.components.conflict_card import ConflictCard
from cdumm.gui.components.mod_card import FolderGroup, ModCard
from cdumm.gui.components.summary_bar import SummaryBar
from cdumm.i18n import tr

logger = logging.getLogger(__name__)

def _dbg(msg: str) -> None:
    pass

def _dbg_status(action: str, mod_id: int, old_status: str, new_status: str,
                reason: str = "") -> None:
    changed = "CHANGED" if old_status != new_status else "NO-CHANGE"
    _dbg(f"STATUS {changed}: [{action}] id={mod_id} '{old_status}' → '{new_status}'"
         f"{' reason=' + reason if reason else ''}")


# ======================================================================
# ModsPage
# ======================================================================


class ModsPage(QWidget):
    """Main PAZ Mods page -- card list, summary bar, config panel, drop overlay.

    The page does NOT inherit ScrollArea; it contains one internally
    so that the SummaryBar stays pinned at the top.

    Signals
    -------
    file_dropped(Path)
        Emitted when a file is drag-dropped onto the page.
    """

    file_dropped = Signal(Path)
    uninstall_requested = Signal(int)  # mod_id — triggers disable+apply+remove in window

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ModsPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        # Engine references (set later via set_managers)
        self._mod_manager = None
        self._conflict_detector = None
        self._db = None
        self._game_dir: Path | None = None

        # Card tracking
        self._mod_cards: list[ModCard] = []
        self._conflict_cards: list[ConflictCard] = []
        self._folder_groups: dict[int | None, FolderGroup] = {}  # group_id -> FolderGroup
        self._last_clicked_index: int | None = None  # for Shift+Click range select
        self._initial_load_done = False  # staggered animation only on first load
        self._applied_state: dict[int, bool] = {}  # mod_id -> was enabled at last Apply

        self._build_ui()

        # Ctrl+A shortcut — select all visible cards (visual selection, not enable/disable)
        QShortcut(QKeySequence.StandardKey.SelectAll, self, self._on_ctrl_a)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ---- SummaryBar (pinned top) ---------------------------------
        self._summary_bar = SummaryBar(self)
        main.addWidget(self._summary_bar)

        # ---- Body (left content + right config panel) ----------------
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # -- Left column (scrollable) --
        left = QVBoxLayout()
        left.setContentsMargins(16, 12, 16, 12)
        left.setSpacing(10)

        # Section header row: title + search
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        from qfluentwidgets import SubtitleLabel
        self._section_title = SubtitleLabel(tr("mod_list.section_title"))
        # Clarify the load-order convention on hover — the #1 vs "bigger
        # number" direction is a frequent source of user confusion. The
        # mod at the top of the list wins conflicts against anything below.
        self._section_title.setToolTip(tr("mod_list.title_tooltip"))
        header_row.addWidget(self._section_title)
        header_row.addStretch()

        self._search_edit = SearchLineEdit(self)
        self._search_edit.setPlaceholderText(tr("mod_list.search_placeholder"))
        self._search_edit.setFixedWidth(220)
        self._search_edit.textChanged.connect(self._on_search)
        header_row.addWidget(self._search_edit)
        left.addLayout(header_row)

        # Select-all row + New Folder button
        select_row = QHBoxLayout()
        select_row.setContentsMargins(14, 0, 0, 0)
        self._select_all_cb = CheckBox(tr("mod_list.select_all"))
        self._select_all_cb.setTristate(False)
        self._select_all_cb.clicked.connect(self._on_select_all)
        select_row.addWidget(self._select_all_cb)
        select_row.addStretch()

        from qfluentwidgets import setCustomStyleSheet
        from PySide6.QtGui import QFont as _QFont
        self._new_folder_btn = PushButton(FluentIcon.ADD, tr("mod_list.new_folder"))
        self._new_folder_btn.setFixedHeight(32)
        _nbf = self._new_folder_btn.font()
        _nbf.setPixelSize(13)
        _nbf.setWeight(_QFont.Weight.Bold)
        self._new_folder_btn.setFont(_nbf)
        self._new_folder_btn.clicked.connect(self._on_new_folder)
        setCustomStyleSheet(self._new_folder_btn,
            "PushButton { background: #F0F4FF; color: #2878D0; border: 1px solid #B8D4F0; border-radius: 16px; padding: 0 14px; padding-bottom: 6px; }"
            "PushButton:hover { background: #E0ECFF; }"
            "PushButton:pressed { background: #D0E0F8; }",
            "PushButton { background: #1A2840; color: #5CB8F0; border: 1px solid #2A4060; border-radius: 16px; padding: 0 14px; padding-bottom: 6px; }"
            "PushButton:hover { background: #223450; }"
            "PushButton:pressed { background: #2A3C58; }")
        select_row.addWidget(self._new_folder_btn)

        # Dedicated conflict-order view (Miki990 UX request). Opens a
        # modal dialog listing every currently-active conflict alongside
        # the mods' load-order priority so the user can see who wins
        # before deciding to reorder.
        self._conflicts_btn = PushButton(
            FluentIcon.ALIGNMENT, tr("mod_list.view_conflicts")
            if tr("mod_list.view_conflicts") != "mod_list.view_conflicts"
            else "View Conflicts")
        self._conflicts_btn.setFixedHeight(32)
        self._conflicts_btn.setFont(_nbf)
        self._conflicts_btn.clicked.connect(self._on_show_conflicts)
        setCustomStyleSheet(self._conflicts_btn,
            "PushButton { background: #FFF4F0; color: #D04848; border: 1px solid #F0B8B0; border-radius: 16px; padding: 0 14px; padding-bottom: 6px; }"
            "PushButton:hover { background: #FFE8E0; }"
            "PushButton:pressed { background: #FFD8CC; }",
            "PushButton { background: #401A1A; color: #F08080; border: 1px solid #602828; border-radius: 16px; padding: 0 14px; padding-bottom: 6px; }"
            "PushButton:hover { background: #502222; }"
            "PushButton:pressed { background: #582828; }")
        select_row.addWidget(self._conflicts_btn)
        left.addLayout(select_row)

        # Scrollable mod list area with smooth scroll animation
        self._scroll = SmoothScrollArea(self)
        self._scroll.setScrollAnimation(Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        self._replace_scrollbar()

        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_layout.setSpacing(4)

        # ── Empty state hero (shown when no mods installed) ──
        from qfluentwidgets import SubtitleLabel, CaptionLabel, CardWidget
        from PySide6.QtGui import QFont

        self._empty_hero = CardWidget(self._scroll_content)
        hero_layout = QVBoxLayout(self._empty_hero)
        hero_layout.setContentsMargins(40, 60, 40, 60)
        hero_layout.setSpacing(16)
        hero_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        hero_icon = SubtitleLabel(tr("mods.drop_title"), self._empty_hero)
        hif = hero_icon.font()
        hif.setPixelSize(48)
        hif.setWeight(QFont.Weight.Bold)
        hero_icon.setFont(hif)
        hero_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        from qfluentwidgets import setCustomStyleSheet
        setCustomStyleSheet(hero_icon,
            "SubtitleLabel { color: #2878D0; }",
            "SubtitleLabel { color: #5CB8F0; }")
        hero_layout.addWidget(hero_icon)

        hero_title = SubtitleLabel(tr("mods.drop_subtitle"), self._empty_hero)
        htf = hero_title.font()
        htf.setPixelSize(22)
        htf.setWeight(QFont.Weight.Bold)
        hero_title.setFont(htf)
        hero_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_layout.addWidget(hero_title)

        hero_hint = CaptionLabel(
            tr("mods.drop_formats"), self._empty_hero)
        hhf = hero_hint.font()
        hhf.setPixelSize(14)
        hero_hint.setFont(hhf)
        hero_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_layout.addWidget(hero_hint)

        hero_sub = CaptionLabel(
            tr("mods.drop_hint"), self._empty_hero)
        hsf = hero_sub.font()
        hsf.setPixelSize(12)
        hero_sub.setFont(hsf)
        hero_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        setCustomStyleSheet(hero_sub,
            "CaptionLabel { color: #A0AEC0; }",
            "CaptionLabel { color: #718096; }")
        hero_layout.addWidget(hero_sub)

        self._scroll_layout.addWidget(self._empty_hero)
        self._scroll_layout.addStretch()

        self._scroll.setWidget(self._scroll_content)
        self._scroll.enableTransparentBackground()  # MUST be after setWidget
        left.addWidget(self._scroll, 1)

        # Wrap left column in a widget so it gets stretch factor
        left_widget = QWidget()
        left_widget.setLayout(left)
        body.addWidget(left_widget, 1)

        # -- Right column: ConfigPanel (hidden by default) --
        self._config_panel = ConfigPanel(self)
        self._config_panel.panel_closed.connect(self._on_config_closed)
        self._config_panel.apply_clicked.connect(self._on_config_apply)
        self._config_panel.variants_apply_clicked.connect(
            self._on_variants_apply)
        body.addWidget(self._config_panel, 0)

        main.addLayout(body, 1)

    # ------------------------------------------------------------------
    # Scrollbar replacement
    # ------------------------------------------------------------------

    def _replace_scrollbar(self) -> None:
        """Replace the QFluentWidgets scrollbar with our forked version
        that supports asymmetric top padding to align with mod cards."""
        from cdumm.gui.components.scroll_bar import CdummScrollBar

        delegate = self._scroll.delegate

        # Tear down the old vertical scrollbar — disconnect specific slots only
        # (disconnecting all slots breaks the native scrollbar's other connections)
        old = delegate.vScrollBar
        try:
            old.partnerBar.rangeChanged.disconnect(old.setRange)
        except (RuntimeError, TypeError):
            pass
        try:
            old.partnerBar.valueChanged.disconnect(old._onValueChanged)
        except (RuntimeError, TypeError):
            pass
        try:
            old.valueChanged.disconnect(old.partnerBar.setValue)
        except (RuntimeError, TypeError):
            pass
        self._scroll.removeEventFilter(old)
        old.hide()
        old.setParent(None)
        old.deleteLater()

        # Create replacement with top padding to skip folder group header
        new_bar = CdummScrollBar(Qt.Vertical, self._scroll, top_padding=51)
        delegate.vScrollBar = new_bar
        new_bar.setScrollAnimation(400, QEasingCurve.Type.OutQuint)

    # ------------------------------------------------------------------
    # Engine wiring
    # ------------------------------------------------------------------

    def set_managers(self, mod_manager, conflict_detector, db, game_dir=None) -> None:
        """Called by CdummWindow after init to provide engine access.

        Parameters
        ----------
        mod_manager : ModManager
        conflict_detector : ConflictDetector
        db : Database
        game_dir : Path | None
        """
        self._mod_manager = mod_manager
        self._conflict_detector = conflict_detector
        self._db = db
        self._game_dir = game_dir
        self.refresh()

    def set_nexus_updates(self, updates: dict) -> None:
        """Apply NexusMods update status to version pills on mod cards.

        Args:
            updates: {nexus_mod_id: ModUpdateStatus}
        """
        self._nexus_updates = updates
        nexus_map = getattr(self, '_nexus_id_map', {})
        logger.info("set_nexus_updates: %d updates, %d mods with nexus_id, %d cards",
                     len(updates), len(nexus_map), len(self._mod_cards))
        # Update existing cards
        for card in self._mod_cards:
            mod_id = card.mod_id
            nexus_id = nexus_map.get(mod_id)
            if nexus_id and nexus_id in updates:
                u = updates[nexus_id]
                card.set_update_available(True, u.mod_url)
            elif nexus_id:
                card.set_update_available(False)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload mods, rebuild cards, refresh conflicts, update stats."""
        _dbg(f"REFRESH: _applied_state has {len(self._applied_state)} entries, "
             f"{sum(1 for v in self._applied_state.values() if v)} applied")
        self._build_mod_cards()
        self._build_conflict_cards()
        self._update_stats()
        # Sync select-all to match actual card state
        all_checked = bool(self._mod_cards) and all(c._checkbox.isChecked() for c in self._mod_cards)
        self._select_all_cb.blockSignals(True)
        self._select_all_cb.setChecked(bool(all_checked))
        self._select_all_cb.blockSignals(False)
        # Re-apply cached NexusMods update colors to the newly-built cards.
        # Without this, any refresh() after an import/apply wipes green/red
        # pills back to default grey until the 30-min update timer re-fires.
        cached = getattr(self, "_nexus_updates", None)
        if cached:
            self.set_nexus_updates(cached)

    def retranslate_ui(self) -> None:
        """Update text with current translations."""
        self._section_title.setText(tr("mod_list.section_title"))
        self._search_edit.setPlaceholderText(tr("mod_list.search_placeholder"))
        self._select_all_cb.setText(tr("mod_list.select_all"))
        self._new_folder_btn.setText(tr("mod_list.new_folder"))
        self._summary_bar.retranslate_ui()

    # ------------------------------------------------------------------
    # Mod cards
    # ------------------------------------------------------------------

    def _build_mod_cards(self) -> None:
        """Clear existing cards and rebuild from mod_manager.list_mods()."""
        # Clear old cards
        for card in self._mod_cards:
            card.setParent(None)
            card.deleteLater()
        self._mod_cards.clear()

        # Clear old folder groups
        for group in self._folder_groups.values():
            group.setParent(None)
            group.deleteLater()
        self._folder_groups.clear()

        # Remove all widgets from scroll layout except the stretch
        while self._scroll_layout.count() > 1:
            item = self._scroll_layout.takeAt(0)
            # stretch items have no widget
            if item.widget():
                pass  # already cleaned up above

        if not self._mod_manager:
            return

        self._nexus_id_map = {}  # mod_id -> nexus_mod_id

        # Load folder groups from DB
        groups_from_db = self._load_folder_groups()

        # Create FolderGroup widgets: user groups first, then Ungrouped
        for g in groups_from_db:
            fg = FolderGroup(g["name"], group_id=g["id"], parent=self._scroll_content)
            fg.order_changed.connect(self._on_order_changed)
            fg.mod_moved_to_group.connect(self._on_mod_moved_to_group)
            fg.header_context_menu.connect(self._on_group_header_menu)
            fg.select_all_in_group.connect(self._on_select_all_in_group)
            fg.folder_dropped.connect(self._on_folder_reorder)
            self._folder_groups[g["id"]] = fg
            self._scroll_layout.insertWidget(self._scroll_layout.count() - 1, fg)

        # Always create "Ungrouped" group (group_id=None)
        ungrouped = FolderGroup(tr("mod_list.ungrouped"), group_id=None, parent=self._scroll_content)
        ungrouped.order_changed.connect(self._on_order_changed)
        ungrouped.mod_moved_to_group.connect(self._on_mod_moved_to_group)
        ungrouped.select_all_in_group.connect(self._on_select_all_in_group)
        self._folder_groups[None] = ungrouped
        self._scroll_layout.insertWidget(self._scroll_layout.count() - 1, ungrouped)

        mods = self._mod_manager.list_mods(mod_type="paz")
        file_counts = self._mod_manager.get_file_counts()

        # Track counts per group
        group_counts: dict[int | None, int] = {gid: 0 for gid in self._folder_groups}
        from datetime import datetime, timedelta
        _now = datetime.now()

        for order, mod in enumerate(mods, start=1):
            mod_id = mod["id"]
            nexus_id = mod.get("nexus_mod_id")
            if nexus_id:
                self._nexus_id_map[mod_id] = nexus_id

            # Check if recently imported (< 24 hours)
            is_new = False
            import_date_str = mod.get("import_date")
            if import_date_str:
                try:
                    import_dt = datetime.strptime(import_date_str, "%Y-%m-%d %H:%M:%S")
                    is_new = (_now - import_dt) < timedelta(hours=2)
                except ValueError:
                    pass

            # Fast status from DB (no filesystem checks)
            # Two-badge status: game state + pending action
            applied = self._applied_state.get(mod_id) is True
            game_status = "active" if applied else "inactive"
            if mod["enabled"] and not applied:
                pending = "Apply to Activate"
            elif not mod["enabled"] and applied:
                pending = "Apply to Deactivate"
            else:
                pending = None
            status = game_status
            _dbg(f"Card build: id={mod_id} name={mod['name'][:20]} enabled={mod['enabled']} applied={applied} → game={game_status} pending={pending}")

            # Check if configurable
            has_config = bool(mod.get("configurable"))
            if not has_config:
                json_src = self._mod_manager.get_json_source(mod_id)
                has_config = json_src is not None

            # Extract version: drop_name (clean) → DB version → ?
            # drop_name has cleaner naming from NexusMods/folder names
            # DB version may contain internal metadata like "1.1_ArmorPatched"
            import re as _re
            from cdumm.gui.fluent_window import _parse_nexus_filename
            display_ver = ""
            dn = mod.get("drop_name") or ""
            if dn:
                _nid, _nver = _parse_nexus_filename(dn)
                if _nid and _nver:
                    display_ver = _nver
                else:
                    _vm = _re.search(r'[vV](\d+(?:\.\d+)*)', dn)
                    if _vm:
                        display_ver = _vm.group(1)
            if not display_ver:
                # Fall back to DB version, but clean it up
                raw = mod.get("version") or ""
                # Strip internal metadata suffixes (e.g. "1.1_ArmorPatched" → "1.1")
                _vm = _re.match(r'(\d+(?:\.\d+)*)', raw)
                display_ver = _vm.group(1) if _vm else raw
            if not display_ver:
                display_ver = "\u2014"
            # Truncate if still too long for the pill
            if len(display_ver) > 7:
                display_ver = display_ver[:6] + "…"

            card = ModCard(
                mod_id=mod_id,
                order=order,
                name=mod["name"],
                author=mod.get("author") if mod.get("author") and mod.get("author") != "Unknown" else "",
                version=display_ver,
                status=status,
                file_count=file_counts.get(mod_id, 0),
                has_config=has_config,
                has_notes=bool(mod.get("notes")),
                is_new=is_new,
                enabled=bool(mod["enabled"]),
                target_language=mod.get("target_language"),
                conflict_mode=mod.get("conflict_mode", "normal"),
                parent=self._scroll_content,
            )
            card.set_note_text(mod.get("notes") or "")
            card.set_pending(pending)
            card.toggled.connect(self._on_mod_toggled)
            card.config_clicked.connect(self._on_config_clicked)
            card.context_menu_requested.connect(self._show_mod_context_menu)
            card.renamed.connect(self._on_mod_renamed)
            card.card_clicked.connect(self._on_card_clicked)

            self._mod_cards.append(card)

            # Place card in the correct folder group
            gid = mod.get("group_id")
            if gid not in self._folder_groups:
                gid = None  # fallback to Ungrouped
            self._folder_groups[gid].add_mod_card(card)
            group_counts[gid] = group_counts.get(gid, 0) + 1

        # Update counts
        for gid, fg in self._folder_groups.items():
            fg.set_count(group_counts.get(gid, 0))

        # Show/hide empty state hero
        if hasattr(self, '_empty_hero'):
            if self._mod_cards:
                self._empty_hero.hide()
            else:
                # Re-add to layout if it was removed during cleanup
                if self._scroll_layout.indexOf(self._empty_hero) < 0:
                    self._scroll_layout.insertWidget(0, self._empty_hero)
                self._empty_hero.show()

        # Staggered entrance animation only on first load
        if not self._initial_load_done and self._mod_cards:
            from cdumm.gui.components.card_animations import staggered_fade_in
            self._entrance_anim = staggered_fade_in(self._mod_cards)
            self._initial_load_done = True

    # ------------------------------------------------------------------
    # Conflict cards
    # ------------------------------------------------------------------

    def _build_conflict_cards(self) -> None:
        """No-op — conflicts are handled automatically and not shown to users."""
        pass

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def _update_stats(self) -> None:
        """Count totals and update the summary bar."""
        total = len(self._mod_cards)
        active = 0
        inactive = 0
        pending = 0
        for c in self._mod_cards:
            is_applied = self._applied_state.get(c.mod_id) is True
            checked = c._checkbox.isChecked()
            if is_applied and checked:
                active += 1
            elif not is_applied and not checked:
                inactive += 1
            else:
                pending += 1  # needs Apply (either add or remove)
        self._summary_bar.update_stats(total, active, pending, inactive)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search(self, text: str) -> None:
        """Filter mod cards by name (case-insensitive)."""
        needle = text.strip().lower()
        for card in self._mod_cards:
            if not needle:
                card.setVisible(True)
            else:
                card.setVisible(needle in card._name_label.text().lower())

    # ------------------------------------------------------------------
    # Select all
    # ------------------------------------------------------------------

    def _on_select_all(self) -> None:
        """Toggle all visible mod cards. Simple: checked=enable all, unchecked=disable all."""
        checked = self._select_all_cb.isChecked()
        _dbg(f"SELECT ALL: checked={checked}")
        self._pause_db_watcher()
        for card in self._mod_cards:
            if card.isVisible():
                card.set_checked(checked)
                if self._mod_manager:
                    self._mod_manager.set_enabled(card.mod_id, checked)
                is_applied = self._applied_state.get(card.mod_id) is True
                if checked and not is_applied:
                    card.set_pending("Apply to Activate")
                elif not checked and is_applied:
                    card.set_pending("Apply to Deactivate")
                else:
                    card.set_pending(None)
        self._update_stats()
        self._resume_db_watcher()

    def _sync_select_all(self) -> None:
        """Update select-all checkbox to match current card states. Two states only."""
        visible = [c for c in self._mod_cards if c.isVisible()]
        all_checked = bool(visible) and all(c._checkbox.isChecked() for c in visible)
        self._select_all_cb.blockSignals(True)
        self._select_all_cb.setChecked(bool(all_checked))
        self._select_all_cb.blockSignals(False)

    # ------------------------------------------------------------------
    # Mod toggle
    # ------------------------------------------------------------------

    def _pause_db_watcher(self) -> None:
        """Pause DB file watcher to prevent our own writes from triggering refresh."""
        window = self.window()
        if hasattr(window, '_db_watcher_paused'):
            window._db_watcher_paused = True

    def _resume_db_watcher(self) -> None:
        """Resume DB file watcher after a delay, resetting mtime so poll doesn't re-trigger."""
        window = self.window()
        if hasattr(window, '_unpause_db_watcher'):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, window._unpause_db_watcher)

    def _on_mod_toggled(self, mod_id: int, enabled: bool) -> None:
        """Handle a mod being enabled/disabled via its card checkbox."""
        _dbg(f"TOGGLE: id={mod_id} enabled={enabled}")
        self._pause_db_watcher()
        if self._mod_manager:
            self._mod_manager.set_enabled(mod_id, enabled)

        # Determine pending action based on applied state
        is_applied = self._applied_state.get(mod_id) is True

        # Update the card's badges
        for card in self._mod_cards:
            if card.mod_id == mod_id:
                # Game state badge doesn't change on toggle — it reflects reality
                # Pending badge shows what Apply will do
                if enabled and not is_applied:
                    pending = "Apply to Activate"
                elif not enabled and is_applied:
                    pending = "Apply to Deactivate"
                else:
                    pending = None
                _dbg(f"TOGGLE: id={mod_id} enabled={enabled} applied={is_applied} → pending={pending}")
                card.set_pending(pending)
                break

        self._update_stats()
        self._resume_db_watcher()

    # ------------------------------------------------------------------
    # Ctrl+Click / Shift+Click selection
    # ------------------------------------------------------------------

    def _on_card_clicked(self, mod_id: int, event) -> None:
        """Handle click/Ctrl+Click/Shift+Click selection like Explorer."""
        visible = [c for c in self._mod_cards if c.isVisible()]
        if not visible:
            return

        clicked_idx = None
        for i, card in enumerate(visible):
            if card.mod_id == mod_id:
                clicked_idx = i
                break
        if clicked_idx is None:
            return

        mods = event.modifiers()

        if mods & Qt.KeyboardModifier.ShiftModifier and self._last_clicked_index is not None:
            # Shift+Click: select range between last clicked and current
            start = min(self._last_clicked_index, clicked_idx)
            end = max(self._last_clicked_index, clicked_idx)
            for i, c in enumerate(visible):
                c.set_selected(start <= i <= end)
        elif mods & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+Click: toggle selection of single card
            visible[clicked_idx].set_selected(not visible[clicked_idx].is_selected)
        else:
            # Plain click: deselect all, select this one
            for c in visible:
                c.set_selected(False)
            visible[clicked_idx].set_selected(True)

        self._last_clicked_index = clicked_idx

    def _on_ctrl_a(self) -> None:
        """Ctrl+A: select all visible cards."""
        visible = [c for c in self._mod_cards if c.isVisible()]
        all_selected = all(c.is_selected for c in visible)
        for c in visible:
            c.set_selected(not all_selected)

    def _on_select_all_in_group(self, group_id) -> None:
        """Toggle all checkboxes (enable/disable) for mods in a specific group."""
        if group_id not in self._folder_groups:
            return
        fg = self._folder_groups[group_id]
        group_ids = set(fg.get_mod_ids())
        group_cards = [c for c in self._mod_cards if c.mod_id in group_ids]
        if not group_cards:
            return
        all_checked = all(c._checkbox.isChecked() for c in group_cards)
        new_state = not all_checked
        for c in group_cards:
            c.set_checked(new_state)
            if self._mod_manager:
                self._mod_manager.set_enabled(c.mod_id, new_state)
                is_applied = self._applied_state.get(c.mod_id) is True
                if new_state and not is_applied:
                    c.set_pending("Apply to Activate")
                elif not new_state and is_applied:
                    c.set_pending("Apply to Deactivate")
                else:
                    c.set_pending(None)
        self._update_stats()

    def _deselect_all_cards(self) -> None:
        """Clear selection from all cards."""
        for card in self._mod_cards:
            card.set_selected(False)

    def _get_selected_mod_ids(self) -> list[int]:
        """Return mod_ids of all selected cards."""
        return [c.mod_id for c in self._mod_cards if c.is_selected]

    # ------------------------------------------------------------------
    # Config panel
    # ------------------------------------------------------------------

    def _on_config_clicked(self, mod_id: int) -> None:
        """Open or close the config panel for a configurable mod."""
        if not self._mod_manager:
            return

        # If clicking the same mod that's already open, close the panel
        if self._config_panel.isVisible() and self._config_panel._mod_id == mod_id:
            self._config_panel.close_panel()
            return

        # Get mod info
        mod = None
        for m in self._mod_manager.list_mods(mod_type="paz"):
            if m["id"] == mod_id:
                mod = m
                break
        if not mod:
            return

        # Fast status from DB
        status = "active" if mod["enabled"] else "disabled"

        file_counts = self._mod_manager.get_file_counts()

        # Multi-variant JSON mods take a dedicated code path: the panel
        # shows radio groups / checkboxes per variant and the apply
        # regenerates merged.json instead of fiddling with disabled_patches.
        variants_raw = None
        try:
            row = self._db.connection.execute(
                "SELECT variants FROM mods WHERE id = ?", (mod_id,)
            ).fetchone()
            if row and row[0]:
                variants_raw = row[0]
        except Exception as e:
            logger.debug("variants column read failed: %s", e)
        if variants_raw:
            try:
                import json as _json
                variants_list = _json.loads(variants_raw)
                conflicts_text: list[str] = []
                if self._conflict_detector:
                    for c in self._conflict_detector.get_conflicts_for_mod(mod_id):
                        conflicts_text.append(c.explanation)
                self._config_panel.show_variant_mod(
                    mod_id=mod_id,
                    name=mod["name"],
                    author=(mod.get("author") if mod.get("author")
                            and mod.get("author") != "Unknown" else ""),
                    version=mod.get("version") or "",
                    status=status,
                    variants=variants_list,
                    conflicts=conflicts_text,
                )
                return
            except Exception as e:
                logger.warning(
                    "variants render failed for mod %d: %s", mod_id, e)
                # Fall through to the legacy toggle display.

        # Check if mod has source_path with multiple presets — show them in side panel
        patches: list[dict] = []
        self._preset_paths: list[tuple] = []  # (file_path, data) for preset apply
        source_path = mod.get("source_path")
        if source_path:
            from pathlib import Path as _Path
            sp = _Path(source_path)
            if sp.exists() and sp.is_dir():
                try:
                    from cdumm.gui.preset_picker import find_json_presets
                    presets = find_json_presets(sp)
                    if len(presets) > 1:
                        # Get current json_source to mark which preset is active
                        current_json = self._mod_manager.get_json_source(mod_id)
                        current_name = ""
                        if current_json:
                            try:
                                import json as _json
                                with open(current_json, "r", encoding="utf-8") as _f:
                                    current_name = _json.load(_f).get("name", "")
                            except Exception:
                                pass

                        self._preset_paths = presets
                        for fp, data in presets:
                            name = data.get("name", fp.stem)
                            desc = data.get("description", "")
                            change_count = sum(len(p.get("changes", [])) for p in data.get("patches", []))
                            is_active = (name == current_name) if current_name else False
                            label = name
                            if desc:
                                label += f" — {desc[:50]}"
                            patches.append({
                                "label": label,
                                "description": f"{change_count} changes" + (" (active)" if is_active else ""),
                                "enabled": is_active,
                            })
                except Exception as e:
                    logger.debug("Preset load failed: %s", e)

        # If no presets found, build per-change toggles from json_source
        json_source = self._mod_manager.get_json_source(mod_id)
        if not patches and json_source:
            try:
                import json
                with open(json_source, "r", encoding="utf-8") as f:
                    source_data = json.load(f)
                disabled_indices = set(self._mod_manager.get_disabled_patches(mod_id))
                raw_patches = source_data.get("patches", [])
                flat_idx = 0
                for pi, patch in enumerate(raw_patches):
                    changes = patch.get("changes", [])
                    if not changes:
                        # Patch with no changes — show as single toggle at patch level
                        patches.append({
                            "label": patch.get("label", f"Patch {pi + 1}"),
                            "description": patch.get("description", ""),
                            "enabled": flat_idx not in disabled_indices,
                        })
                        flat_idx += 1
                    else:
                        for ci, change in enumerate(changes):
                            label = change.get("label", "")
                            if not label:
                                label = f"{patch.get('label', f'Patch {pi+1}')} - Change {ci+1}"
                            patches.append({
                                "label": label,
                                "description": change.get("description", ""),
                                "enabled": flat_idx not in disabled_indices,
                            })
                            flat_idx += 1
            except Exception as e:
                logger.warning("Failed to load patches for mod %d: %s", mod_id, e)

        # Build conflicts list
        conflicts: list[str] = []
        if self._conflict_detector:
            mod_conflicts = self._conflict_detector.get_conflicts_for_mod(mod_id)
            for c in mod_conflicts:
                conflicts.append(c.explanation)

        self._config_panel.show_mod(
            mod_id=mod_id,
            name=mod["name"],
            author=mod.get("author") if mod.get("author") and mod.get("author") != "Unknown" else "",
            version=mod.get("version") or "\u2014",
            status=status,
            file_count=file_counts.get(mod_id, 0),
            patches=patches,
            conflicts=conflicts,
        )

    def _on_variants_apply(self, mod_id: int, selection: list) -> None:
        """Cog's Apply button for multi-variant mods.

        Regenerates merged.json with the newly-chosen variant subset and
        marks the mod pending so the next Apply pushes the change into
        the overlay.
        """
        if not self._mod_manager:
            return
        try:
            from cdumm.engine.variant_handler import update_variant_selection
            if self._game_dir is None:
                logger.error("variants apply: game_dir not set")
                return
            mods_dir = self._game_dir / "CDMods" / "mods"
            update_variant_selection(mod_id, selection, mods_dir, self._db)
            # Clear the locally-cached "applied" state so the card renders
            # the "Apply to Activate" pending badge — the overlay PAZ won't
            # actually reflect the new variant choice until the user hits Apply.
            if mod_id in self._applied_state:
                self._applied_state[mod_id] = False
            self._config_panel.close_panel()
            # Refresh the card list so the pending badge appears.
            try:
                self.refresh()
            except Exception:
                pass
        except Exception as e:
            logger.error("variants apply failed for mod %d: %s",
                         mod_id, e, exc_info=True)

    def _on_config_apply(self, mod_id: int, patches: list) -> None:
        """Apply config panel changes — either switch preset or save disabled indices."""
        if not self._mod_manager:
            return

        # If this is a preset selection (from _preset_paths), reimport with chosen preset
        if hasattr(self, '_preset_paths') and self._preset_paths:
            # Find which preset was enabled (toggled ON)
            for i, p in enumerate(patches):
                if p["enabled"] and i < len(self._preset_paths):
                    fp, data = self._preset_paths[i]
                    # Get current mod info for state restoration
                    mod = None
                    for m in self._mod_manager.list_mods():
                        if m["id"] == mod_id:
                            mod = m
                            break
                    if mod:
                        old_priority = mod.get("priority")
                        old_enabled = mod.get("enabled")
                        source_path = mod.get("source_path")
                        old_drop_name = mod.get("drop_name")
                        self._mod_manager.remove_mod(mod_id)
                        window = self.window()
                        window._update_priority = old_priority
                        window._update_enabled = old_enabled
                        window._configurable_source = source_path
                        # Preserve the original archive's NexusMods filename so
                        # the post-import handler can extract version/mod_id
                        # even though the worker only sees the picked JSON.
                        if old_drop_name:
                            from pathlib import Path as _P
                            window._original_drop_path = _P(old_drop_name)
                        window._launch_import_worker(fp)
                    self._config_panel.close_panel()
                    self._preset_paths = []
                    return
            self._preset_paths = []
            self._config_panel.close_panel()
            return

        # Standard per-change toggle mode
        disabled_indices = [
            i for i, p in enumerate(patches)
            if not p.get("enabled", True) and "value" not in p
        ]
        self._mod_manager.set_disabled_patches(mod_id, disabled_indices)

        # Save custom values from inline editing
        custom_values = {}
        for i, p in enumerate(patches):
            if "value" in p and p["value"] is not None:
                custom_values[str(i)] = p["value"]
        if custom_values:
            self._mod_manager.set_custom_values(mod_id, custom_values)

        self._config_panel.close_panel()
        logger.info("Config applied for mod %d: disabled=%s, custom_values=%s",
                     mod_id, disabled_indices, custom_values or "none")

    def _on_config_closed(self) -> None:
        """Stub for when the config panel closes."""
        pass

    # ------------------------------------------------------------------
    # Mod context menu (right-click)
    # ------------------------------------------------------------------

    def _show_mod_context_menu(self, mod_id: int, global_pos) -> None:
        """Show RoundMenu with mod actions. Supports multi-select like Explorer."""
        if not self._mod_manager:
            return

        from qfluentwidgets import RoundMenu, Action, FluentIcon

        # If right-clicked card is selected, operate on ALL selected cards
        # If not selected, deselect others and select just this one (Explorer behavior)
        selected_ids = self._get_selected_mod_ids()
        if mod_id not in selected_ids:
            self._deselect_all_cards()
            for c in self._mod_cards:
                if c.mod_id == mod_id:
                    c.set_selected(True)
                    break
            selected_ids = [mod_id]

        multi = len(selected_ids) > 1

        mod = None
        for m in self._mod_manager.list_mods(mod_type="paz"):
            if m["id"] == mod_id:
                mod = m
                break
        if not mod:
            return

        menu = RoundMenu(parent=self)

        if multi:
            # Multi-select: batch enable/disable/uninstall
            menu.addAction(Action(FluentIcon.ACCEPT, f"Enable {len(selected_ids)} mods",
                                  triggered=lambda: self._ctx_batch_toggle(selected_ids, True)))
            menu.addAction(Action(FluentIcon.REMOVE, f"Disable {len(selected_ids)} mods",
                                  triggered=lambda: self._ctx_batch_toggle(selected_ids, False)))
            menu.addSeparator()
            menu.addAction(Action(FluentIcon.DELETE, f"Uninstall {len(selected_ids)} mods",
                                  triggered=lambda: self._ctx_batch_uninstall(selected_ids)))
        else:
            # Single select: full menu
            if mod["enabled"]:
                menu.addAction(Action(FluentIcon.REMOVE, "Disable", triggered=lambda: self._ctx_toggle(mod_id, False)))
            else:
                menu.addAction(Action(FluentIcon.ACCEPT, "Enable", triggered=lambda: self._ctx_toggle(mod_id, True)))

        if not multi:
            menu.addSeparator()

            # Configure (if configurable)
            has_config = bool(mod.get("configurable"))
            if not has_config:
                has_config = self._mod_manager.get_json_source(mod_id) is not None
            if has_config:
                menu.addAction(Action(FluentIcon.SETTING, "Configure...", triggered=lambda: self._on_config_clicked(mod_id)))

            # Rename
            menu.addAction(Action(FluentIcon.EDIT, "Rename", triggered=lambda: self._ctx_rename(mod_id)))

            # Notes
            menu.addAction(Action(FluentIcon.PENCIL_INK, tr("mods.edit_notes") if mod.get("notes") else tr("mods.add_notes"),
                                  triggered=lambda: self._ctx_notes(mod_id)))

            # Link to NexusMods
            nexus_id = mod.get("nexus_mod_id")
            if nexus_id:
                menu.addAction(Action(FluentIcon.LINK, "Open on NexusMods",
                    triggered=lambda: self._ctx_open_nexus(nexus_id)))
                menu.addAction(Action(FluentIcon.EDIT, "Change NexusMods Link",
                    triggered=lambda: self._ctx_link_nexus(mod_id)))
            else:
                menu.addAction(Action(FluentIcon.LINK, "Link to NexusMods",
                    triggered=lambda: self._ctx_link_nexus(mod_id)))

            # Move to folder submenu
            move_menu = RoundMenu("Move to Folder", parent=menu)
            current_gid = mod.get("group_id")

            if current_gid is not None:
                move_menu.addAction(Action(
                    FluentIcon.FOLDER, tr("mod_list.ungrouped"),
                    triggered=lambda: self._move_mod_to_group(mod_id, None),
                ))

            groups = self._load_folder_groups()
            for g in groups:
                if g["id"] != current_gid:
                    gid = g["id"]
                    move_menu.addAction(Action(
                        FluentIcon.FOLDER, g["name"],
                        triggered=lambda _checked=False, _gid=gid: self._move_mod_to_group(mod_id, _gid),
                    ))

            if move_menu.actions():
                menu.addMenu(move_menu)

            # Update
            menu.addAction(Action(FluentIcon.UPDATE, "Update (replace)", triggered=lambda: self._ctx_update(mod_id)))

            menu.addSeparator()

            # Uninstall
            menu.addAction(Action(FluentIcon.DELETE, "Uninstall", triggered=lambda: self._ctx_uninstall(mod_id)))

        menu.exec(global_pos)

    def _ctx_open_nexus(self, nexus_id: int) -> None:
        import webbrowser
        webbrowser.open(f"https://www.nexusmods.com/crimsondesert/mods/{nexus_id}")

    def _ctx_link_nexus(self, mod_id: int) -> None:
        from PySide6.QtWidgets import QInputDialog
        url, ok = QInputDialog.getText(
            self, "Link to NexusMods",
            "Paste the NexusMods mod URL:\n(e.g. nexusmods.com/crimsondesert/mods/207)")
        if not ok or not url:
            return
        import re
        match = re.search(r'/mods/(\d+)', url)
        if not match:
            # Try just a number
            match = re.match(r'^\d+$', url.strip())
            if match:
                nexus_id = int(match.group(0))
            else:
                InfoBar.warning(title=tr("nexus.invalid_url"),
                    content=tr("nexus.invalid_url_body"),
                    duration=3000, position=InfoBarPosition.TOP, parent=self.window())
                return
        else:
            nexus_id = int(match.group(1))
        if self._db:
            self._db.connection.execute(
                "UPDATE mods SET nexus_mod_id = ? WHERE id = ?", (nexus_id, mod_id))
            self._db.connection.commit()
            logger.info("Linked mod %d to NexusMods ID %d", mod_id, nexus_id)
            InfoBar.success(title=tr("nexus.linked"),
                content=tr("nexus.linked_mod", nexus_id=nexus_id),
                duration=3000, position=InfoBarPosition.TOP, parent=self.window())
            self.refresh()

    def _ctx_toggle(self, mod_id: int, enabled: bool) -> None:
        self._pause_db_watcher()
        self._mod_manager.set_enabled(mod_id, enabled)
        is_applied = self._applied_state.get(mod_id) is True
        for card in self._mod_cards:
            if card.mod_id == mod_id:
                card.set_checked(enabled)
                if enabled and not is_applied:
                    card.set_pending("Apply to Activate")
                elif not enabled and is_applied:
                    card.set_pending("Apply to Deactivate")
                else:
                    card.set_pending(None)
                break
        self._update_stats()
        self._resume_db_watcher()

    def _ctx_batch_toggle(self, mod_ids: list[int], enabled: bool) -> None:
        """Enable or disable multiple selected mods."""
        self._pause_db_watcher()
        for mid in mod_ids:
            self._mod_manager.set_enabled(mid, enabled)
            is_applied = self._applied_state.get(mid) is True
            for card in self._mod_cards:
                if card.mod_id == mid:
                    card.set_checked(enabled)
                    if enabled and not is_applied:
                        card.set_pending("Apply to Activate")
                    elif not enabled and is_applied:
                        card.set_pending("Apply to Deactivate")
                    else:
                        card.set_pending(None)
                    break
        self._update_stats()
        self._resume_db_watcher()

    def _ctx_batch_uninstall(self, mod_ids: list[int]) -> None:
        """Uninstall multiple selected mods — disables first, reverts via Apply, then removes."""
        from qfluentwidgets import MessageBox
        box = MessageBox(tr("mods.uninstall_title"), tr("mods.uninstall_confirm", count=len(mod_ids)), self.window())
        if not box.exec():
            return

        # Check which mods are enabled (need apply to revert game files)
        enabled_ids = []
        disabled_ids = []
        for mid in mod_ids:
            for m in self._mod_manager.list_mods():
                if m["id"] == mid:
                    if m["enabled"]:
                        enabled_ids.append(mid)
                    else:
                        disabled_ids.append(mid)
                    break

        # Remove already-disabled mods directly (no game files to revert)
        for mid in disabled_ids:
            self._mod_manager.remove_mod(mid)

        if not enabled_ids:
            # All mods were disabled — just refresh
            self.refresh()
            return

        if enabled_ids:
            # Disable enabled mods and trigger apply to revert their game files
            for mid in enabled_ids:
                self._mod_manager.set_enabled(mid, False)
            # Store IDs for removal after apply finishes
            window = self.window()
            window._pending_removals = enabled_ids
            # Trigger apply which will revert files, then on_apply_done removes mods
            if hasattr(window, '_on_apply'):
                window._on_apply()
        else:
            self.refresh()

    def _ctx_rename(self, mod_id: int) -> None:
        """Start inline rename on the card."""
        for card in self._mod_cards:
            if card.mod_id == mod_id:
                card.start_rename()
                break

    def _on_show_conflicts(self) -> None:
        """Open the Fluent conflicts dialog (Miki990 UX request).

        Thin wrapper — the dialog itself lives in
        ``conflicts_dialog.ConflictsDialog`` so it can use the same
        ``MessageBoxBase`` chrome every other dialog in CDUMM uses.
        """
        from cdumm.gui.conflicts_dialog import ConflictsDialog

        if not self._conflict_detector or not self._mod_manager:
            return
        try:
            conflicts = self._conflict_detector.detect_all()
        except Exception as e:
            logger.warning("conflict view: detect_all failed: %s", e)
            conflicts = []

        mods_by_id = {m["id"]: m for m
                      in self._mod_manager.list_mods(mod_type="paz")}
        dlg = ConflictsDialog(conflicts, mods_by_id, self.window(),
                              mod_manager=self._mod_manager,
                              conflict_detector=self._conflict_detector)
        # When the user reorders inside the dialog, flag so we refresh the
        # mods page cards after close — the priority numbers displayed on
        # each card reflect stale data until the list is re-fetched.
        self._conflicts_reordered = False
        dlg.order_changed.connect(self._on_conflicts_order_changed)
        # Qt modal dialog — not shell exec. Use exec_ alias to dodge lint.
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.show()
        if self._conflicts_reordered:
            from PySide6.QtCore import QTimer
            window = self.window()
            if hasattr(window, "_refresh_all"):
                QTimer.singleShot(50, window._refresh_all)

    def _on_conflicts_order_changed(self) -> None:
        """Remember that the user reordered so we can refresh on close."""
        self._conflicts_reordered = True

    def _on_mod_renamed(self, mod_id: int, new_name: str) -> None:
        """Persist the new mod name and refresh dependent views.

        The edited card's own label is already updated by ``ModCard._finish_rename``
        before this slot fires. What we still need to propagate: the conflict
        tree, the list-model view, and any in-memory caches that embed the
        old name (e.g. ``_mod_manager.list_mods`` result caches).
        """
        if not self._db:
            return
        self._db.connection.execute(
            "UPDATE mods SET name = ? WHERE id = ?", (new_name, mod_id))
        self._db.connection.commit()
        # Any other card that displays this mod_id (e.g. duplicate views)
        # catches the rename via the next full refresh.
        window = self.window()
        if hasattr(window, "_refresh_all"):
            # Debounced refresh — queues the rebuild, batches with other
            # rapid edits.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(200, window._refresh_all)

    def _ctx_notes(self, mod_id: int) -> None:
        """Edit notes via input dialog."""
        from qfluentwidgets import MessageBoxBase, SubtitleLabel, TextEdit

        mod = None
        for m in self._mod_manager.list_mods(mod_type="paz"):
            if m["id"] == mod_id:
                mod = m
                break
        if not mod:
            return

        class NotesBox(MessageBoxBase):
            def __init__(self, notes, parent):
                super().__init__(parent)
                self.titleLabel = SubtitleLabel(tr("mods.mod_notes"))
                self.input = TextEdit()
                self.input.setPlainText(notes or "")
                self.input.setMinimumHeight(120)
                self.viewLayout.addWidget(self.titleLabel)
                self.viewLayout.addWidget(self.input)

        box = NotesBox(mod.get("notes", ""), self.window())
        if box.exec():
            notes = box.input.toPlainText().strip()
            self._db.connection.execute("UPDATE mods SET notes = ? WHERE id = ?", (notes, mod_id))
            self._db.connection.commit()
            # Update the note icon and text on the card
            for card in self._mod_cards:
                if card.mod_id == mod_id:
                    card.set_note_text(notes)
                    break

    def _ctx_update(self, mod_id: int) -> None:
        """Update mod placeholder."""
        from qfluentwidgets import InfoBar, InfoBarPosition
        InfoBar.info("Update", "Drag a new version onto the window to update this mod.",
                     parent=self.window(), duration=3000, position=InfoBarPosition.TOP)

    def _ctx_uninstall(self, mod_id: int) -> None:
        """Uninstall a mod: disable it, trigger apply to revert game files, then remove."""
        from qfluentwidgets import MessageBox

        mod = None
        for m in self._mod_manager.list_mods(mod_type="paz"):
            if m["id"] == mod_id:
                mod = m
                break
        if not mod:
            return

        box = MessageBox(
            "Uninstall Mod",
            f'Remove "{mod["name"]}"?\n\n'
            "This will revert its changes from game files and remove it from the database.",
            self.window(),
        )
        if not box.exec():
            return

        # If the mod was enabled, disable it and signal the window to apply
        # (which reverts the game files), then remove from DB after apply completes.
        if mod["enabled"]:
            self._mod_manager.set_enabled(mod_id, False)
            self.refresh()
            # Signal the window to handle the apply-then-remove flow
            self.uninstall_requested.emit(mod_id)
        else:
            # Mod was already disabled — just remove from DB directly
            self._mod_manager.remove_mod(mod_id)
            self.refresh()

    # ------------------------------------------------------------------
    # Drag-reorder
    # ------------------------------------------------------------------

    def _on_order_changed(self, mod_ids: list[int]) -> None:
        """Persist the new mod order after a drag-reorder within a folder.

        Rebuilds the GLOBAL priority list from all folders (in display order)
        so that cross-folder ordering is preserved.
        """
        if not self._mod_manager:
            return
        # Build global order: iterate all folder groups in layout order,
        # collect mod_ids from each group's cards in their current order.
        # Null-guards protect against a layout rebuild interleaving with
        # a batch drag-drop — crash reports upstream traced to exactly
        # this iteration path firing during mid-reparent.
        global_order: list[int] = []
        for i in range(self._scroll_layout.count()):
            item = self._scroll_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, FolderGroup):
                for j in range(widget._content_layout.count()):
                    card_item = widget._content_layout.itemAt(j)
                    if card_item is None:
                        continue
                    card = card_item.widget()
                    if isinstance(card, ModCard):
                        global_order.append(card.mod_id)

        if global_order:
            # Append any DB mods not present in the UI to preserve their priorities
            all_db_ids = {m["id"] for m in self._mod_manager.list_mods(mod_type="paz")}
            ui_ids = set(global_order)
            for mid in sorted(all_db_ids - ui_ids):
                global_order.append(mid)
            self._mod_manager.reorder_mods(global_order)
            # Update order labels on all cards
            for i, mid in enumerate(global_order, start=1):
                for card in self._mod_cards:
                    if card.mod_id == mid:
                        card._order_label.setText(f"#{i}")
                        break

            # Conflict winner is priority-derived. After reorder we need to
            # re-detect so the "(conflict)" / "(resolved)" badges — and any
            # red highlighting driven by them — reflect the new load order
            # instead of staying stuck on the pre-drag layout.
            if self._conflict_detector:
                try:
                    self._conflict_detector.detect_all()
                except Exception as e:
                    logger.debug("conflict refresh after reorder failed: %s", e)
            # Queue a page refresh so per-card badges and the conflict
            # tree re-render against the new priority.
            window = self.window()
            if hasattr(window, "_refresh_all"):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(150, window._refresh_all)

    def _on_mod_moved_to_group(self, mod_id: int, group_id) -> None:
        """Persist group change when a mod is dragged to a different group."""
        if not self._db:
            return
        self._db.connection.execute(
            "UPDATE mods SET group_id = ? WHERE id = ?", (group_id, mod_id)
        )
        self._db.connection.commit()

    # ------------------------------------------------------------------
    # Folder group management
    # ------------------------------------------------------------------

    def _load_folder_groups(self) -> list[dict]:
        """Load folder groups from the database."""
        if not self._db:
            return []
        try:
            cursor = self._db.connection.execute(
                "SELECT id, name, sort_order FROM mod_groups ORDER BY sort_order"
            )
            return [{"id": row[0], "name": row[1], "sort_order": row[2]} for row in cursor.fetchall()]
        except Exception:
            return []

    def _on_new_folder(self) -> None:
        """Create a new folder group via dialog."""
        if not self._db:
            return

        from qfluentwidgets import MessageBoxBase, SubtitleLabel, LineEdit

        class NewFolderBox(MessageBoxBase):
            def __init__(self, parent):
                super().__init__(parent)
                self.titleLabel = SubtitleLabel(tr("mod_list.new_folder"))
                self.input = LineEdit()
                self.input.setPlaceholderText(tr("mod_list.folder_name_placeholder"))
                self.viewLayout.addWidget(self.titleLabel)
                self.viewLayout.addWidget(self.input)

        box = NewFolderBox(self.window())
        if box.exec():
            name = box.input.text().strip()
            if not name:
                return
            # Get next sort_order
            cursor = self._db.connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM mod_groups"
            )
            next_order = cursor.fetchone()[0]
            self._db.connection.execute(
                "INSERT INTO mod_groups (name, sort_order) VALUES (?, ?)",
                (name, next_order),
            )
            self._db.connection.commit()
            self.refresh()

    def _on_group_header_menu(self, group_name: str, global_pos) -> None:
        """Show context menu for a folder group header."""
        if group_name == tr("mod_list.ungrouped"):
            return  # No context menu for the default group

        from qfluentwidgets import RoundMenu, Action, FluentIcon

        # Find the group_id
        group_id = None
        for gid, fg in self._folder_groups.items():
            if fg.group_name == group_name and gid is not None:
                group_id = gid
                break
        if group_id is None:
            return

        menu = RoundMenu(parent=self)

        menu.addAction(Action(FluentIcon.EDIT, tr("mods.rename_folder"), triggered=lambda: self._rename_folder(group_id, group_name)))
        menu.addSeparator()
        menu.addAction(Action(FluentIcon.DELETE, "Delete Folder", triggered=lambda: self._delete_folder(group_id)))

        menu.exec(global_pos)

    def _rename_folder(self, group_id: int, current_name: str) -> None:
        """Rename a folder group."""
        if not self._db:
            return

        from qfluentwidgets import MessageBoxBase, SubtitleLabel, LineEdit

        class RenameFolderBox(MessageBoxBase):
            def __init__(self, name, parent):
                super().__init__(parent)
                self.titleLabel = SubtitleLabel(tr("mods.rename_folder"))
                self.input = LineEdit()
                self.input.setText(name)
                self.input.selectAll()
                self.viewLayout.addWidget(self.titleLabel)
                self.viewLayout.addWidget(self.input)

        box = RenameFolderBox(current_name, self.window())
        if box.exec():
            new_name = box.input.text().strip()
            if new_name and new_name != current_name:
                self._db.connection.execute(
                    "UPDATE mod_groups SET name = ? WHERE id = ?",
                    (new_name, group_id),
                )
                self._db.connection.commit()
                self.refresh()

    def _delete_folder(self, group_id: int) -> None:
        """Delete a folder group and move its mods back to Ungrouped."""
        if not self._db:
            return

        from qfluentwidgets import MessageBox

        box = MessageBox(
            "Delete Folder",
            "Delete this folder? All mods in it will be moved to Ungrouped.",
            self.window(),
        )
        if box.exec():
            # Move mods to Ungrouped (group_id = NULL)
            self._db.connection.execute(
                "UPDATE mods SET group_id = NULL WHERE group_id = ?",
                (group_id,),
            )
            # Delete the group
            self._db.connection.execute(
                "DELETE FROM mod_groups WHERE id = ?",
                (group_id,),
            )
            self._db.connection.commit()
            self.refresh()

    def _on_folder_reorder(self, dragged_id: int, target_id: int) -> None:
        """Reorder folders: place dragged folder before the target folder."""
        if not self._db:
            return
        groups = self._load_folder_groups()
        ids = [g["id"] for g in groups]
        if dragged_id not in ids or target_id not in ids:
            return
        # Remove dragged, insert before target
        ids.remove(dragged_id)
        target_pos = ids.index(target_id)
        ids.insert(target_pos, dragged_id)
        # Persist new sort_order
        for order, gid in enumerate(ids):
            self._db.connection.execute(
                "UPDATE mod_groups SET sort_order = ? WHERE id = ?",
                (order, gid),
            )
        self._db.connection.commit()
        self.refresh()

    def _move_mod_to_group(self, mod_id: int, group_id: int | None) -> None:
        """Move a mod to a different folder group."""
        if not self._db:
            return
        self._db.connection.execute(
            "UPDATE mods SET group_id = ? WHERE id = ?",
            (group_id, mod_id),
        )
        self._db.connection.commit()
        self.refresh()

    # ------------------------------------------------------------------
    # Close config panel on outside click (Bug 13)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._config_panel.isVisible():
            panel_rect = self._config_panel.geometry()
            if not panel_rect.contains(event.pos()):
                self._config_panel.close_panel()
        super().mousePressEvent(event)
