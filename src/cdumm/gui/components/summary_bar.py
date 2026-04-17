"""SummaryBar — horizontal stats bar with action buttons."""

from PySide6.QtCore import QEasingCurve, QTimeLine, Qt, Signal
from PySide6.QtGui import QFont, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget, QStyle, QStyleOptionButton, QStylePainter

from qfluentwidgets import (
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    isDarkTheme,
)

from cdumm.i18n import tr


class _CenteredPushButton(PushButton):
    """PushButton that visually centers text using font metrics."""
    def paintEvent(self, event):
        qp = QStylePainter(self)
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        text = opt.text
        opt.text = ''
        qp.drawControl(QStyle.ControlElement.CE_PushButton, opt)
        fm = self.fontMetrics()
        path = QPainterPath()
        path.addText(0, 0, self.font(), text.replace('&', ''))
        font_center = fm.ascent() - fm.height() / 2
        text_center = path.boundingRect().center().y()
        qp.translate(0, -(font_center + text_center))
        opt.text = text
        qp.drawControl(QStyle.ControlElement.CE_PushButtonLabel, opt)


class _CenteredPrimaryButton(PrimaryPushButton):
    """PrimaryPushButton that visually centers text using font metrics."""
    def paintEvent(self, event):
        qp = QStylePainter(self)
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        text = opt.text
        opt.text = ''
        qp.drawControl(QStyle.ControlElement.CE_PushButton, opt)
        fm = self.fontMetrics()
        path = QPainterPath()
        path.addText(0, 0, self.font(), text.replace('&', ''))
        font_center = fm.ascent() - fm.height() / 2
        text_center = path.boundingRect().center().y()
        qp.translate(0, -(font_center + text_center))
        opt.text = text
        qp.drawControl(QStyle.ControlElement.CE_PushButtonLabel, opt)


class SummaryBar(QWidget):
    """Horizontal bar showing mod statistics and action buttons.

    Signals
    -------
    apply_clicked
        Emitted when the "Apply Changes" button is pressed.
    launch_clicked
        Emitted when the "Launch Game" button is pressed.
    """

    apply_clicked = Signal()
    revert_clicked = Signal()
    launch_clicked = Signal()

    # (tr_key, dot color)
    _STAT_DEFS = [
        ("stats.total", "#2878D0"),
        ("stats.active", "#22C55E"),
        ("stats.pending", "#E65100"),
        ("stats.inactive", "#9CA3AF"),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SummaryBar")
        self.setFixedHeight(60)
        self._apply_bar_style()

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 0, 24, 0)
        root.setSpacing(20)

        # -- Stat items --
        self._number_labels: list[StrongBodyLabel] = []
        self._caption_labels: list[CaptionLabel] = []

        for tr_key, dot_color in self._STAT_DEFS:
            item = QHBoxLayout()
            item.setSpacing(6)
            item.setContentsMargins(0, 0, 0, 0)

            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(
                f"background: {dot_color}; border-radius: 5px; border: none;"
            )
            item.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

            number = StrongBodyLabel("0")
            nf = number.font()
            nf.setPixelSize(26)
            nf.setWeight(QFont.Weight.Bold)
            number.setFont(nf)
            item.addWidget(number, 0, Qt.AlignmentFlag.AlignVCenter)
            self._number_labels.append(number)

            caption = CaptionLabel(tr(tr_key))
            cf = caption.font()
            cf.setPixelSize(12)
            caption.setFont(cf)
            item.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
            self._caption_labels.append(caption)

            root.addLayout(item)

        # -- Spacer --
        root.addStretch(1)

        # -- Action buttons (big, centered, pill-shaped) --
        from qfluentwidgets import setCustomStyleSheet

        self._apply_btn = _CenteredPrimaryButton(tr("action_bar.apply").strip())
        self._apply_btn.setFixedHeight(40)
        self._apply_btn.setMinimumWidth(130)
        af = self._apply_btn.font()
        af.setPixelSize(14)
        af.setWeight(QFont.Weight.Bold)
        self._apply_btn.setFont(af)
        self._apply_btn.clicked.connect(self.apply_clicked)
        setCustomStyleSheet(self._apply_btn,
            "PrimaryPushButton { background: #2878D0; color: white; border-radius: 20px; border: none; padding: 0 28px; }"
            "PrimaryPushButton:hover { background: #3388E0; }"
            "PrimaryPushButton:pressed { background: #2060B0; }",
            "PrimaryPushButton { background: #3A8FE0; color: white; border-radius: 20px; border: none; padding: 0 28px; }"
            "PrimaryPushButton:hover { background: #4DA0F0; }"
            "PrimaryPushButton:pressed { background: #2878D0; }")
        root.addWidget(self._apply_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._revert_btn = _CenteredPushButton(tr("action_bar.revert_vanilla").strip())
        self._revert_btn.setFixedHeight(40)
        self._revert_btn.setMinimumWidth(130)
        rf = self._revert_btn.font()
        rf.setPixelSize(14)
        rf.setWeight(QFont.Weight.Bold)
        self._revert_btn.setFont(rf)
        self._revert_btn.clicked.connect(self.revert_clicked)
        setCustomStyleSheet(self._revert_btn,
            "PushButton { background: #F0F4F8; color: #4A5568; border: 1px solid #E2E8F0; border-radius: 20px; padding: 0 28px; }"
            "PushButton:hover { background: #E2E8F0; }"
            "PushButton:pressed { background: #CBD5E0; }",
            "PushButton { background: #2D3748; color: #E2E8F0; border: 1px solid #4A5568; border-radius: 20px; padding: 0 28px; }"
            "PushButton:hover { background: #3A4A5C; }"
            "PushButton:pressed { background: #4A5568; }")
        root.addWidget(self._revert_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._launch_btn = _CenteredPushButton(tr("action_bar.launch_game").strip())
        self._launch_btn.setFixedHeight(40)
        self._launch_btn.setMinimumWidth(130)
        lf = self._launch_btn.font()
        lf.setPixelSize(14)
        lf.setWeight(QFont.Weight.Bold)
        self._launch_btn.setFont(lf)
        self._launch_btn.clicked.connect(self.launch_clicked)
        setCustomStyleSheet(self._launch_btn,
            "PushButton { background: #2878D0; color: #FFFFFF; border: 1px solid #2878D0; border-radius: 20px; padding: 0 28px; }"
            "PushButton:hover { background: #3A8AE0; border-color: #3A8AE0; }"
            "PushButton:pressed { background: #1F68B8; border-color: #1F68B8; }",
            "PushButton { background: #2878D0; color: #FFFFFF; border: 1px solid #3A8AE0; border-radius: 20px; padding: 0 28px; }"
            "PushButton:hover { background: #3A8AE0; border-color: #5CA0F0; }"
            "PushButton:pressed { background: #1F68B8; border-color: #2878D0; }")
        root.addWidget(self._launch_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # Animation tracking
        self._current_values: list[int] = [0, 0, 0, 0]
        self._timelines: list[QTimeLine | None] = [None, None, None, None]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_stats(
        self, total: int = 0, active: int = 0, pending: int = 0, inactive: int = 0
    ) -> None:
        """Animate stat numbers from current to new values."""
        new_values = [total, active, pending, inactive]

        for i, (label, new_val) in enumerate(zip(self._number_labels, new_values)):
            old_val = self._current_values[i]
            if old_val == new_val:
                continue

            # Stop any running animation for this stat
            if self._timelines[i] is not None:
                self._timelines[i].stop()

            # Small changes: animate. Large jumps: set instantly.
            if abs(new_val - old_val) <= 200:
                timeline = QTimeLine(350, self)
                timeline.setFrameRange(old_val, new_val)
                timeline.setEasingCurve(QEasingCurve.Type.OutCubic)

                def _make_updater(_label):
                    def _update(frame):
                        _label.setText(str(frame))
                    return _update

                timeline.frameChanged.connect(_make_updater(label))
                timeline.start()
                self._timelines[i] = timeline
            else:
                label.setText(str(new_val))

            self._current_values[i] = new_val

    def retranslate_ui(self) -> None:
        """Update text with current translations."""
        for caption, (tr_key, _) in zip(self._caption_labels, self._STAT_DEFS):
            caption.setText(tr(tr_key))
        self._apply_btn.setText("\u25B6 " + tr("action_bar.apply").strip())
        self._revert_btn.setText(tr("action_bar.revert_vanilla").strip())
        self._launch_btn.setText(tr("action_bar.launch_game").strip())

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _apply_bar_style(self) -> None:
        dark = isDarkTheme()
        bg = "#14171E" if dark else "#FAFBFC"
        self._bar_bg = bg
        self._bar_border = "#2D3340" if dark else "#E5E7EB"
        self.setAutoFillBackground(False)
        self.update()

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QColor, QPainter, QPen
        painter = QPainter(self)
        # Background
        painter.fillRect(self.rect(), QColor(self._bar_bg if hasattr(self, '_bar_bg') else "#FAFBFC"))
        # Bottom border
        border_color = self._bar_border if hasattr(self, '_bar_border') else "#E5E7EB"
        painter.setPen(QPen(QColor(border_color), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_bar_style()
