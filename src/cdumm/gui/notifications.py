"""In-app notification centre.

Every ``InfoBar`` toast the app raises is (a) auto-dismissed after a few
seconds and (b) recorded here, then surfaced through a single bell button
in the navigation rail (with an unread-count badge) so the user can catch
up on anything they missed — on any page, without a per-page widget.
"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)

# Level -> dot colour for the panel rows.
_LEVEL_COLOURS = {
    "success": "#2ECC71",
    "warning": "#F39C12",
    "error": "#E74C3C",
    "info": "#3498DB",
}


class Notification:
    __slots__ = ("level", "title", "message", "time", "read",
                 "action_label", "action_cb")

    def __init__(self, level: str, title: str, message: str,
                 action_label: str | None = None, action_cb=None) -> None:
        self.level = level
        self.title = title
        self.message = message
        self.time = datetime.now()
        self.read = False
        self.action_label = action_label
        self.action_cb = action_cb


def restart_app() -> None:
    """Relaunch CDUMM so new startup-time settings (e.g. interface zoom)
    take effect.

    A PyInstaller one-file exe passes its extraction dir to child
    processes via ``_MEI*`` / ``_PYI*`` environment variables. A naive
    relaunch therefore makes the new instance reuse THIS process's ``_MEI``
    dir — which the bootloader deletes as we exit — and the child crashes
    with "No module named '_sqlite3'". Launch the child with those vars
    stripped so it extracts its own fresh copy.
    """
    import os
    import subprocess
    import sys
    from PySide6.QtWidgets import QApplication
    try:
        args = ([sys.executable] + sys.argv[1:]
                if getattr(sys, "frozen", False)
                else [sys.executable] + sys.argv)
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("_MEI") or k.startswith("_PYI"))}
        kwargs = {"close_fds": True, "env": env}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so the child
            # fully outlives us.
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        subprocess.Popen(args, **kwargs)
    except Exception as e:
        logger.warning("restart failed: %s", e)
        return
    QApplication.quit()


class NotificationStore(QObject):
    """Holds the recent-notifications list and signals when it changes."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._items: list[Notification] = []

    def add(self, level: str, title: str, message: str = "",
            action_label: str | None = None, action_cb=None) -> None:
        title, message = str(title), str(message or "")
        self._items.insert(0, Notification(
            level, title, message, action_label, action_cb))
        del self._items[100:]  # keep the list bounded
        # Mirror every notification into the app log so the text — e.g.
        # the "N patches skipped" apply summary — survives in the bug
        # report even after the toast disappears and can be copied from
        # the log file. falobos76 asked for exactly this (#191): the
        # on-screen/notification text used to live only in memory.
        line = f"notification [{level}] {title}" + (f": {message}" if message else "")
        try:
            _lvl = {"error": logging.ERROR, "warning": logging.WARNING}.get(
                level, logging.INFO)
            logger.log(_lvl, "%s", line)
        except Exception:
            pass
        self.changed.emit()

    def items(self) -> list[Notification]:
        return list(self._items)

    def unread_count(self) -> int:
        return sum(1 for n in self._items if not n.read)

    def mark_all_read(self) -> None:
        for n in self._items:
            n.read = True
        self.changed.emit()

    def clear(self) -> None:
        self._items.clear()
        self.changed.emit()


_store = NotificationStore()


def store() -> NotificationStore:
    return _store


def install_infobar_capture() -> None:
    """Wrap ``InfoBar.success/warning/error/info`` so every toast is
    recorded here and auto-dismisses (persistent -1 durations become 5s).

    Idempotent, and wrapped defensively so it can never break a toast.
    """
    from qfluentwidgets import InfoBar
    if getattr(InfoBar, "_cdumm_captured", False):
        return

    for level in ("success", "warning", "error", "info"):
        orig = getattr(InfoBar, level)

        def _make(orig, level):
            def wrapper(title, content="", *args, **kwargs):
                try:
                    if kwargs.get("duration", 1000) == -1:
                        kwargs["duration"] = 5000
                    _store.add(level, title, content)
                except Exception:
                    pass
                return orig(title, content, *args, **kwargs)
            return staticmethod(wrapper)

        setattr(InfoBar, level, _make(orig, level))

    InfoBar._cdumm_captured = True


def _rel_time(dt: datetime) -> str:
    secs = int((datetime.now() - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return dt.strftime("%b %d")


class NotificationPanel(QWidget):
    """Flyout content: the recent-notifications list + actions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(360)
        from qfluentwidgets import StrongBodyLabel, TransparentPushButton

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Notifications", self))
        header.addStretch()
        self._read_btn = TransparentPushButton("Mark all read", self)
        self._read_btn.clicked.connect(_store.mark_all_read)
        header.addWidget(self._read_btn)
        self._clear_btn = TransparentPushButton("Clear", self)
        self._clear_btn.clicked.connect(_store.clear)
        header.addWidget(self._clear_btn)
        root.addLayout(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._list_host = QWidget(self._scroll)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_host)
        self._scroll.setMinimumHeight(220)
        root.addWidget(self._scroll)

        self.refresh()
        _store.changed.connect(self.refresh)

    def refresh(self) -> None:
        # Clear existing rows (keep the trailing stretch).
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        items = _store.items()
        if not items:
            from qfluentwidgets import CaptionLabel
            # Centre the empty-state text within the scroll region. Sandwich
            # it between two stretches so it sits in the middle of the box
            # instead of pinned to the top edge (where it looked like it was
            # floating above the panel).
            self._list_layout.insertStretch(0)
            empty = CaptionLabel("You're all caught up.", self._list_host)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_layout.insertWidget(1, empty)
            return

        for n in items:
            self._list_layout.insertWidget(
                self._list_layout.count() - 1, self._make_row(n))

    def _make_row(self, n: Notification) -> QWidget:
        from qfluentwidgets import BodyLabel, CaptionLabel
        row = QWidget(self._list_host)
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(10)

        dot = QLabel(row)
        dot.setFixedSize(10, 10)
        colour = _LEVEL_COLOURS.get(n.level, "#3498DB")
        dot.setStyleSheet(f"background: {colour}; border-radius: 5px;")
        h.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        # Let the user select and copy the notification text — the apply
        # "N patches skipped" summary only lived here and couldn't be
        # copied before (falobos76, #191).
        _selectable = (Qt.TextInteractionFlag.TextSelectableByMouse
                       | Qt.TextInteractionFlag.TextSelectableByKeyboard)

        col = QVBoxLayout()
        col.setSpacing(1)
        title = BodyLabel(n.title, row)
        title.setWordWrap(True)
        title.setTextInteractionFlags(_selectable)
        if n.read:
            title.setStyleSheet("BodyLabel { color: #9AA0A6; }")
        col.addWidget(title)
        if n.message:
            msg = CaptionLabel(n.message, row)
            msg.setWordWrap(True)
            msg.setTextInteractionFlags(_selectable)
            col.addWidget(msg)
        col.addWidget(CaptionLabel(_rel_time(n.time), row))
        h.addLayout(col, 1)

        if n.action_label and n.action_cb:
            from qfluentwidgets import PrimaryPushButton
            act = PrimaryPushButton(n.action_label, row)
            act.setFixedHeight(30)

            def _do(_=None, cb=n.action_cb):
                try:
                    cb()
                except Exception as e:
                    logger.warning("notification action failed: %s", e)
            act.clicked.connect(_do)
            h.addWidget(act, 0, Qt.AlignmentFlag.AlignVCenter)

        # Scope the tint to the row itself. A bare ``QWidget { … }`` rule
        # cascades to every child, so the BodyLabel / CaptionLabel each got
        # their own translucent pill painted directly behind the text —
        # which on some themes sits light-on-text and makes the notification
        # hard to read in dark mode (falobos76, #191). An objectName
        # selector paints only the row, so the text renders on the flat
        # panel background with full theme contrast.
        row.setObjectName("notifRow")
        row.setStyleSheet(
            "#notifRow { background: rgba(127,127,127,0.08); border-radius: 8px; }")
        return row


class _BadgeManager(QObject):
    """Keeps an unread-count badge pinned to the bell nav widget and in
    sync with the store."""

    def __init__(self, bell_widget: QWidget) -> None:
        super().__init__(bell_widget)
        self._bell = bell_widget
        self._badge = None
        bell_widget.installEventFilter(self)
        _store.changed.connect(self._refresh)
        self._refresh()

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            self._reposition()
        return False

    def _refresh(self) -> None:
        from qfluentwidgets import InfoBadge
        count = _store.unread_count()
        if count <= 0:
            if self._badge is not None:
                self._badge.hide()
            return
        text = "99+" if count > 99 else str(count)
        if self._badge is None:
            self._badge = InfoBadge.error(text, parent=self._bell)
        else:
            self._badge.setText(text)
            self._badge.show()
        self._badge.adjustSize()
        self._reposition()

    def _reposition(self) -> None:
        if self._badge is None:
            return
        x = max(0, self._bell.width() - self._badge.width() - 6)
        self._badge.move(x, 4)
        self._badge.raise_()


def install_bell(window) -> None:
    """Add the notification bell to the navigation rail (bottom section)
    and wire it to the store. Call this BEFORE the bug-report nav item is
    added so the bell sits above it.
    """
    install_infobar_capture()

    from qfluentwidgets import FluentIcon, NavigationItemPosition, Flyout, FlyoutAnimationType

    nav = window.navigationInterface

    def _open_panel():
        try:
            _store.mark_all_read()
            bell_w = nav.widget("notif_bell")
            panel = NotificationPanel(window)
            Flyout.make(panel, target=bell_w, parent=window,
                        aniType=FlyoutAnimationType.SLIDE_RIGHT)
        except Exception as e:
            logger.warning("notification panel failed: %s", e)

    nav.addItem(
        routeKey="notif_bell",
        icon=FluentIcon.RINGER,
        text="Notifications",
        onClick=_open_panel,
        selectable=False,
        position=NavigationItemPosition.BOTTOM,
        tooltip="Notifications",
    )

    try:
        bell_w = nav.widget("notif_bell")
        if bell_w is not None:
            window._notif_badge_mgr = _BadgeManager(bell_w)
    except Exception as e:
        logger.debug("bell badge setup skipped: %s", e)
