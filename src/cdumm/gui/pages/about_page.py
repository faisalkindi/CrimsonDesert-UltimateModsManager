"""About page for CDUMM v3 Fluent window."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    HyperlinkButton,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
)

from cdumm import __version__
from cdumm.i18n import tr

logger = logging.getLogger(__name__)


class _LinkCard(CardWidget):
    """Card with an icon, title, description, and hyperlink button."""

    def __init__(self, icon: FluentIcon, title: str, description: str,
                 url: str, parent=None):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(16)

        # Text section
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        text_layout.setContentsMargins(0, 0, 0, 0)
        title_label = StrongBodyLabel(title, self)
        text_layout.addWidget(title_label)
        desc_label = CaptionLabel(description, self)
        desc_label.setWordWrap(True)
        text_layout.addWidget(desc_label)
        layout.addLayout(text_layout, stretch=1)

        # Link button
        link_btn = HyperlinkButton(url, tr("about.link_open"), self, icon)
        self._link_btn = link_btn
        layout.addWidget(link_btn)

    def retranslate_open(self) -> None:
        """Update the 'Open' button text after a language change."""
        if hasattr(self, "_link_btn"):
            self._link_btn.setText(tr("about.link_open"))


class AboutPage(SmoothScrollArea):
    """About page with app info, links, and license."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AboutPage")
        self.setWidgetResizable(True)

        # Content container
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(36, 20, 36, 20)
        self._layout.setSpacing(12)

        # --- App header card ---
        header_card = CardWidget(self._container)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(24, 20, 24, 20)
        header_layout.setSpacing(20)

        # Logo — to the left of app name (theme-aware)
        self._logo_label = QLabel(header_card)
        self._update_logo()
        header_layout.addWidget(self._logo_label)

        # App info text
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        self._app_title = SubtitleLabel(tr("about.app_title"), header_card)
        info_layout.addWidget(self._app_title)
        self._version_label = StrongBodyLabel(tr("about.version", version=__version__), header_card)
        info_layout.addWidget(self._version_label)

        # Update status — shows "Up to date" by default, changes when update found
        from PySide6.QtGui import QFont as _QF
        self._update_status = StrongBodyLabel(tr("about.up_to_date"), header_card)
        usf = self._update_status.font()
        usf.setPixelSize(13)
        self._update_status.setFont(usf)
        from qfluentwidgets import setCustomStyleSheet
        setCustomStyleSheet(self._update_status,
            "StrongBodyLabel{color:#A3BE8C;}", "StrongBodyLabel{color:#A3BE8C;}")
        info_layout.addWidget(self._update_status)

        tagline = CaptionLabel(
            tr("about.tagline"),
            header_card,
        )
        tagline.setWordWrap(True)
        info_layout.addWidget(tagline)
        header_layout.addLayout(info_layout, stretch=1)

        self._layout.addWidget(header_card)

        # --- Ko-fi Donation Card (prominent, right after header) ---
        self._layout.addSpacing(8)
        self._build_kofi_card()

        # --- Links section ---
        self._layout.addSpacing(8)
        self._links_title = SubtitleLabel(tr("about.links"), self._container)
        self._layout.addWidget(self._links_title)
        self._layout.addSpacing(4)

        self._layout.addWidget(_LinkCard(
            FluentIcon.GITHUB,
            tr("about.github_title"),
            tr("about.github_desc"),
            "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager",
            self._container,
        ))

        self._layout.addWidget(_LinkCard(
            FluentIcon.GLOBE,
            tr("about.nexus_title"),
            tr("about.nexus_desc"),
            "https://www.nexusmods.com/crimsondesert/mods/207",
            self._container,
        ))

        self._layout.addWidget(_LinkCard(
            FluentIcon.FEEDBACK,
            tr("about.bug_title"),
            tr("about.bug_desc"),
            "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/issues/new/choose",
            self._container,
        ))

        # --- License section ---
        self._layout.addSpacing(8)
        self._license_title = SubtitleLabel(tr("about.license"), self._container)
        self._layout.addWidget(self._license_title)
        self._layout.addSpacing(4)

        license_card = CardWidget(self._container)
        lic_layout = QVBoxLayout(license_card)
        lic_layout.setContentsMargins(20, 16, 20, 16)
        lic_layout.setSpacing(4)
        lic_label = BodyLabel(
            tr("about.license_name"), license_card
        )
        lic_layout.addWidget(lic_label)
        lic_desc = CaptionLabel(
            tr("about.license_desc"),
            license_card,
        )
        lic_desc.setWordWrap(True)
        lic_layout.addWidget(lic_desc)
        self._layout.addWidget(license_card)

        # --- Credits ---
        self._layout.addSpacing(8)
        self._credits_title = SubtitleLabel(tr("about.credits"), self._container)
        self._layout.addWidget(self._credits_title)
        self._layout.addSpacing(4)

        credits_card = CardWidget(self._container)
        credits_layout = QVBoxLayout(credits_card)
        credits_layout.setContentsMargins(20, 16, 20, 16)
        credits_layout.setSpacing(4)
        self._credits_dev = BodyLabel(tr("about.credits_dev"), credits_card)
        credits_layout.addWidget(self._credits_dev)
        self._credits_detail = CaptionLabel(tr("about.credits_detail"), credits_card)
        credits_layout.addWidget(self._credits_detail)
        self._layout.addWidget(credits_card)

        self._layout.addStretch()

        self.setWidget(self._container)
        self.enableTransparentBackground()
        self.setScrollAnimation(Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

    def _build_kofi_card(self) -> None:
        """Build a clean Ko-fi donation card with a left accent stripe."""
        from PySide6.QtGui import QFont, QDesktopServices
        from PySide6.QtCore import QUrl
        from qfluentwidgets import setCustomStyleSheet, PrimaryPushButton

        # Outer wrapper with left accent stripe
        card = CardWidget(self._container)
        setCustomStyleSheet(card,
            "CardWidget {"
            "  border: 1px solid #E0E0E0;"
            "  border-left: 4px solid #FF5E5B;"
            "  border-radius: 10px;"
            "}",
            "CardWidget {"
            "  border: 1px solid #3A3E48;"
            "  border-left: 4px solid #FF5E5B;"
            "  border-radius: 10px;"
            "}")

        layout = QHBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # Text
        text_col = QVBoxLayout()
        text_col.setSpacing(4)

        title = StrongBodyLabel(tr("about.enjoying"), card)
        tf = title.font()
        tf.setPixelSize(15)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        text_col.addWidget(title)

        desc = CaptionLabel(tr("about.enjoying_desc"), card)
        desc.setWordWrap(True)
        df = desc.font()
        df.setPixelSize(13)
        desc.setFont(df)
        self._enjoying_desc = desc
        text_col.addWidget(desc)

        layout.addLayout(text_col, 1)

        # Ko-fi button
        kofi_btn = PrimaryPushButton(tr("about.support_kofi"), card)
        kofi_btn.setFixedHeight(36)
        kofi_btn.setMinimumWidth(150)
        bf = kofi_btn.font()
        bf.setPixelSize(13)
        bf.setWeight(QFont.Weight.DemiBold)
        kofi_btn.setFont(bf)
        setCustomStyleSheet(kofi_btn,
            "PrimaryPushButton {"
            "  background: #FF5E5B; color: white; border: none;"
            "  border-radius: 18px; padding-bottom: 4px;"
            "}"
            "PrimaryPushButton:hover { background: #FF4644; }"
            "PrimaryPushButton:pressed { background: #E03E3C; }",
            "PrimaryPushButton {"
            "  background: #FF5E5B; color: white; border: none;"
            "  border-radius: 18px; padding-bottom: 4px;"
            "}"
            "PrimaryPushButton:hover { background: #FF4644; }"
            "PrimaryPushButton:pressed { background: #E03E3C; }")
        kofi_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://ko-fi.com/kindiboy")))
        layout.addWidget(kofi_btn)

        self._layout.addWidget(card)

    def set_managers(self, **kwargs) -> None:
        """Accept engine references (not needed for about, keeps interface uniform)."""
        pass

    def refresh(self) -> None:
        """No-op -- about page is static."""
        pass

    def retranslate_ui(self) -> None:
        """Update text with current translations."""
        self._app_title.setText(tr("about.app_title"))
        self._version_label.setText(tr("about.version", version=__version__))
        self._links_title.setText(tr("about.links"))
        self._license_title.setText(tr("about.license"))
        self._credits_title.setText(tr("about.credits"))
        self._credits_dev.setText(tr("about.credits_dev"))
        self._credits_detail.setText(tr("about.credits_detail"))
        if hasattr(self, "_enjoying_desc"):
            self._enjoying_desc.setText(tr("about.enjoying_desc"))
        # Refresh all LinkCard "Open" buttons
        for card in self._container.findChildren(_LinkCard):
            card.retranslate_open()

    def set_update_status(self, tag: str, url: str, body: str = "") -> None:
        """Update the about page to show an available update."""
        from qfluentwidgets import setCustomStyleSheet, HyperlinkButton

        self._update_status.setText(f"Update available: {tag}")
        setCustomStyleSheet(self._update_status,
            "StrongBodyLabel{color:#EBCB8B;}", "StrongBodyLabel{color:#EBCB8B;}")

        # Add an update card at the top of links section
        update_card = CardWidget(self._container)
        uc_layout = QHBoxLayout(update_card)
        uc_layout.setContentsMargins(20, 14, 20, 14)
        uc_layout.setSpacing(16)

        from PySide6.QtGui import QFont
        info_col = QVBoxLayout()
        info_col.setSpacing(4)
        uc_title = StrongBodyLabel(f"CDUMM {tag} is available!", update_card)
        tf = uc_title.font()
        tf.setPixelSize(15)
        tf.setWeight(QFont.Weight.Bold)
        uc_title.setFont(tf)
        setCustomStyleSheet(uc_title,
            "StrongBodyLabel{color:#2878D0;}", "StrongBodyLabel{color:#5B9BD5;}")
        info_col.addWidget(uc_title)

        if body:
            notes = CaptionLabel(body[:200] + ("..." if len(body) > 200 else ""), update_card)
            notes.setWordWrap(True)
            nf = notes.font()
            nf.setPixelSize(12)
            notes.setFont(nf)
            info_col.addWidget(notes)

        uc_layout.addLayout(info_col, stretch=1)

        dl_btn = HyperlinkButton(url, tr("main.download"), update_card)
        df = dl_btn.font()
        df.setPixelSize(14)
        df.setWeight(QFont.Weight.DemiBold)
        dl_btn.setFont(df)
        uc_layout.addWidget(dl_btn)

        # Insert after header card (index 1 in layout, after header at 0)
        self._layout.insertWidget(1, update_card)

    def _update_logo(self) -> None:
        """Set the logo pixmap based on current theme."""
        from qfluentwidgets import isDarkTheme
        variant = "cdumm-logo-dark.png" if isDarkTheme() else "cdumm-logo-light.png"
        if getattr(sys, "frozen", False):
            p = Path(sys._MEIPASS) / "assets" / variant
        else:
            p = Path(__file__).resolve().parents[4] / "assets" / variant
        if not p.exists():
            p = p.parent / "cdumm-logo.png"
        if p.exists():
            pm = QPixmap(str(p)).scaledToWidth(
                200, Qt.TransformationMode.SmoothTransformation)
            self._logo_label.setPixmap(pm)
            self._logo_label.setFixedSize(pm.size())
