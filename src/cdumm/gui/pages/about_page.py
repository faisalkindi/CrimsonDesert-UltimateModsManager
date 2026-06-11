"""About page for CDUMM v3 Fluent window."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
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
    """Card with an icon, title, description, and hyperlink button.

    #184 (devCKVargas): the whole row is a hit target now, not just
    the small Open button on the right. Click anywhere on the card
    and the URL opens in the default browser. The Open button is kept
    visible for discoverability and continues to work as before.
    """

    def __init__(self, icon: FluentIcon, title_key: str, desc_key: str,
                 url: str, parent=None):
        super().__init__(parent)

        self._url = url
        self._title_key = title_key
        self._desc_key = desc_key
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(16)

        # Text section
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        text_layout.setContentsMargins(0, 0, 0, 0)
        title_label = StrongBodyLabel(tr(title_key), self)
        self._title_label = title_label
        text_layout.addWidget(title_label)
        desc_label = CaptionLabel(tr(desc_key), self)
        desc_label.setWordWrap(True)
        self._desc_label = desc_label
        text_layout.addWidget(desc_label)
        layout.addLayout(text_layout, stretch=1)

        # Link button
        link_btn = HyperlinkButton(url, tr("about.link_open"), self, icon)
        self._link_btn = link_btn
        layout.addWidget(link_btn)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl(self._url))
            event.accept()
            return
        super().mousePressEvent(event)

    def retranslate_open(self) -> None:
        """Update title, description, and 'Open' button text after a
        language change."""
        if hasattr(self, "_link_btn"):
            self._link_btn.setText(tr("about.link_open"))
        if hasattr(self, "_title_label"):
            self._title_label.setText(tr(self._title_key))
        if hasattr(self, "_desc_label"):
            self._desc_label.setText(tr(self._desc_key))


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
        # ``_update_tag`` tracks which state the label is in so a
        # language switch can re-render the right text (None = up to date).
        self._update_tag: str | None = None
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
        self._tagline = tagline
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
            "about.github_title",
            "about.github_desc",
            "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager",
            self._container,
        ))

        self._layout.addWidget(_LinkCard(
            FluentIcon.GLOBE,
            "about.nexus_title",
            "about.nexus_desc",
            "https://www.nexusmods.com/crimsondesert/mods/207",
            self._container,
        ))

        self._layout.addWidget(_LinkCard(
            FluentIcon.FEEDBACK,
            "about.bug_title",
            "about.bug_desc",
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
        self._license_name = lic_label
        lic_layout.addWidget(lic_label)
        lic_desc = CaptionLabel(
            tr("about.license_desc"),
            license_card,
        )
        lic_desc.setWordWrap(True)
        self._license_desc = lic_desc
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
        self._kofi_title = title
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
        self._kofi_btn = kofi_btn
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
        self._tagline.setText(tr("about.tagline"))
        # Update status is state-aware: re-render the same state in the
        # new language instead of resetting to "up to date".
        if self._update_tag:
            self._update_status.setText(
                tr("about.update_available", tag=self._update_tag))
        else:
            self._update_status.setText(tr("about.up_to_date"))
        self._links_title.setText(tr("about.links"))
        self._license_title.setText(tr("about.license"))
        self._license_name.setText(tr("about.license_name"))
        self._license_desc.setText(tr("about.license_desc"))
        self._credits_title.setText(tr("about.credits"))
        self._credits_dev.setText(tr("about.credits_dev"))
        self._credits_detail.setText(tr("about.credits_detail"))
        if hasattr(self, "_kofi_title"):
            self._kofi_title.setText(tr("about.enjoying"))
        if hasattr(self, "_kofi_btn"):
            self._kofi_btn.setText(tr("about.support_kofi"))
        if hasattr(self, "_enjoying_desc"):
            self._enjoying_desc.setText(tr("about.enjoying_desc"))
        # Refresh all LinkCard titles, descriptions, and "Open" buttons
        for card in self._container.findChildren(_LinkCard):
            card.retranslate_open()

    def set_update_status(self, tag: str, url: str, body: str = "") -> None:
        """Update the about page to show an available update."""
        from qfluentwidgets import setCustomStyleSheet, HyperlinkButton

        self._update_tag = tag
        self._update_status.setText(tr("about.update_available", tag=tag))
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
