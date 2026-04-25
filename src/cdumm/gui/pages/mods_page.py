"""ModsPage -- main PAZ Mods page for CDUMM v3.

Card-based mod list with summary bar, conflict cards, config panel,
drag-drop import overlay, and search/filter controls.
"""

from __future__ import annotations

import logging
import sys
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
    InfoBar,
    InfoBarPosition,
    PushButton,
    SearchLineEdit,
    SmoothScrollArea,
)

from cdumm.engine.swap_cache import cache_root_for, resolve_cfg_src
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


def _flatten_folder_variants(root, max_depth: int = 4):
    """Walk a multi-level variant tree and return every LEAF variant.

    Leaves are folders that contain game content (found by
    find_folder_variants returning <2 nested variants AND the folder
    itself passing find_folder_variants's content check). Used by the
    cog side panel so nested variant mods like Character Creator
    (Female/{Goblin,Human,Orc}) show as a single flat radio list
    instead of just the top-level Female/Male.

    Returns a list of (leaf_path, relative_label) tuples where the
    label is the path relative to ``root`` with os separators
    normalized to "/".
    """
    from cdumm.gui.preset_picker import find_folder_variants
    results: list[tuple] = []

    def walk(path, depth: int):
        if depth > max_depth:
            return
        children = find_folder_variants(path)
        if len(children) < 2:
            # Leaf: the path itself IS one variant (or not a variant at
            # all). Caller handled the "0 variants" case before calling
            # us, so we only emit when path != root.
            if path != root:
                rel = path.relative_to(root).as_posix()
                results.append((path, rel))
            return
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return results


def _grid_axes_from_leaves(leaves):
    """Return per-level axis lists when the leaves form a regular grid.

    A regular grid means: every leaf rel_path has the same depth, and
    at each level the set of options is identical across every parent
    combination. Character Creator satisfies this — every leaf has
    depth 2, level-0 is always {Female, Male}, level-1 is always
    {Goblin, Human, Orc}.

    Returns list of ordered option lists, one per level
    (e.g. [["Female", "Male"], ["Goblin", "Human", "Orc"]]) or None
    when the tree is irregular and should fall back to flat radio.
    """
    if len(leaves) < 2:
        return None
    parts_list = [rel.split("/") for _, rel in leaves]
    depth = len(parts_list[0])
    if depth < 2 or any(len(p) != depth for p in parts_list):
        return None
    axes: list[list[str]] = []
    for level in range(depth):
        # Option must appear in the SAME set for every prefix combo.
        # Simple check: collect the unique level-values and require the
        # set of (prefix_tuple, level_value) to contain every combo of
        # prefix * level_value.
        level_values: list[str] = []
        seen = set()
        for parts in parts_list:
            v = parts[level]
            if v not in seen:
                seen.add(v)
                level_values.append(v)
        axes.append(level_values)
    # Verify grid: total combos must equal leaf count
    product = 1
    for a in axes:
        product *= len(a)
    if product != len(parts_list):
        return None
    # Verify every leaf's parts are in the axis values (defensive)
    for parts in parts_list:
        for i, v in enumerate(parts):
            if v not in axes[i]:
                return None
    return axes


def _preset_signature(data: dict) -> str | None:
    """Return a stable signature over the `patches` block so two JSON
    presets can be compared even when they have no `name` field. Used
    by the cog side panel to mark the active preset for version-
    variant mods (Even Faster Vanilla Trimmer etc.)."""
    try:
        import hashlib
        import json as _json
        patches = data.get("patches") if isinstance(data, dict) else None
        if patches is None:
            return None
        blob = _json.dumps(patches, sort_keys=True,
                           ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()
    except Exception:
        return None


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
        # Update existing cards. Three-state logic (Codex review
        # finding 1 from the v3.1.8 plan):
        #   - entry present AND has_update=True  -> RED pill (Click To Update)
        #   - entry present AND has_update=False -> GREEN/no pill (confirmed current)
        #   - no entry                           -> GREY/no pill (unknown)
        # Prior code painted RED whenever an entry existed, ignoring
        # has_update. That caused 'mod #17 still says outdated' even
        # after check_mod_updates correctly classified it as current.
        for card in self._mod_cards:
            mod_id = card.mod_id
            nexus_id = nexus_map.get(mod_id)
            if nexus_id and nexus_id in updates:
                u = updates[nexus_id]
                if getattr(u, "has_update", False):
                    card.set_update_available(
                        True, u.mod_url,
                        nexus_mod_id=nexus_id,
                        latest_file_id=getattr(u, "latest_file_id", 0))
                    # Connect update_clicked once per card. Disconnect first
                    # to dedup since Qt 6 raises if the slot isn't connected
                    # — the only legitimate failures here are TypeError
                    # (signature mismatch) and RuntimeError (no connection).
                    try:
                        card.update_clicked.disconnect(self._on_update_clicked)
                    except (TypeError, RuntimeError):
                        pass
                    card.update_clicked.connect(self._on_update_clicked)
                else:
                    # Confirmed current — fill in a missing version label
                    # from the Nexus-reported one before clearing the red
                    # pill. Cards with a real local version stay
                    # untouched.
                    card.fill_missing_version(getattr(u, "latest_version", ""))
                    card.set_update_available(False)
            elif nexus_id:
                card.set_update_available(False)
        # Refresh the summary bar so the Outdated counter reflects the
        # new update set. Without this the count would stay stale until
        # the next refresh().
        self._update_stats()

    def _on_update_clicked(self, mod_id: int, nexus_mod_id: int,
                            file_id: int, fallback_url: str) -> None:
        """Handle a click on the red 'Click To Update' pill.

        For premium users this triggers a direct download (no browser
        round-trip). The Nexus AUP allows this for premium accounts —
        the website handover is only required for free users. If
        ``download_link.json`` returns 403 (free tier), we fall back
        to opening the mod's Files tab, exactly like the old behaviour.

        We delegate to the parent window's ``_handle_direct_update``
        helper since download/import lives there alongside the existing
        nxm:// pipeline.
        """
        win = self.window()
        if hasattr(win, "_handle_direct_update"):
            win._handle_direct_update(mod_id, nexus_mod_id, file_id,
                                       fallback_url)
        elif fallback_url:
            import webbrowser
            webbrowser.open(fallback_url)

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
        # Re-render every red "Click To Update" pill in the new
        # locale (H3 fix). Without this, the pill stays in the old
        # language until the next nexus update check (up to 30 min).
        for card in self._mod_cards:
            if hasattr(card, "retranslate_version"):
                card.retranslate_version()

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

        # Lazy-import the filename parser only when needed below.
        _parse_nexus = None

        for order, mod in enumerate(mods, start=1):
            mod_id = mod["id"]
            nexus_id = mod.get("nexus_mod_id")
            if not nexus_id:
                # Fallback: try to parse it from drop_name on the fly.
                # Mods imported as folders or with non-standard names
                # might not have nexus_mod_id stored even after the
                # one-shot backfill. Without this, those cards stay
                # GREY forever (set_update_available is gated on
                # nexus_id being truthy). One-time DB write so future
                # update checks can find them too.
                drop_name = mod.get("drop_name") or ""
                if drop_name:
                    if _parse_nexus is None:
                        from cdumm.engine.nexus_filename import (
                            parse_nexus_filename as _parse_nexus,
                        )
                    stem = drop_name
                    for _ext in (".zip", ".7z", ".rar", ".json", ".bsdiff"):
                        if stem.lower().endswith(_ext):
                            stem = stem[: -len(_ext)]
                            break
                    nid, _ = _parse_nexus(stem)
                    if nid:
                        nexus_id = nid
                        if self._db:
                            try:
                                self._db.connection.execute(
                                    "UPDATE mods SET nexus_mod_id = ? "
                                    "WHERE id = ? AND (nexus_mod_id IS NULL "
                                    "OR nexus_mod_id = 0)",
                                    (nexus_id, mod_id))
                                self._db.connection.commit()
                            except Exception as e:
                                logger.debug(
                                    "fallback nexus_id write failed: %s", e)
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

    def stream_add_mod(self, mod_id: int) -> None:
        """Append one mod card without rebuilding the whole list.

        Called per-mod during batch import so cards appear as each mod
        finishes. Rebuilding the whole list (via _build_mod_cards) stalls
        the batch worker's stdout pipe. This single-row path reads just
        one mod from the DB (SQLite WAL makes the read lock-free vs the
        worker's writes) and creates one ModCard.
        """
        if not self._mod_manager:
            return
        # Avoid duplicates if streaming fires twice for the same mod
        if any(c._mod_id == mod_id for c in self._mod_cards):
            return

        mod = None
        for m in self._mod_manager.list_mods(mod_type="paz"):
            if m["id"] == mod_id:
                mod = m
                break
        if not mod:
            return

        from datetime import datetime, timedelta
        import re as _re
        from cdumm.gui.fluent_window import _parse_nexus_filename
        _now = datetime.now()

        file_counts = self._mod_manager.get_file_counts()

        is_new = False
        import_date_str = mod.get("import_date")
        if import_date_str:
            try:
                import_dt = datetime.strptime(
                    import_date_str, "%Y-%m-%d %H:%M:%S")
                is_new = (_now - import_dt) < timedelta(hours=2)
            except ValueError:
                pass

        applied = self._applied_state.get(mod_id) is True
        game_status = "active" if applied else "inactive"
        pending = None
        if mod["enabled"] and not applied:
            pending = "Apply to Activate"
        elif not mod["enabled"] and applied:
            pending = "Apply to Deactivate"

        has_config = bool(mod.get("configurable"))
        if not has_config:
            json_src = self._mod_manager.get_json_source(mod_id)
            has_config = json_src is not None

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
            raw = mod.get("version") or ""
            _vm = _re.match(r'(\d+(?:\.\d+)*)', raw)
            display_ver = _vm.group(1) if _vm else raw
        if not display_ver:
            display_ver = "\u2014"
        if len(display_ver) > 7:
            display_ver = display_ver[:6] + "…"

        card = ModCard(
            mod_id=mod_id,
            order=len(self._mod_cards) + 1,
            name=mod["name"],
            author=(mod.get("author") if mod.get("author")
                    and mod.get("author") != "Unknown" else ""),
            version=display_ver,
            status=game_status,
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

        # Route to the mod's folder group (batch imports go to Ungrouped
        # by default, but a custom group_id might already be set).
        gid = mod.get("group_id")
        if gid not in self._folder_groups:
            gid = None
        target_group = self._folder_groups.get(gid)
        if target_group is None:
            # Ungrouped hasn't been created yet — fall back to a full
            # rebuild so the scaffolding exists.
            self._build_mod_cards()
            return
        target_group.add_mod_card(card)
        target_group.set_count(target_group.count() + 1 if hasattr(
            target_group, "count") else len(self._mod_cards))

        # Hide the empty-state hero since we have at least one card now
        if hasattr(self, "_empty_hero"):
            self._empty_hero.hide()

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
        # Count mods that have a NexusMods update available. The lookup
        # is the same one set_nexus_updates uses to color the version
        # pills, just summed here for the summary bar. Must respect
        # has_update — confirmed-current entries (has_update=False)
        # are still in the dict so the GREEN pill knows what version
        # to paint, but they should NOT count as outdated. Counting
        # every dict entry produced "10 outdated" alongside zero red
        # pills.
        outdated = 0
        nexus_updates = getattr(self, "_nexus_updates", None) or {}
        if nexus_updates:
            nexus_map = getattr(self, "_nexus_id_map", {}) or {}
            for c in self._mod_cards:
                nid = nexus_map.get(c.mod_id)
                if nid and nid in nexus_updates and getattr(
                        nexus_updates[nid], "has_update", False):
                    outdated += 1
        self._summary_bar.update_stats(total, active, pending, inactive,
                                       outdated=outdated)

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
                # Enrich each variant with `_has_labels` so the panel can
                # surface a "Configure options..." button. Narrow this
                # to variants with ACTUAL meaningful choices — a mutex
                # group (Unlimited Dragon Flying's five Ride Duration
                # presets) OR a small set of independent toggles (≤15).
                # A flat list of 456 region flags has no meaningful
                # choice beyond "all or none"; the variant's enable
                # toggle already covers that — no point making the user
                # scroll 456 checkboxes.
                try:
                    if self._game_dir is not None:
                        from cdumm.gui.preset_picker import (
                            _detect_mutex_offset_groups)
                        variants_dir = (self._game_dir / "CDMods" / "mods"
                                        / str(mod_id) / "variants")
                        for v in variants_list:
                            fn = v.get("filename")
                            if not fn:
                                continue
                            vpath = variants_dir / fn
                            if not vpath.exists():
                                continue
                            try:
                                vdata = _json.loads(vpath.read_text(
                                    encoding="utf-8"))
                            except Exception:
                                continue
                            # ONLY surface the per-variant Configure
                            # button when there's a real "pick one of N"
                            # decision — a mutex group of changes that
                            # share an offset (Unlimited Dragon Flying's
                            # 5 Ride Duration presets at offset 21860562).
                            # NPC Trust Gain's "Trust Me 10x" / "5x" /
                            # "20x" are themselves the user's choice
                            # (the variant radios above) and their
                            # internal labels are just per-NPC patches —
                            # toggling individual ones doesn't make UX
                            # sense. Same for Region Dismount Removal's
                            # 456 region flags. Mutex-only avoids
                            # offering false choices.
                            if _detect_mutex_offset_groups(vdata):
                                v["_has_labels"] = True
                                v["_json_path"] = str(vpath)
                except Exception as _e:
                    logger.debug("variant label enrichment failed: %s", _e)
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
        self._folder_variant_paths: list[tuple] = []  # (variant_dir, display_name)
        source_path = mod.get("source_path")
        if source_path:
            from pathlib import Path as _Path
            sp = _Path(source_path)
            # Folder-variant archives (e.g. Vaxis LoD): source_path points
            # at the original archive. Re-extract on demand to a temp
            # dir, scan for NNNN-style variant folders, and show them.
            # Apply re-launches the import worker on the chosen variant.
            if sp.exists() and sp.is_file() and sp.suffix.lower() in (
                    ".zip", ".7z", ".rar"):
                try:
                    sp = self._extract_archive_for_cog(sp, mod_id)
                except Exception as e:
                    logger.debug("archive extract for cog failed: %s", e)
                    sp = None
            if sp and sp.exists() and sp.is_dir():
                try:
                    from cdumm.gui.preset_picker import (
                        find_json_presets, find_folder_variants)
                    presets = find_json_presets(sp)
                    leaves = _flatten_folder_variants(sp)
                    # Folder-variant branch — only when there are no JSON
                    # presets (XML-only mods like Vaxis LoD). JSON-preset
                    # mods take priority below.
                    if len(presets) <= 1 and len(leaves) >= 2:
                        # drop_name encodes "<archive>||<variant_rel>" for
                        # variant mods imported via the nested picker. We
                        # parse out the rel path to mark the active leaf.
                        current_drop = mod.get("drop_name") or ""
                        current_rel = ""
                        if "||" in current_drop:
                            current_rel = current_drop.split("||", 1)[1]
                        self._folder_variant_paths = [
                            (lp, rel) for lp, rel in leaves]

                        # Grid detection: if leaves form a clean product
                        # (gender × race for Character Creator), render
                        # as multiple radio groups (one per level) so
                        # the user picks independently per axis.
                        axes = _grid_axes_from_leaves(leaves)
                        if axes is not None:
                            active_parts = (current_rel.split("/")
                                             if current_rel else [])
                            # Heuristic header per level: recognise common
                            # axis names (Gender/Race) else fall back to
                            # "Variant N" so mods with arbitrary axes
                            # (e.g. "Size / Color") still render as
                            # distinct sections rather than two "VARIANT"
                            # headers the user can't distinguish.
                            def _level_header(values: list[str],
                                              level: int) -> str:
                                if any("male" in v.lower() or "female" in v.lower()
                                       for v in values):
                                    return "Gender"
                                if any(v.lower() in ("human", "orc", "goblin", "elf")
                                       for v in values):
                                    return "Race"
                                return f"Variant {level + 1}"
                            self._folder_variant_axes = axes
                            self._folder_variant_is_grid = True
                            variants_meta: list[dict] = []
                            for level, values in enumerate(axes):
                                active_val = (active_parts[level]
                                              if level < len(active_parts)
                                              else None)
                                for v in values:
                                    variants_meta.append({
                                        "label": v.replace("_", " "),
                                        "filename": v,
                                        "enabled": v == active_val,
                                        "group": level,
                                        "_level": level,
                                        "_header": _level_header(values, level),
                                    })
                            self._config_panel.show_variant_mod(
                                mod_id=mod_id,
                                name=mod["name"],
                                author=(mod.get("author")
                                        if mod.get("author")
                                        and mod.get("author") != "Unknown"
                                        else ""),
                                version=mod.get("version") or "",
                                status=status,
                                variants=variants_meta,
                                conflicts=[],
                            )
                            return  # show_variant_mod fires its own animation
                        # Flat folder variants (Vaxis LOD: extra-shadows
                        # vs no-extra-shadows). They're mutually
                        # exclusive — only ONE LOD configuration can be
                        # active at a time. Render as a radio group via
                        # show_variant_mod with `group=0` instead of
                        # building checkbox `patches` (the prior code
                        # used show_mod, letting the user tick both).
                        self._folder_variant_is_grid = False
                        active_i = -1
                        for i, (lp, rel) in enumerate(leaves):
                            if current_rel and rel == current_rel:
                                active_i = i
                                break
                        if active_i < 0 and current_drop:
                            for i, (lp, rel) in enumerate(leaves):
                                if lp.name and lp.name in current_drop:
                                    active_i = i
                                    break
                        # Default to first variant if nothing matches
                        # so the radio group always has a selection
                        # (Qt radios without a default look broken).
                        if active_i < 0:
                            active_i = 0
                        flat_variants_meta: list[dict] = []
                        for i, (lp, rel) in enumerate(leaves):
                            flat_variants_meta.append({
                                "label": rel.replace("/", " > ").replace(
                                    "_", " "),
                                "filename": rel,
                                "description": "folder variant",
                                "enabled": i == active_i,
                                # Same positive group => single radio
                                # set with mutually-exclusive picks.
                                "group": 0,
                            })
                        self._config_panel.show_variant_mod(
                            mod_id=mod_id,
                            name=mod["name"],
                            author=(mod.get("author")
                                    if mod.get("author")
                                    and mod.get("author") != "Unknown"
                                    else ""),
                            version=mod.get("version") or "",
                            status=status,
                            variants=flat_variants_meta,
                            conflicts=[],
                        )
                        return  # show_variant_mod fires its own animation
                    if len(presets) > 1:
                        # Mark which preset is active. Try matching by the
                        # `name` field first (structured mods). Fall back to
                        # a content signature over the patches block when
                        # `name` is absent — bare-bones mods like Even
                        # Faster Vanilla Trimmer have no name/description,
                        # so name-matching always returned False and the
                        # cog wouldn't show which version was picked.
                        current_json = self._mod_manager.get_json_source(mod_id)
                        current_name = ""
                        current_sig = None
                        if current_json:
                            try:
                                import json as _json
                                with open(current_json, "r", encoding="utf-8") as _f:
                                    current_data = _json.load(_f)
                                current_name = current_data.get("name", "") or ""
                                current_sig = _preset_signature(current_data)
                            except Exception:
                                pass

                        self._preset_paths = presets
                        for fp, data in presets:
                            name = data.get("name", fp.stem)
                            desc = data.get("description", "")
                            change_count = sum(len(p.get("changes", [])) for p in data.get("patches", []))
                            if current_name and data.get("name"):
                                is_active = (name == current_name)
                            elif current_sig is not None:
                                is_active = (_preset_signature(data) == current_sig)
                            else:
                                is_active = False
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

        Three flavours:
        - Flat folder variants (Vaxis LoD: extra-shadows vs no-extra-
          shadows). One radio group, pick one leaf, re-import on it.
        - Grid folder variants (Character Creator): selection rows have
          '_level' / '_header' keys. Route to a folder-re-import that
          drops the current mod and relaunches the worker on the new
          leaf path.
        - Legacy JSON multi-variant mods (``mods.variants`` column): fall
          through to update_variant_selection.
        """
        if not self._mod_manager:
            return

        # Flat folder-variant branch — single radio group, pick the
        # enabled leaf, swap. _folder_variant_paths is set by the cog-
        # open code when leaves are flat (no grid axes).
        if (getattr(self, "_folder_variant_paths", None)
                and not getattr(self, "_folder_variant_is_grid", False)
                and selection and not any("_level" in s for s in selection)):
            picked = None
            for s in selection:
                if s.get("enabled") and s.get("group", -1) >= 0:
                    picked = s
                    break
            if picked is None:
                logger.warning("flat folder-variant apply: no leaf picked")
                return
            target_rel = picked.get("filename", "")
            chosen_leaf = None
            for lp, rel in self._folder_variant_paths:
                if rel == target_rel:
                    chosen_leaf = lp
                    break
            if chosen_leaf is None:
                logger.error(
                    "flat folder-variant apply: no leaf matches %r",
                    target_rel)
                return
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
                # Stage out of sources/<id>/ before remove_mod yanks it.
                worker_path = chosen_leaf
                try:
                    from pathlib import Path as _P
                    cl = _P(chosen_leaf)
                    sources_root = (self._game_dir / "CDMods"
                                    / "sources" / str(mod_id)
                                    if self._game_dir else None)
                    if (sources_root and cl.exists()
                            and (sources_root in cl.parents
                                 or cl == sources_root)):
                        from cdumm.engine.temp_workspace import make_temp_dir
                        import shutil as _sh
                        staged_root = make_temp_dir(
                            f"cdumm_swap_{mod_id}_")
                        staged = staged_root / cl.name
                        _sh.copytree(cl, staged)
                        worker_path = staged
                        logger.info(
                            "Flat folder swap: staged %s -> %s "
                            "before remove_mod", cl, staged)
                except Exception as _e:
                    logger.warning(
                        "Flat folder staging failed: %s", _e)
                # cfg_src: where the configurable_scanner will look on
                # next startup to verify the mod still has variants.
                # Priority:
                #   1. Old source_path verbatim if it's the original
                #      ARCHIVE file (rar/zip/7z) — scanner rescues from
                #      the archive and finds all variants. This is the
                #      common case: source_path was set during initial
                #      import to the dropped archive.
                #   2. Clone the FULL old sources/<id>/ to a stable
                #      temp ONLY when source_path was already pointing
                #      INTO sources/ (means the scanner had previously
                #      rescued — sources/<id>/ now contains the full
                #      archive contents, so cloning it captures all
                #      variants).
                #   3. drop_name basename as last-ditch.
                sources_root = (self._game_dir / "CDMods"
                                / "sources" / str(mod_id)
                                if self._game_dir else None)
                cfg_src = resolve_cfg_src(
                    source_path=source_path,
                    sources_dir=sources_root,
                    cache_root=cache_root_for(self._game_dir, mod_id)
                    if self._game_dir else Path(""),
                )
                if not cfg_src:
                    cfg_src = source_path
                if not cfg_src and old_drop_name:
                    cfg_src = old_drop_name.split("||", 1)[0]
                self._mod_manager.remove_mod(mod_id)
                window = self.window()
                window._update_priority = old_priority
                window._update_enabled = old_enabled
                window._configurable_source = cfg_src
                window._variant_leaf_rel = target_rel
                if old_drop_name:
                    raw = old_drop_name.split("||", 1)[0]
                    from pathlib import Path as _P
                    window._original_drop_path = _P(raw)
                window._launch_import_worker(worker_path)
            self._config_panel.close_panel()
            self._folder_variant_paths = []
            return

        # Grid folder-variant branch — pick from each level's enabled
        # radio, combine into a rel_path, find the matching leaf.
        if getattr(self, "_folder_variant_is_grid", False) and \
                selection and any("_level" in s for s in selection):
            picks_by_level: dict[int, str] = {}
            for s in selection:
                if s.get("enabled") and "_level" in s:
                    picks_by_level.setdefault(s["_level"], s["filename"])
            axes = getattr(self, "_folder_variant_axes", None) or []
            if not axes or any(
                    level not in picks_by_level
                    for level in range(len(axes))):
                logger.warning(
                    "variants apply: grid pick incomplete "
                    "(levels=%s picks=%s)", len(axes), picks_by_level)
                return
            target_rel = "/".join(
                picks_by_level[level] for level in range(len(axes)))
            # Find the leaf path matching that rel.
            chosen_leaf = None
            for lp, rel in getattr(self, "_folder_variant_paths", []):
                if rel == target_rel:
                    chosen_leaf = lp
                    break
            if chosen_leaf is None:
                logger.error(
                    "variants apply: no leaf matches rel=%r", target_rel)
                return
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
                # Stage the chosen leaf to a session temp before
                # remove_mod (mod_manager cleans CDMods/sources/<id>/,
                # which yanks the leaf out from under the worker if
                # the leaf lives there). Mirrors the non-grid swap
                # branch in _on_config_apply.
                worker_path = chosen_leaf
                try:
                    from pathlib import Path as _P
                    cl = _P(chosen_leaf)
                    sources_root = (self._game_dir / "CDMods"
                                    / "sources" / str(mod_id)
                                    if self._game_dir else None)
                    if (sources_root and cl.exists()
                            and (sources_root in cl.parents
                                 or cl == sources_root)):
                        from cdumm.engine.temp_workspace import make_temp_dir
                        import shutil as _sh
                        staged_root = make_temp_dir(
                            f"cdumm_swap_{mod_id}_")
                        staged = staged_root / cl.name
                        _sh.copytree(cl, staged)
                        worker_path = staged
                        logger.info(
                            "Grid variant swap: staged %s -> %s "
                            "before remove_mod", cl, staged)
                except Exception as _e:
                    logger.warning(
                        "Grid variant staging failed (%s) — worker "
                        "may not find leaf after remove_mod", _e)
                sources_root = (self._game_dir / "CDMods"
                                / "sources" / str(mod_id)
                                if self._game_dir else None)
                cfg_src = resolve_cfg_src(
                    source_path=source_path,
                    sources_dir=sources_root,
                    cache_root=cache_root_for(self._game_dir, mod_id)
                    if self._game_dir else Path(""),
                )
                if not cfg_src:
                    cfg_src = source_path
                if not cfg_src and old_drop_name:
                    cfg_src = old_drop_name.split("||", 1)[0]
                self._mod_manager.remove_mod(mod_id)
                window = self.window()
                window._update_priority = old_priority
                window._update_enabled = old_enabled
                window._configurable_source = cfg_src
                # Stash the new variant rel so the post-import handler
                # writes "<archive>||<new_rel>" into drop_name.
                window._variant_leaf_rel = target_rel
                if old_drop_name:
                    # Preserve the original archive name for version /
                    # nexus parsing. drop_name may contain '||<rel>';
                    # strip that.
                    raw = old_drop_name.split("||", 1)[0]
                    from pathlib import Path as _P
                    window._original_drop_path = _P(raw)
                window._launch_import_worker(worker_path)
            self._config_panel.close_panel()
            self._folder_variant_paths = []
            self._folder_variant_is_grid = False
            return

        try:
            from cdumm.engine.variant_handler import update_variant_selection
            if self._game_dir is None:
                logger.error("variants apply: game_dir not set")
                return
            mods_dir = self._game_dir / "CDMods" / "mods"
            # Collect any per-variant label selections the user made via
            # the "Configure options..." button on each variant row.
            label_sel = None
            try:
                prev = getattr(self._config_panel,
                               "_variant_label_prev", None)
                dirty = getattr(self._config_panel,
                                "_variant_label_dirty", None)
                if prev and dirty:
                    label_sel = {fn: prev[fn] for fn in dirty if fn in prev}
            except Exception:
                label_sel = None
            update_variant_selection(
                mod_id, selection, mods_dir, self._db,
                label_selections=label_sel)
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

    def _extract_archive_for_cog(self, archive: Path, mod_id: int) -> Path | None:
        """Extract a folder-variant archive to a per-session temp dir so
        the cog panel can scan variants without leaving files on disk.

        Re-uses the same extraction for repeated opens of the same mod's
        cog by keying on mod_id. The temp dir is cleaned up on app exit
        (tempfile default behaviour)."""
        cache = getattr(self, "_folder_variant_extract_cache", None)
        if cache is None:
            cache = {}
            self._folder_variant_extract_cache = cache
        if mod_id in cache:
            cached = cache[mod_id]
            if cached.exists():
                return cached
        from cdumm.engine.temp_workspace import make_temp_dir
        dest = make_temp_dir(f"cdumm_cog_{mod_id}_")
        suffix = archive.suffix.lower()
        if suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        elif suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest)
        elif suffix == ".rar":
            import subprocess
            for tool in ("7z", "7z.exe",
                         r"C:\Program Files\7-Zip\7z.exe"):
                try:
                    subprocess.run(
                        [tool, "x", str(archive), f"-o{dest}", "-y"],
                        capture_output=True, timeout=120,
                        creationflags=getattr(
                            subprocess, "CREATE_NO_WINDOW", 0))
                    break
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
        else:
            import shutil as _sh
            _sh.rmtree(dest, ignore_errors=True)
            return None
        cache[mod_id] = dest
        return dest

    def _on_config_apply(self, mod_id: int, patches: list) -> None:
        """Apply config panel changes — either switch preset or save disabled indices."""
        if not self._mod_manager:
            return

        # Folder-variant swap (Vaxis LoD style XML-only mods). Whichever
        # variant the user enabled in the panel becomes the new active
        # one — we drop the existing mod row and re-launch the import
        # worker on the chosen variant directory, restoring priority +
        # enabled state so the swap is transparent.
        if (hasattr(self, "_folder_variant_paths")
                and self._folder_variant_paths):
            for i, p in enumerate(patches):
                if p.get("enabled") and i < len(self._folder_variant_paths):
                    variant_dir, _name = self._folder_variant_paths[i]
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
                        # When source_path was a directory (already-
                        # extracted folder mod), variant_dir lives INSIDE
                        # CDMods/sources/<mod_id>/. remove_mod deletes
                        # that whole tree (mod_manager.remove_mod cleans
                        # both deltas/ and sources/), which would yank
                        # variant_dir out from under the worker before
                        # it can read. Stage the variant to a session
                        # temp first so the worker gets a stable path.
                        worker_path = variant_dir
                        try:
                            from pathlib import Path as _P
                            vd = _P(variant_dir)
                            sources_root = (self._game_dir / "CDMods"
                                            / "sources" / str(mod_id)
                                            if self._game_dir else None)
                            if (sources_root and vd.exists()
                                    and (sources_root in vd.parents
                                         or vd == sources_root)):
                                from cdumm.engine.temp_workspace import make_temp_dir
                                import shutil as _sh
                                staged_root = make_temp_dir(
                                    f"cdumm_swap_{mod_id}_")
                                staged = staged_root / vd.name
                                _sh.copytree(vd, staged)
                                worker_path = staged
                                logger.info(
                                    "Folder-variant swap: staged %s -> "
                                    "%s before remove_mod",
                                    vd, staged)
                        except Exception as _e:
                            logger.warning(
                                "Folder-variant staging failed (%s) — "
                                "worker may not find variant after "
                                "remove_mod", _e)
                        sources_root = (self._game_dir / "CDMods"
                                        / "sources" / str(mod_id)
                                        if self._game_dir else None)
                        cfg_src = resolve_cfg_src(
                            source_path=source_path,
                            sources_dir=sources_root,
                            cache_root=cache_root_for(
                                self._game_dir, mod_id)
                            if self._game_dir else Path(""),
                        )
                        if not cfg_src:
                            cfg_src = source_path
                        if not cfg_src and old_drop_name:
                            cfg_src = old_drop_name.split("||", 1)[0]
                        self._mod_manager.remove_mod(mod_id)
                        window = self.window()
                        window._update_priority = old_priority
                        window._update_enabled = old_enabled
                        window._configurable_source = cfg_src
                        if old_drop_name:
                            window._original_drop_path = Path(old_drop_name)
                        window._launch_import_worker(worker_path)
                    self._config_panel.close_panel()
                    self._folder_variant_paths = []
                    return
            self._folder_variant_paths = []
            self._config_panel.close_panel()
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
                        # Stage the picked preset to a session temp if
                        # it lives inside sources/<mod_id>/ — remove_mod
                        # would otherwise delete it before the worker
                        # reads.
                        worker_path = fp
                        try:
                            from pathlib import Path as _P
                            fp_path = _P(fp)
                            sources_root = (self._game_dir / "CDMods"
                                            / "sources" / str(mod_id)
                                            if self._game_dir else None)
                            if (sources_root and fp_path.exists()
                                    and (sources_root in fp_path.parents
                                         or fp_path == sources_root)):
                                from cdumm.engine.temp_workspace import make_temp_dir
                                import shutil as _sh
                                staged_root = make_temp_dir(
                                    f"cdumm_preset_{mod_id}_")
                                staged = staged_root / fp_path.name
                                if fp_path.is_file():
                                    _sh.copy2(fp_path, staged)
                                else:
                                    _sh.copytree(fp_path, staged)
                                worker_path = staged
                                logger.info(
                                    "Preset swap: staged %s -> %s "
                                    "before remove_mod",
                                    fp_path, staged)
                        except Exception as _e:
                            logger.warning(
                                "Preset staging failed (%s) — worker "
                                "may not find preset after remove_mod",
                                _e)
                        sources_root = (self._game_dir / "CDMods"
                                        / "sources" / str(mod_id)
                                        if self._game_dir else None)
                        cfg_src = resolve_cfg_src(
                            source_path=source_path,
                            sources_dir=sources_root,
                            cache_root=cache_root_for(
                                self._game_dir, mod_id)
                            if self._game_dir else Path(""),
                        )
                        if not cfg_src:
                            cfg_src = source_path
                        if not cfg_src and old_drop_name:
                            cfg_src = old_drop_name.split("||", 1)[0]
                        self._mod_manager.remove_mod(mod_id)
                        window = self.window()
                        window._update_priority = old_priority
                        window._update_enabled = old_enabled
                        window._configurable_source = cfg_src
                        # Preserve the original archive's NexusMods filename so
                        # the post-import handler can extract version/mod_id
                        # even though the worker only sees the picked JSON.
                        if old_drop_name:
                            window._original_drop_path = Path(old_drop_name)
                        window._launch_import_worker(worker_path)
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
            # Multi-select: batch enable/disable/uninstall/reimport
            menu.addAction(Action(FluentIcon.ACCEPT, f"Enable {len(selected_ids)} mods",
                                  triggered=lambda: self._ctx_batch_toggle(selected_ids, True)))
            menu.addAction(Action(FluentIcon.REMOVE, f"Disable {len(selected_ids)} mods",
                                  triggered=lambda: self._ctx_batch_toggle(selected_ids, False)))
            menu.addSeparator()
            menu.addAction(Action(FluentIcon.SYNC,
                                  f"Reimport {len(selected_ids)} mods from source",
                                  triggered=lambda: self._ctx_batch_reimport(selected_ids)))
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

            # Open source files in Explorer
            menu.addAction(Action(FluentIcon.FOLDER, tr("mod_context.open_source"),
                                  triggered=lambda: self._ctx_open_source(mod_id, mod)))

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

            # Reimport from source (single) — regenerates deltas
            # against current vanilla. Useful after a game update.
            menu.addAction(Action(FluentIcon.SYNC, "Reimport from source",
                                  triggered=lambda: self._ctx_batch_reimport([mod_id])))

            menu.addSeparator()

            # Uninstall
            menu.addAction(Action(FluentIcon.DELETE, "Uninstall", triggered=lambda: self._ctx_uninstall(mod_id)))

        menu.exec(global_pos)

    def _ctx_open_nexus(self, nexus_id: int) -> None:
        import webbrowser
        webbrowser.open(f"https://www.nexusmods.com/crimsondesert/mods/{nexus_id}")

    def _ctx_open_source(self, mod_id: int, mod: dict) -> None:
        """Open the mod's source folder in Windows Explorer.

        Uses os.startfile on the resolved path (Windows default action for a
        directory = open in Explorer). Shows an InfoBar if no folder exists
        or if Windows refuses to open it.
        """
        import os
        from qfluentwidgets import InfoBar, InfoBarPosition

        from cdumm.engine.mod_source_path import resolve_mod_source_path

        if self._game_dir is None:
            return

        path = resolve_mod_source_path(mod, self._game_dir)
        if path is None:
            InfoBar.warning(
                title=tr("mod_context.open_source_not_found_title"),
                content=tr("mod_context.open_source_not_found_body"),
                duration=4000, position=InfoBarPosition.TOP, parent=self.window())
            return

        try:
            os.startfile(str(path))
        except OSError as e:
            logger.warning("Open source files: os.startfile failed for %s: %s", path, e)
            InfoBar.error(
                title=tr("mod_context.open_source_failed_title"),
                content=tr("mod_context.open_source_failed_body", error=str(e)),
                duration=4000, position=InfoBarPosition.TOP, parent=self.window())

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

    def _ctx_batch_reimport(self, mod_ids: list[int],
                              skip_confirm: bool = False) -> None:
        """Reimport each selected mod from its stored source.

        After a game update (Steam patch), every mod's stored delta
        targets the OLD vanilla bytes. Applying them against NEW
        vanilla produces garbage and crashes the game. This action
        regenerates deltas by rerunning import from each mod's
        original zip/folder, preserving the mod row (priority, notes,
        enabled state) via ``existing_mod_id``.

        ``skip_confirm=True`` bypasses the interactive confirm dialog;
        the RecoveryFlow orchestrator sets this so it can drive the
        reimport step without user intervention (v3.2).
        """
        from qfluentwidgets import MessageBox
        from PySide6.QtCore import QProcess
        import json as _json
        import tempfile

        window = self.window()
        if not mod_ids or not self._mod_manager:
            return
        if not getattr(window, "_game_dir", None) or not getattr(window, "_db", None):
            InfoBar.error(
                title="Not ready",
                content="Game directory not set.",
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            return

        # Gather a usable source for each selected mod. Three fallback
        # rules, in order:
        #   1. ``source_path`` IF the folder is non-empty (PAZ-style
        #      mod imported as a folder/archive).
        #   2. ``json_source`` from the DB (JSON-patch mods archive
        #      their original .json into deltas/<id>/source.json; the
        #      source_path folder ends up empty).
        #   3. Skip otherwise.
        # Without rule 2, recovery's reimport pass tries to reimport
        # JSON-patch mods from an empty folder and fails with
        # "Found 0 file(s)" — the user's screenshot showed 17 of those.
        import os
        entries: list[tuple[int, str, str]] = []  # (mod_id, name, source)
        missing: list[str] = []
        for m in self._mod_manager.list_mods():
            if m["id"] not in mod_ids:
                continue
            sp = m.get("source_path") or ""
            chosen: str | None = None
            if sp and os.path.isdir(sp) and os.listdir(sp):
                chosen = sp
            elif sp and os.path.isfile(sp):
                chosen = sp
            else:
                js = m.get("json_source") or ""
                if js and os.path.isfile(js):
                    chosen = js
            if not chosen:
                missing.append(m["name"])
                continue
            entries.append((m["id"], m["name"], chosen))

        if not entries:
            if not skip_confirm:
                MessageBox(
                    "Reimport",
                    "None of the selected mods have a stored source. "
                    "Reimport needs the original zip/folder to regenerate "
                    "patches. Drop the original file back in to fix those.",
                    window).exec()
            return

        if not skip_confirm:
            msg = (f"Reimport {len(entries)} mod(s) from their stored "
                   "sources?\n\n"
                   "This regenerates patches against the current vanilla. "
                   "Use this after a game update when mods stop working.")
            if missing:
                # Show the actual names so users know which mods need
                # manual drag-drop. Cap the inline list so a huge
                # selection doesn't blow up the dialog.
                shown = missing[:15]
                more = len(missing) - len(shown)
                names_block = "\n".join(f"  - {n}" for n in shown)
                if more > 0:
                    names_block += f"\n  - ... and {more} more"
                msg += (f"\n\n{len(missing)} mod(s) can't be reimported "
                        "automatically (no stored source) and will be "
                        "skipped:\n\n"
                        f"{names_block}\n\n"
                        "Drop their original files back in to fix those "
                        "manually.")
            if not MessageBox("Reimport from source", msg, window).exec():
                return

        # Write mod_id\tsource_path lines to a temp file for the
        # worker subprocess.
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False, encoding="utf-8")
        for mid, _name, sp in entries:
            tmp.write(f"{mid}\t{sp}\n")
        tmp.close()

        total = len(entries)
        tip = window._make_state_tooltip(f"Reimporting {total} mod(s)...")
        window._active_progress = tip

        proc = QProcess(window)
        from cdumm.gui.fluent_window import _quiet_qprocess
        _quiet_qprocess(proc)
        window._active_worker = proc

        _buf = [""]
        _errors: list[str] = []
        _succeeded = 0

        def _on_stdout():
            nonlocal _succeeded
            raw = proc.readAllStandardOutput().data().decode(
                "utf-8", errors="replace")
            _buf[0] += raw
            logger.debug(
                "Reimport _on_stdout: read %d chars, buf=%d",
                len(raw), len(_buf[0]))
            while "\n" in _buf[0]:
                line, _buf[0] = _buf[0].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    m = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                t = m.get("type")
                if t == "batch_progress":
                    idx = m.get("index", 0)
                    nm = m.get("name", "")
                    try:
                        tip.setContent(f"({idx + 1}/{total}) {nm}")
                    except RuntimeError:
                        pass
                elif t == "batch_item":
                    if m.get("error"):
                        _errors.append(
                            f"{m.get('name', '?')}: {m['error']}")
                    else:
                        _succeeded += 1
                elif t == "progress":
                    try:
                        tip.setContent(
                            f"({m.get('batch_index', 0) + 1}/{total}) "
                            f"{m.get('msg', '')}")
                    except RuntimeError:
                        pass

        def _on_finished(_code, _status):
            logger.info(
                "Reimport _on_finished fired: code=%s status=%s "
                "succeeded=%d errors=%d", _code, _status,
                _succeeded, len(_errors))
            try:
                tip.setContent(
                    f"Reimported {_succeeded}/{total} mod(s)")
                tip.setState(True)
            except RuntimeError:
                pass
            try:
                proc.deleteLater()
            except Exception as _e:
                logger.warning("Reimport proc.deleteLater failed: %s", _e)
            window._active_worker = None
            window._active_progress = None
            logger.info(
                "Reimport _on_finished: cleared _active_worker, calling _resume_timers")
            if hasattr(window, "_resume_timers"):
                try:
                    window._resume_timers()
                except Exception as _e:
                    logger.warning("Reimport _resume_timers failed: %s", _e)
            if _errors:
                InfoBar.warning(
                    title="Reimport finished with issues",
                    content=(f"{_succeeded} reimported, "
                             f"{len(_errors)} failed:\n\n"
                             + "\n".join(_errors[:10])),
                    duration=-1, position=InfoBarPosition.TOP,
                    parent=window)
            else:
                InfoBar.success(
                    title="Reimport complete",
                    content=(f"{_succeeded} mod(s) reimported. Click "
                             "Apply to use the refreshed patches."),
                    duration=5000, position=InfoBarPosition.TOP,
                    parent=window)
            if hasattr(window, "_refresh_all"):
                window._refresh_all()

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.finished.connect(_on_finished)
        exe = sys.executable
        args = ["--worker", "reimport_batch", tmp.name,
                str(window._game_dir), str(window._db.db_path),
                str(window._deltas_dir)]
        proc.start(exe, args)
        logger.info(
            "Reimport batch started: %d mods, PID=%s",
            total, proc.processId())

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
