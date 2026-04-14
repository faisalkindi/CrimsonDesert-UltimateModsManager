"""Splash screen — big logo with transparent background."""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen


def show_splash() -> QSplashScreen:
    """Create and show a transparent splash screen with the CDUMM logo."""
    from cdumm import __version__

    # Find logo
    if getattr(sys, "frozen", False):
        logo_path = Path(sys._MEIPASS) / "assets" / "cdumm-logo.png"
    else:
        logo_path = Path(__file__).resolve().parents[2] / "assets" / "cdumm-logo.png"

    # Canvas size
    W, H = 500, 420

    pixmap = QPixmap(W, H)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.SmoothPixmapTransform
        | QPainter.RenderHint.TextAntialiasing
    )

    # Draw logo centered, big
    if logo_path.exists():
        logo = QPixmap(str(logo_path))
        # Scale logo to fill most of the width
        scaled = logo.scaled(
            380, 280,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (W - scaled.width()) // 2
        y = 20
        painter.drawPixmap(x, y, scaled)

    # Version text below logo
    painter.setPen(QColor(255, 255, 255, 200))
    font = painter.font()
    font.setPixelSize(14)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(
        0, 310, W, 30,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        f"v{__version__}",
    )

    # Loading status area (bottom)
    painter.setPen(QColor(255, 255, 255, 120))
    font.setPixelSize(12)
    font.setWeight(QFont.Weight.Normal)
    painter.setFont(font)
    painter.drawText(
        0, H - 40, W, 30,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        "Loading...",
    )
    painter.end()

    splash = QSplashScreen(pixmap)
    splash.setMask(pixmap.mask())
    splash.show()
    return splash
