"""Splash screen — logo + version on a rounded card, with a dedicated
loading-status line that never overlaps the logo."""

import sys
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap,
)
from PySide6.QtWidgets import QSplashScreen


class _CdummSplash(QSplashScreen):
    """Splash that draws the loading message in a fixed rect below the
    logo (word-wrapped + elided), instead of the default full-widget
    overlay that could land on top of the logo or spill past the edge.
    """

    def __init__(self, pixmap: QPixmap, msg_rect: QRect) -> None:
        super().__init__(pixmap)
        self._msg = ""
        self._msg_rect = msg_rect

    def showMessage(self, message="", alignment=Qt.AlignmentFlag.AlignHCenter,
                    color=None) -> None:  # noqa: N802
        self._msg = (message or "").strip()
        self.repaint()

    def drawContents(self, painter: QPainter) -> None:  # noqa: N802
        if not self._msg:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = painter.font()
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Normal)
        painter.setFont(f)
        painter.setPen(QColor(230, 232, 236, 210))
        # Elide first so a very long message can't blow past two lines.
        fm = QFontMetrics(f)
        text = fm.elidedText(
            self._msg, Qt.TextElideMode.ElideRight, self._msg_rect.width() * 2)
        painter.drawText(
            self._msg_rect,
            (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
             | Qt.TextFlag.TextWordWrap),
            text,
        )
        painter.restore()


def show_splash() -> QSplashScreen:
    """Create and show the splash: a dark rounded card with the logo,
    version, and a reserved status line."""
    from cdumm import __version__

    if getattr(sys, "frozen", False):
        _assets = Path(sys._MEIPASS) / "assets"
    else:
        _assets = Path(__file__).resolve().parents[2] / "assets"
    logo_path = _assets / "cdumm-logo-light.png"
    if not logo_path.exists():
        logo_path = _assets / "cdumm-logo.png"

    W, H = 460, 380
    pixmap = QPixmap(W, H)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.SmoothPixmapTransform
        | QPainter.RenderHint.TextAntialiasing
    )

    # Rounded card background (opaque, so both the logo and the status
    # line sit inside the masked region and stay visible).
    card = QPainterPath()
    card.addRoundedRect(0, 0, W, H, 26, 26)
    painter.fillPath(card, QColor(26, 27, 30, 245))

    # Logo — centred in the upper portion, leaving a clear strip below
    # for the version + status line.
    if logo_path.exists():
        logo = QPixmap(str(logo_path))
        scaled = logo.scaled(
            240, 240,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (W - scaled.width()) // 2
        painter.drawPixmap(x, 34, scaled)

    # Version — fixed line below the logo.
    painter.setPen(QColor(255, 255, 255, 220))
    vf = painter.font()
    vf.setPixelSize(15)
    vf.setWeight(QFont.Weight.DemiBold)
    painter.setFont(vf)
    painter.drawText(
        QRect(0, 296, W, 26),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        f"v{__version__}",
    )
    painter.end()

    # Reserved status-message strip near the bottom, with side margins.
    msg_rect = QRect(28, 330, W - 56, 40)

    splash = _CdummSplash(pixmap, msg_rect)
    splash.setMask(pixmap.mask())
    splash.show()
    return splash
