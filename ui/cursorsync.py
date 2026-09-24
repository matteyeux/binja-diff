# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Click a line on one side, and the other side goes to the matching line.

Neither ``LinearView`` nor ``FlowGraphWidget`` says when its cursor moves, so
this watches the clicks and keys that move it, reads ``getCurrentOffset()``
once the widget has handled them, and sends the other side to the aligned
address. Moving the other side programmatically produces no input event, so
following never bounces back.
"""

from __future__ import annotations

from collections.abc import Callable

# binaryninjaui must be imported before PySide6; see ui/__init__.
import binaryninjaui  # noqa: F401
from PySide6.QtCore import QEvent, QObject, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QPushButton, QWidget

from ..core.align import counterpart

_SIDE_PROPERTY = "binjaDiffSide"
_SETTING = "cursorSync"
_ORGANIZATION = _APPLICATION = "binja-diff"

#: Input that moves a cursor: clicks, and keys (arrows, page up and down).
_MOVES = {QEvent.Type.MouseButtonRelease, QEvent.Type.KeyRelease}


def _link_pixmap(color, size: int = 16, scale: int = 2) -> QPixmap:
    """Two interlocked chain links, the usual picture of "linked"."""

    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(1.6)
    painter.setPen(pen)
    painter.translate(size / 2, size / 2)
    painter.rotate(-45)
    width, height = size * 0.52, size * 0.30
    for offset in (-width * 0.32, width * 0.32):
        painter.drawRoundedRect(
            QRectF(offset - width / 2, -height / 2, width, height), height / 2, height / 2
        )
    painter.end()
    return pixmap


def link_icon(widget: QWidget) -> QIcon:
    """The Sync button's icon, drawn in the widget's own text colour.

    Drawn rather than loaded, so it follows the theme; the off state is dimmed,
    so the button reads as off even where a style draws checked buttons flat.
    """

    color = widget.palette().buttonText().color()
    dimmed = QColor(color)
    dimmed.setAlphaF(0.45)
    icon = QIcon()
    icon.addPixmap(_link_pixmap(color), QIcon.Normal, QIcon.On)
    icon.addPixmap(_link_pixmap(dimmed), QIcon.Normal, QIcon.Off)
    return icon


class CursorSync(QObject):
    """Follows the cursor of one pane with the other, while its button is on."""

    def __init__(
        self,
        parent: QWidget,
        offset_of: Callable[[str], int | None],
        go_to: Callable[[str, int], None],
    ):
        super().__init__(parent)
        self._offset_of = offset_of
        self._go_to = go_to
        self._pairs: list[tuple[int, int]] = []

        self.button = QPushButton(parent)
        self.button.setIcon(link_icon(self.button))
        self.button.setIconSize(QSize(16, 16))
        self.button.setCheckable(True)
        self.button.setToolTip(
            "Sync: clicking a line on one side moves the other side to the matching line"
        )
        self.button.setAccessibleName("Sync")
        saved = QSettings(_ORGANIZATION, _APPLICATION).value(_SETTING, True)
        self.button.setChecked(saved not in (False, "false", "0", 0))
        self.button.toggled.connect(self._toggled)

    def set_pairs(self, pairs: list[tuple[int, int]]) -> None:
        self._pairs = pairs

    def watch(self, widget, side: str) -> None:
        """Follow the cursor of ``widget``, the pane on ``side`` ("left" or "right")."""

        # A scroll area takes its clicks on the viewport, keys on itself.
        targets = [widget]
        viewport = getattr(widget, "viewport", None)
        if callable(viewport):
            targets.append(viewport())
        for target in targets:
            target.setProperty(_SIDE_PROPERTY, side)
            target.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if self.button.isChecked() and event.type() in _MOVES:
            side = watched.property(_SIDE_PROPERTY)
            if side in ("left", "right"):
                # After the widget has handled the event, so the cursor has moved.
                QTimer.singleShot(0, lambda: self.follow(side))
        return False

    def follow(self, side: str) -> None:
        address = self._offset_of(side)
        if address is None or not self._pairs:
            return
        target = counterpart(self._pairs, address, from_left=side == "left")
        if target is not None:
            self._go_to("right" if side == "left" else "left", target)

    def _toggled(self, checked: bool) -> None:
        QSettings(_ORGANIZATION, _APPLICATION).setValue(_SETTING, checked)
        if checked:
            self.follow("left")
