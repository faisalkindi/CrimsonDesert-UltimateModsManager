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
        link_btn = HyperlinkButton(url, "Open", self, icon)
        layout.addWidget(link_btn)


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

        # Logo — to the left of app name
        logo_label = QLabel(header_card)
        logo_path = self._find_logo()
        if logo_path:
            pm = QPixmap(logo_path).scaledToWidth(
                200, Qt.TransformationMode.SmoothTransformation,
            )
            logo_label.setPixmap(pm)
            logo_label.setFixedSize(pm.size())
        header_layout.addWidget(logo_label)

        # App info text
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        self._app_title = SubtitleLabel(tr("about.app_title"), header_card)
        info_layout.addWidget(self._app_title)
        self._version_label = StrongBodyLabel(tr("about.version", version=__version__), header_card)
        info_layout.addWidget(self._version_label)
        tagline = CaptionLabel(
            tr("about.tagline"),
            header_card,
        )
        tagline.setWordWrap(True)
        info_layout.addWidget(tagline)
        header_layout.addLayout(info_layout, stretch=1)

        self._layout.addWidget(header_card)

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

    @staticmethod
    def _find_logo() -> str | None:
        """Locate the CDUMM logo image."""
        if getattr(sys, "frozen", False):
            p = Path(sys._MEIPASS) / "assets" / "cdumm-logo.png"
        else:
            p = Path(__file__).resolve().parents[4] / "assets" / "cdumm-logo.png"
        return str(p) if p.exists() else None
