"""First-time welcome wizard — language, theme, greeting, game folder.

Shown on every launch until the user completes it.
Four steps:
  1. Pick your language (card grid, no 2-letter codes)
  2. Pick your theme (light/dark with preview)
  3. Playful welcome message in chosen language
  4. Game folder detection / manual picker with store icon
"""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QPixmap,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ── Language display names ────────────────────────────────────────────

_LANG_NAMES = {
    "en": "English", "de": "Deutsch", "es": "Espa\u00f1ol",
    "fr": "Fran\u00e7ais", "ko": "\ud55c\uad6d\uc5b4", "pt-BR": "Portugu\u00eas",
    "zh-TW": "\u7e41\u9ad4\u4e2d\u6587", "ar": "\u0627\u0644\u0639\u0631\u0628\u064a\u0629",
    "it": "Italiano", "pl": "Polski", "ru": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
    "tr": "T\u00fcrk\u00e7e", "ja": "\u65e5\u672c\u8a9e", "zh-CN": "\u7b80\u4f53\u4e2d\u6587",
    "uk": "\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430",
    "id": "Indonesia",
}

_WELCOME_MESSAGES = {
    "en": ("Welcome, Adventurer!", "Your mods are about to get organized."),
    "de": ("Willkommen, Abenteurer!", "Deine Mods werden jetzt organisiert."),
    "es": ("\u00a1Bienvenido, Aventurero!", "Tus mods est\u00e1n a punto de organizarse."),
    "fr": ("Bienvenue, Aventurier!", "Vos mods sont sur le point d'\u00eatre organis\u00e9s."),
    "ko": ("\ubaa8\ud5d8\uac00\ub2d8, \ud658\uc601\ud569\ub2c8\ub2e4!", "\ubaa8\ub4dc \uad00\ub9ac\uac00 \uc2dc\uc791\ub429\ub2c8\ub2e4."),
    "pt-BR": ("Bem-vindo, Aventureiro!", "Seus mods v\u00e3o ficar organizados."),
    "zh-TW": ("\u6b61\u8fce\uff0c\u5192\u96aa\u8005\uff01", "\u4f60\u7684\u6a21\u7d44\u5373\u5c07\u88ab\u6574\u7406\u3002"),
    "ar": ("\u0623\u0647\u0644\u0627\u064b \u0628\u0627\u0644\u0645\u063a\u0627\u0645\u0631!", "\u0633\u064a\u062a\u0645 \u062a\u0646\u0638\u064a\u0645 \u062a\u0639\u062f\u064a\u0644\u0627\u062a\u0643."),
    "it": ("Benvenuto, Avventuriero!", "I tuoi mod stanno per essere organizzati."),
    "pl": ("Witaj, Poszukiwaczu Przyg\u00f3d!", "Twoje mody zaraz zostan\u0105 zorganizowane."),
    "ru": ("\u0414\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c!", "\u0412\u0430\u0448\u0438 \u043c\u043e\u0434\u044b \u0441\u043a\u043e\u0440\u043e \u0431\u0443\u0434\u0443\u0442 \u0443\u043f\u043e\u0440\u044f\u0434\u043e\u0447\u0435\u043d\u044b."),
    "tr": ("Ho\u015fgeldin, Maceraci!", "Modlarin d\u00fczenlenecek."),
    "ja": ("\u3088\u3046\u3053\u305d\u3001\u5192\u967a\u8005\uff01", "\u3042\u306a\u305f\u306eMOD\u304c\u6574\u7406\u3055\u308c\u307e\u3059\u3002"),
    "zh-CN": ("\u6b22\u8fce\uff0c\u5192\u96669\u8005\uff01", "\u4f60\u7684\u6a21\u7ec4\u5373\u5c06\u88ab\u6574\u7406\u3002"),
    "uk": ("\u041b\u0430\u0441\u043a\u0430\u0432\u043e \u043f\u0440\u043e\u0441\u0438\u043c\u043e!", "\u0412\u0430\u0448\u0456 \u043c\u043e\u0434\u0438 \u043d\u0435\u0437\u0430\u0431\u0430\u0440\u043e\u043c \u0431\u0443\u0434\u0443\u0442\u044c \u0432\u043f\u043e\u0440\u044f\u0434\u043a\u043e\u0432\u0430\u043d\u0456."),
    "id": ("Selamat Datang, Petualang!", "Mod-mu akan segera tertata rapi."),
}

_STORE_INFO = {
    "steam": {"name": "Steam", "icon": "store-steam.svg", "icon_dark": "store-steam-white.svg"},
    "epic": {"name": "Epic Games", "icon": "store-epic.svg", "icon_dark": "store-epic-white.svg"},
    "xbox": {"name": "Xbox", "icon": "store-xbox.svg", "icon_dark": "store-xbox-white.svg"},
}


# ── Style helpers ─────────────────────────────────────────────────────

def _accent():
    return "#2878D0"

def _bg(dark): return "#0F1117" if dark else "#F5F7FA"
def _card_bg(dark): return "#1A1D27" if dark else "#FFFFFF"
def _card_hover(dark): return "#252A38" if dark else "#EDF2F7"
def _card_selected(dark): return "#1E3A5F" if dark else "#DBEAFE"
def _border(dark): return "#2D3340" if dark else "#E2E8F0"
def _text1(dark): return "#F0F4F8" if dark else "#1A202C"
def _text2(dark): return "#8B95A5" if dark else "#64748B"


# ── Language card (just the name, no code, no border box) ─────────────

class _LangCard(QFrame):
    clicked = Signal(str)

    def __init__(self, code: str, name: str, dark: bool, parent=None):
        super().__init__(parent)
        self._code = code
        self._dark = dark
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(140, 48)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel(name)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self._label.font()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.DemiBold)
        self._label.setFont(f)
        layout.addWidget(self._label)

        self._apply_style()

    def _apply_style(self):
        if self._selected:
            self.setStyleSheet(
                f"_LangCard {{ background: {_card_selected(self._dark)}; "
                f"border: 2px solid {_accent()}; border-radius: 10px; }}")
            self._label.setStyleSheet(f"color: {_accent()}; background: transparent;")
        else:
            self.setStyleSheet(
                f"_LangCard {{ background: {_card_bg(self._dark)}; "
                f"border: 1px solid {_border(self._dark)}; border-radius: 10px; }}"
                f"_LangCard:hover {{ background: {_card_hover(self._dark)}; }}")
            self._label.setStyleSheet(f"color: {_text1(self._dark)}; background: transparent;")

    def set_selected(self, sel: bool):
        self._selected = sel
        self._apply_style()

    def set_dark(self, dark: bool):
        self._dark = dark
        self._apply_style()

    def mousePressEvent(self, ev):
        self.clicked.emit(self._code)


# ── Theme card ────────────────────────────────────────────────────────

class _ThemeCard(QFrame):
    clicked = Signal(str)

    def __init__(self, theme: str, dark_mode: bool, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._dark_mode = dark_mode
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(200, 140)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        # Mini preview
        preview = QFrame()
        preview.setFixedHeight(70)
        if theme == "dark":
            preview.setStyleSheet(
                "QFrame { background: #0F1117; border-radius: 6px; border: 1px solid #2D3340; }")
        else:
            preview.setStyleSheet(
                "QFrame { background: #FAFBFC; border-radius: 6px; border: 1px solid #E2E8F0; }")
        prev_layout = QVBoxLayout(preview)
        prev_layout.setContentsMargins(8, 8, 8, 8)
        prev_layout.setSpacing(4)
        for w in [60, 45, 30]:
            bar = QFrame()
            bar.setFixedHeight(6)
            bar.setFixedWidth(w)
            c = _accent() if w == 60 else ("#2D3340" if theme == "dark" else "#E2E8F0")
            bar.setStyleSheet(f"background: {c}; border-radius: 3px;")
            prev_layout.addWidget(bar)
        prev_layout.addStretch()
        layout.addWidget(preview)

        from cdumm.i18n import tr
        label = QLabel(tr("wizard.dark") if theme == "dark" else tr("wizard.light"))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = label.font()
        f.setPixelSize(15)
        f.setWeight(QFont.Weight.DemiBold)
        label.setFont(f)
        label.setStyleSheet(f"color: {_text1(dark_mode)}; background: transparent;")
        self._name_label = label
        layout.addWidget(label)

        self._apply_style()

    def _apply_style(self):
        if self._selected:
            self.setStyleSheet(
                f"_ThemeCard {{ background: {_card_selected(self._dark_mode)}; "
                f"border: 2px solid {_accent()}; border-radius: 12px; }}")
        else:
            self.setStyleSheet(
                f"_ThemeCard {{ background: {_card_bg(self._dark_mode)}; "
                f"border: 1px solid {_border(self._dark_mode)}; border-radius: 12px; }}"
                f"_ThemeCard:hover {{ background: {_card_hover(self._dark_mode)}; }}")

    def set_selected(self, sel: bool):
        self._selected = sel
        self._apply_style()

    def set_dark(self, dark: bool):
        self._dark_mode = dark
        self._name_label.setStyleSheet(f"color: {_text1(dark)}; background: transparent;")
        self._apply_style()

    def mousePressEvent(self, ev):
        self.clicked.emit(self._theme)


# ── Welcome Wizard ────────────────────────────────────────────────────

class WelcomeWizard(QDialog):
    """Four-step first-time wizard: language, theme, welcome, game folder."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setFixedSize(660, 520)

        self._chosen_lang = "en"
        self._chosen_theme = "light"
        self._game_dir: str | None = None
        self._dark = False
        self._drag_pos = None

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Logo header
        header = QWidget()
        header.setFixedHeight(70)
        hl = QVBoxLayout(header)
        hl.setContentsMargins(0, 16, 0, 0)
        self._logo_label = QLabel()
        self._logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_logo()
        hl.addWidget(self._logo_label)
        main.addWidget(header)

        # Stacked pages
        self._stack = QStackedWidget()
        main.addWidget(self._stack, 1)

        # Bottom nav
        nav = QWidget()
        nav.setFixedHeight(56)
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(30, 0, 30, 16)

        from cdumm.i18n import tr as _tr
        self._back_btn = QPushButton(_tr("wizard.back"))
        self._back_btn.setFixedSize(100, 36)
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._go_back)
        self._back_btn.setVisible(False)
        nav_layout.addWidget(self._back_btn)
        nav_layout.addStretch()

        self._dots = []
        dots_layout = QHBoxLayout()
        dots_layout.setSpacing(8)
        for _ in range(4):
            dot = QFrame()
            dot.setFixedSize(8, 8)
            self._dots.append(dot)
            dots_layout.addWidget(dot)
        nav_layout.addLayout(dots_layout)
        nav_layout.addStretch()

        self._next_btn = QPushButton(_tr("wizard.next"))
        self._next_btn.setFixedSize(100, 36)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._go_next)
        nav_layout.addWidget(self._next_btn)
        main.addWidget(nav)

        self._build_language_page()
        self._build_theme_page()
        self._build_game_folder_page()
        self._build_welcome_page()

        self._update_visuals()

    # ── Logo ──────────────────────────────────────────────────────────

    def _load_logo(self):
        if getattr(sys, "frozen", False):
            assets = Path(sys._MEIPASS) / "assets"
        else:
            assets = Path(__file__).resolve().parents[2] / "assets"
        variant = "cdumm-logo-dark.png" if self._dark else "cdumm-logo-light.png"
        logo_path = assets / variant
        if not logo_path.exists():
            logo_path = assets / "cdumm-logo.png"
        if logo_path.exists():
            pm = QPixmap(str(logo_path)).scaled(
                180, 45, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._logo_label.setPixmap(pm)

    # ── Page 1: Language ──────────────────────────────────────────────

    def _build_language_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 10, 30, 10)
        layout.setSpacing(14)

        from cdumm.i18n import tr as _tr
        self._p1_title = QLabel(_tr("wizard.choose_language"))
        self._p1_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self._p1_title.font()
        f.setPixelSize(22)
        f.setWeight(QFont.Weight.Bold)
        self._p1_title.setFont(f)
        layout.addWidget(self._p1_title)

        self._p1_sub = QLabel(_tr("wizard.change_later"))
        self._p1_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sf = self._p1_sub.font()
        sf.setPixelSize(12)
        self._p1_sub.setFont(sf)
        layout.addWidget(self._p1_sub)
        layout.addSpacing(6)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._lang_cards: dict[str, _LangCard] = {}
        for i, (code, name) in enumerate(_LANG_NAMES.items()):
            card = _LangCard(code, name, dark=self._dark)
            card.clicked.connect(self._on_lang_selected)
            grid.addWidget(card, i // 4, i % 4)
            self._lang_cards[code] = card
        self._lang_cards["en"].set_selected(True)

        layout.addLayout(grid)
        layout.addStretch()
        self._stack.addWidget(page)

    def _on_lang_selected(self, code: str):
        for c, card in self._lang_cards.items():
            card.set_selected(c == code)
        self._chosen_lang = code
        # Reload i18n and refresh all wizard text
        from cdumm.i18n import load as load_i18n
        load_i18n(code)
        self._refresh_texts()

    # ── Page 2: Theme ─────────────────────────────────────────────────

    def _build_theme_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(20)

        from cdumm.i18n import tr as _tr
        self._p2_title = QLabel(_tr("wizard.pick_theme"))
        self._p2_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self._p2_title.font()
        f.setPixelSize(22)
        f.setWeight(QFont.Weight.Bold)
        self._p2_title.setFont(f)
        layout.addWidget(self._p2_title)

        self._p2_sub = QLabel(_tr("wizard.theme_hint"))
        self._p2_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sf = self._p2_sub.font()
        sf.setPixelSize(12)
        self._p2_sub.setFont(sf)
        layout.addWidget(self._p2_sub)
        layout.addStretch()

        row = QHBoxLayout()
        row.setSpacing(24)
        row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._theme_cards: dict[str, _ThemeCard] = {}
        for theme in ["light", "dark"]:
            card = _ThemeCard(theme, dark_mode=self._dark)
            card.clicked.connect(self._on_theme_selected)
            row.addWidget(card)
            self._theme_cards[theme] = card
        self._theme_cards["light"].set_selected(True)
        layout.addLayout(row)
        layout.addStretch()
        self._stack.addWidget(page)

    def _on_theme_selected(self, theme: str):
        for t, card in self._theme_cards.items():
            card.set_selected(t == theme)
        self._chosen_theme = theme
        self._dark = (theme == "dark")
        self._update_visuals()

    # ── Page 3: Welcome ───────────────────────────────────────────────

    def _build_welcome_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)
        layout.setSpacing(12)
        layout.addStretch()

        self._w_title = QLabel("")
        self._w_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._w_title.setWordWrap(True)
        f = self._w_title.font()
        f.setPixelSize(32)
        f.setWeight(QFont.Weight.Bold)
        self._w_title.setFont(f)
        layout.addWidget(self._w_title)

        self._w_sub = QLabel("")
        self._w_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._w_sub.setWordWrap(True)
        sf = self._w_sub.font()
        sf.setPixelSize(16)
        self._w_sub.setFont(sf)
        layout.addWidget(self._w_sub)

        layout.addStretch()
        self._stack.addWidget(page)

    def _animate_welcome(self):
        title, sub = _WELCOME_MESSAGES.get(self._chosen_lang, _WELCOME_MESSAGES["en"])
        self._w_title.setText(title)
        self._w_title.setStyleSheet(f"color: {_accent()};")
        self._w_sub.setText(sub)
        self._w_sub.setStyleSheet(f"color: {_text1(self._dark)};")
        for widget, delay in [(self._w_title, 0), (self._w_sub, 300)]:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(0.0)
            widget.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity")
            anim.setDuration(500)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            widget._fade_anim = anim
            QTimer.singleShot(delay, anim.start)

    # ── Page 3: Game Folder ─────────────────────────────────────────────

    def _build_game_folder_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 20, 40, 10)
        layout.setSpacing(0)

        from cdumm.i18n import tr as _tr
        self._p4_title = QLabel(_tr("wizard.find_game"))
        self._p4_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self._p4_title.font()
        f.setPixelSize(22)
        f.setWeight(QFont.Weight.Bold)
        self._p4_title.setFont(f)
        layout.addWidget(self._p4_title)
        layout.addSpacing(6)

        self._p4_sub = QLabel("")
        self._p4_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sf = self._p4_sub.font()
        sf.setPixelSize(12)
        self._p4_sub.setFont(sf)
        layout.addWidget(self._p4_sub)

        layout.addStretch()

        # ── Found card (shown when auto-detected) ────────────────────
        self._found_card = QFrame()
        self._found_card.setObjectName("foundCard")
        self._found_card.setFixedHeight(100)
        fc_layout = QVBoxLayout(self._found_card)
        fc_layout.setContentsMargins(20, 16, 20, 16)
        fc_layout.setSpacing(8)

        # Icon + store name row
        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        self._store_icon_label = QLabel()
        self._store_icon_label.setFixedSize(36, 36)
        self._store_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(self._store_icon_label)

        self._store_text = QLabel("")
        stf = self._store_text.font()
        stf.setPixelSize(16)
        stf.setWeight(QFont.Weight.DemiBold)
        self._store_text.setFont(stf)
        top_row.addWidget(self._store_text)
        top_row.addStretch()

        # Green checkmark
        self._check_label = QLabel("\u2713")
        ckf = self._check_label.font()
        ckf.setPixelSize(20)
        ckf.setWeight(QFont.Weight.Bold)
        self._check_label.setFont(ckf)
        self._check_label.setStyleSheet("color: #16A34A;")
        top_row.addWidget(self._check_label)
        fc_layout.addLayout(top_row)

        # Path below
        self._detected_path = QLabel("")
        self._detected_path.setWordWrap(True)
        pf = self._detected_path.font()
        pf.setPixelSize(11)
        self._detected_path.setFont(pf)
        fc_layout.addWidget(self._detected_path)

        self._found_card.setVisible(False)
        layout.addWidget(self._found_card)

        layout.addSpacing(16)

        # ── Manual browse (shown when NOT found, or as fallback) ─────
        self._manual_section = QWidget()
        ml = QVBoxLayout(self._manual_section)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(8)

        self._manual_sep = QLabel("")
        self._manual_sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sepf = self._manual_sep.font()
        sepf.setPixelSize(11)
        self._manual_sep.setFont(sepf)
        ml.addWidget(self._manual_sep)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(_tr("wizard.placeholder"))
        self._path_edit.setFixedHeight(38)
        self._path_edit.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self._path_edit)

        browse_btn = QPushButton(_tr("wizard.browse"))
        browse_btn.setFixedSize(80, 38)
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.clicked.connect(self._on_browse)
        self._browse_btn = browse_btn
        path_row.addWidget(browse_btn)
        ml.addLayout(path_row)

        self._path_status = QLabel("")
        psf = self._path_status.font()
        psf.setPixelSize(11)
        self._path_status.setFont(psf)
        ml.addWidget(self._path_status)

        layout.addWidget(self._manual_section)
        layout.addStretch()
        self._stack.addWidget(page)

    def _detect_game(self):
        """Run game detection and update the UI."""
        from cdumm.storage.game_finder import (
            find_game_directories, validate_game_directory,
            is_steam_install, is_epic_install, is_xbox_install,
        )
        from cdumm.i18n import tr
        candidates = find_game_directories()
        if candidates:
            game_path = candidates[0]
            self._game_dir = str(game_path)

            if is_steam_install(game_path):
                store = "steam"
            elif is_epic_install(game_path):
                store = "epic"
            elif is_xbox_install(game_path):
                store = "xbox"
            else:
                store = None

            # Populate the found card
            if store and store in _STORE_INFO:
                info = _STORE_INFO[store]
                icon_name = info["icon_dark"] if self._dark else info["icon"]
                if getattr(sys, "frozen", False):
                    icon_path = Path(sys._MEIPASS) / "assets" / icon_name
                else:
                    icon_path = Path(__file__).resolve().parents[2] / "assets" / icon_name
                if icon_path.exists():
                    pm = QPixmap(str(icon_path)).scaled(
                        36, 36, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    self._store_icon_label.setPixmap(pm)
                self._store_text.setText(tr("wizard.found_on", store=info["name"]))
                self._store_text.setStyleSheet(f"color: {_text1(self._dark)};")

            self._detected_path.setText(str(game_path))
            self._detected_path.setStyleSheet(f"color: {_text2(self._dark)};")

            # Style the found card — white/gray with green left accent stripe
            card_bg = "#1A1D27" if self._dark else "#FFFFFF"
            card_border = "#2D3340" if self._dark else "#E2E8F0"
            self._found_card.setStyleSheet(
                f"#foundCard {{ background: {card_bg}; "
                f"border: 1px solid {card_border}; border-left: 4px solid #16A34A; "
                f"border-radius: 12px; }}"
                f"#foundCard QLabel {{ border: none; background: transparent; }}")
            self._found_card.setVisible(True)

            # Update subtitle and hide manual section
            self._p4_sub.setText(tr("wizard.auto_found"))
            self._p4_sub.setStyleSheet(f"color: #16A34A;")
            self._manual_section.setVisible(False)
            self._next_btn.setEnabled(True)

        else:
            # Not found — show manual picker
            self._found_card.setVisible(False)
            self._p4_sub.setText(tr("wizard.not_found"))
            self._p4_sub.setStyleSheet(f"color: #DC2626;")
            self._manual_sep.setText(tr("wizard.or_manual"))
            self._manual_sep.setStyleSheet(f"color: {_text2(self._dark)};")
            self._manual_section.setVisible(True)
            self._next_btn.setEnabled(False)

    def _on_path_changed(self, text: str):
        from cdumm.storage.game_finder import validate_game_directory
        from cdumm.i18n import tr
        path = Path(text)
        if validate_game_directory(path):
            self._game_dir = text
            self._path_status.setText(tr("wizard.valid_install"))
            self._path_status.setStyleSheet("color: #16A34A;")
            self._next_btn.setEnabled(True)
        else:
            self._game_dir = None
            self._next_btn.setEnabled(False)
            if text:
                self._path_status.setText(tr("wizard.invalid_path"))
                self._path_status.setStyleSheet("color: #DC2626;")
            else:
                self._path_status.setText("")

    def _on_browse(self):
        from cdumm.i18n import tr as _tr
        folder = QFileDialog.getExistingDirectory(self, _tr("setup.browse_dialog_title"))
        if folder:
            self._path_edit.setText(folder)

    # ── Navigation ────────────────────────────────────────────────────

    def _update_nav(self):
        idx = self._stack.currentIndex()
        from cdumm.i18n import tr
        self._back_btn.setVisible(idx > 0)
        self._back_btn.setText(tr("wizard.back"))
        if idx == 3:
            self._next_btn.setText(tr("wizard.lets_go"))
        else:
            self._next_btn.setText(tr("wizard.next"))
        # Disable next on game page (now page 2) if no game found
        if idx == 2:
            self._next_btn.setEnabled(self._game_dir is not None)
        else:
            self._next_btn.setEnabled(True)
        for i, dot in enumerate(self._dots):
            dot.setStyleSheet(
                f"background: {_accent() if i == idx else (_border(self._dark))}; "
                f"border-radius: 4px;")

    def _go_next(self):
        idx = self._stack.currentIndex()
        if idx < 3:
            self._stack.setCurrentIndex(idx + 1)
            self._update_nav()
            self._update_button_styles()
            if idx + 1 == 2:
                self._detect_game()
            if idx + 1 == 3:
                self._animate_welcome()
        else:
            self.accept()

    def _go_back(self):
        idx = self._stack.currentIndex()
        if idx > 0:
            self._stack.setCurrentIndex(idx - 1)
            self._update_nav()
            self._update_button_styles()

    # ── Visual refresh (called on theme change) ───────────────────────

    def _update_visuals(self):
        bg = _bg(self._dark)
        # Use palette for background to avoid setStyleSheet repositioning bug
        from PySide6.QtGui import QPalette
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(bg))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self._load_logo()
        self._update_button_styles()
        self._update_nav()
        # Update all text colors
        for lbl in [self._p1_title, self._p2_title]:
            lbl.setStyleSheet(f"color: {_text1(self._dark)};")
        for lbl in [self._p1_sub, self._p2_sub]:
            lbl.setStyleSheet(f"color: {_text2(self._dark)};")
        # Update cards
        for card in self._lang_cards.values():
            card.set_dark(self._dark)
        for card in self._theme_cards.values():
            card.set_dark(self._dark)
        # Path edit
        if hasattr(self, '_path_edit'):
            if self._dark:
                self._path_edit.setStyleSheet(
                    "QLineEdit { background: #1C2028; color: #E2E8F0; "
                    "border: 1px solid #2D3340; border-radius: 8px; padding: 0 10px; }")
            else:
                self._path_edit.setStyleSheet(
                    "QLineEdit { background: #FFFFFF; color: #1A202C; "
                    "border: 1px solid #E2E8F0; border-radius: 8px; padding: 0 10px; }")
        if hasattr(self, '_manual_sep'):
            self._manual_sep.setStyleSheet(f"color: {_text2(self._dark)};")
        if hasattr(self, '_p4_title'):
            self._p4_title.setStyleSheet(f"color: {_text1(self._dark)};")
        if hasattr(self, '_p4_sub'):
            self._p4_sub.setStyleSheet(f"color: {_text2(self._dark)};")

    def _update_button_styles(self):
        self._next_btn.setStyleSheet(
            f"QPushButton {{ background: {_accent()}; color: white; "
            f"border: none; border-radius: 8px; font-weight: 600; font-size: 13px; }}"
            f"QPushButton:hover {{ background: #3088E0; }}"
            f"QPushButton:disabled {{ background: #555; color: #999; }}")
        bg = "#1A1D27" if self._dark else "#F0F0F0"
        fg = "#8B95A5" if self._dark else "#64748B"
        self._back_btn.setStyleSheet(
            f"QPushButton {{ background: {bg}; color: {fg}; "
            f"border: none; border-radius: 8px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {'#252A38' if self._dark else '#E2E8F0'}; }}")
        if hasattr(self, '_browse_btn'):
            self._browse_btn.setStyleSheet(
                f"QPushButton {{ background: {bg}; color: {fg}; "
                f"border: 1px solid {_border(self._dark)}; border-radius: 8px; font-size: 12px; }}"
                f"QPushButton:hover {{ background: {'#252A38' if self._dark else '#E2E8F0'}; }}")

    # ── Refresh all text for language change ─────────────────────────

    def _refresh_texts(self):
        from cdumm.i18n import tr
        self._p1_title.setText(tr("wizard.choose_language"))
        self._p1_sub.setText(tr("wizard.change_later"))
        self._p2_title.setText(tr("wizard.pick_theme"))
        self._p2_sub.setText(tr("wizard.theme_hint"))
        self._p4_title.setText(tr("wizard.find_game"))
        self._p4_sub.setText(tr("wizard.looking"))
        self._manual_sep.setText(tr("wizard.or_manual"))
        self._path_edit.setPlaceholderText(tr("wizard.placeholder"))
        self._browse_btn.setText(tr("wizard.browse"))
        idx = self._stack.currentIndex()
        if idx == 3:
            self._next_btn.setText(tr("wizard.lets_go"))
        else:
            self._next_btn.setText(tr("wizard.next"))
        self._back_btn.setText(tr("wizard.back"))

    # ── Prevent close without completing ──────────────────────────────

    def closeEvent(self, event):
        event.ignore()  # Block ALT+F4 — must complete wizard

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            return  # Block Escape too
        super().keyPressEvent(event)

    # ── Drag to move (frameless) ──────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Only enable drag from the header area (top 70px), not from cards/buttons
            if event.position().y() < 70:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            else:
                self._drag_pos = None

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    # ── Results ───────────────────────────────────────────────────────

    @property
    def chosen_language(self) -> str:
        return self._chosen_lang

    @property
    def chosen_theme(self) -> str:
        return self._chosen_theme

    @property
    def game_directory(self) -> str | None:
        return self._game_dir
