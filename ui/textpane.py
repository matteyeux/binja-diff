# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Side-by-side text diff of two functions, at an IL level or language of choice.

Rendering is done here rather than with ``TokenizedTextWidget``. That widget
has no notion of a per-line background: its only highlight is the
token-under-cursor state, and it ignores ``DisassemblyTextLine.highlight``
entirely, so diff rows came out uncolored. A ``QTextEdit`` lets us set a
background per line while still taking every foreground color from the active
Binary Ninja theme via ``getTokenColor``.
"""

from __future__ import annotations

# binaryninjaui must be imported before PySide6; see ui/__init__.
from binaryninjaui import getMonospaceFont, getTokenColor
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.align import (
    AlignedRow,
    LineStatus,
    RenderLevel,
    align_function_text,
    anchor_row,
    function_lines,
    line_address,
)
from . import theme
from .background import LatestOnly
from .levelpicker import LevelPicker


def _as_text_line(line):
    """Unwrap a ``LinearDisassemblyLine`` down to its ``DisassemblyTextLine``."""

    contents = getattr(line, "contents", None)
    return contents if contents is not None else line


class TokenTextView(QTextEdit):
    """Read-only view of tokenized lines with a per-line background color."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.NoWrap)
        self.setUndoRedoEnabled(False)
        self.setFont(getMonospaceFont(self))
        self.setTabChangesFocus(True)
        self._color_cache: dict[int, QColor] = {}

        # theme.line_color blends its tints against this exact color, so the
        # widget has to use it too or every highlight is subtly off.
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, theme.background())
        self.setPalette(palette)

    def token_color(self, token_type) -> QColor:
        """Theme color for a token type, cached.

        ``getTokenColor`` is a UI call per token type; the same handful repeat
        thousands of times in a single function.
        """

        key = int(token_type)
        cached = self._color_cache.get(key)
        if cached is not None:
            return cached
        try:
            color = getTokenColor(self, token_type)
        except Exception:
            color = None
        if not isinstance(color, QColor) or not color.isValid():
            color = self.palette().text().color()
        self._color_cache[key] = color
        return color

    def set_rows(self, rows: list[AlignedRow], side: str) -> None:
        self.setUpdatesEnabled(False)
        self.clear()

        cursor = QTextCursor(self.document())
        # One undo/layout step for the whole function; the panes are rebuilt on
        # every selection change and functions run to thousands of lines.
        cursor.beginEditBlock()

        marker_format = QTextCharFormat()
        marker_format.setForeground(self.palette().text().color())

        # theme.line_color reaches into the active theme on every call; there are
        # only six statuses but thousands of rows.
        colors = {status: theme.line_color(status) for status in LineStatus}

        for index, row in enumerate(rows):
            source = row.left if side == "left" else row.right
            status = row.status if source is not None else LineStatus.GAP

            if index:
                cursor.insertBlock()

            block_format = cursor.blockFormat()
            color = colors[status]
            if color is not None:
                block_format.setBackground(color)
            else:
                block_format.clearBackground()
            cursor.setBlockFormat(block_format)

            cursor.insertText(f"{status.marker} ", marker_format)
            if source is not None:
                self._insert_tokens(cursor, _as_text_line(source))

        cursor.endEditBlock()
        self.moveCursor(QTextCursor.Start)
        self.setUpdatesEnabled(True)

    def _insert_tokens(self, cursor: QTextCursor, text_line) -> None:
        """Write one line's tokens, merging runs that share a color."""

        pending: list[str] = []
        pending_color: QColor | None = None

        def flush() -> None:
            if not pending:
                return
            fmt = QTextCharFormat()
            fmt.setForeground(pending_color)
            cursor.insertText("".join(pending), fmt)
            pending.clear()

        for token in getattr(text_line, "tokens", ()):
            color = self.token_color(token.type)
            if pending_color is None or color != pending_color:
                flush()
                pending_color = color
            pending.append(token.text)
        flush()

    def top_line(self) -> int:
        return self.cursorForPosition(self.rect().topLeft()).blockNumber()

    def scroll_to_line(self, index: int) -> None:
        block = self.document().findBlockByNumber(max(index, 0))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        bar = self.verticalScrollBar()
        # Put the target at the top rather than merely on screen.
        bar.setValue(bar.value() + self.cursorRect(cursor).top())


class DiffTextPane(QWidget):
    """One side of the text diff: a title plus the token view."""

    def __init__(self, parent: QWidget, title: str):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.title = QLabel(title, self)
        self.title.setTextFormat(Qt.PlainText)
        layout.addWidget(self.title)

        self.text = TokenTextView(self)
        layout.addWidget(self.text, 1)

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def set_rows(self, rows: list[AlignedRow], side: str) -> None:
        self.text.set_rows(rows, side)

    def clear(self) -> None:
        self.text.clear()


class DiffOverview(QWidget):
    """A strip beside the panes marking where the differences are.

    The header says how many lines differ; this says where, for the whole
    function at once, and a click goes there. The outlined band is what the
    panes currently show.
    """

    WIDTH = 14

    def __init__(self, parent: QWidget, scroll_to):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Differences in this function. Click to jump.")
        self._scroll_to = scroll_to
        self._total = 0
        #: Runs of consecutive rows sharing a status: (first, count, color).
        self._runs: list[tuple[int, int, QColor]] = []
        self._view = (0.0, 1.0)

    def set_rows(self, rows: list[AlignedRow]) -> None:
        self._total = len(rows)
        self._runs = []
        colors = {status: theme.marker_color(status) for status in LineStatus}
        start, current = 0, None
        for index, row in enumerate([*rows, None]):
            status = row.status if row is not None else None
            if status is current:
                continue
            color = colors.get(current) if current is not None else None
            if color is not None:
                self._runs.append((start, index - start, color))
            start, current = index, status
        self.update()

    def set_viewport(self, top: float, span: float) -> None:
        self._view = (top, span)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.background())
        if not self._total:
            return
        height = self.height()
        scale = height / self._total
        for first, count, color in self._runs:
            # At least two pixels, or a single changed line in a long function
            # would vanish, which is precisely the one worth finding.
            painter.fillRect(
                QRectF(2, first * scale, self.WIDTH - 4, max(count * scale, 2.0)), color
            )
        top, span = self._view
        pen = QPen(self.palette().text().color())
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(QRectF(0.5, top * height, self.WIDTH - 1, max(span * height, 3.0) - 1))

    def mousePressEvent(self, event) -> None:
        self._jump(event.position().y())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self._jump(event.position().y())

    def _jump(self, y: float) -> None:
        if self._total and self.height():
            row = int(min(max(y / self.height(), 0.0), 1.0) * (self._total - 1))
            self._scroll_to(row)


class TextDiffTab(QWidget):
    """A full tab: two panes, synchronized scrolling, and a view selector.

    Aligning a function is done off the UI thread; see ``background``. Only
    filling the two text widgets happens here, since Qt widgets have to be.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._renderer = LatestOnly("Rendering the linear diff")
        #: The last pair asked for, so switching the view can re-render it.
        self._pair: tuple = (None, None, None, None)
        self._syncing = False
        self._rows: list[AlignedRow] = []
        #: Row indices that differ, for next/previous navigation.
        self._change_rows: list[int] = []
        self._change_cursor = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(2)
        self.splitter = QSplitter(Qt.Horizontal, self)
        self.left = DiffTextPane(self.splitter, "Primary")
        self.right = DiffTextPane(self.splitter, "Secondary")
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.right)
        self.splitter.setSizes([1, 1])
        body.addWidget(self.splitter, 1)
        self.overview = DiffOverview(self, self._scroll_to_row)
        body.addWidget(self.overview)
        layout.addLayout(body, 1)

        self._connect_scroll(self.left, self.right)
        self._connect_scroll(self.right, self.left)
        bar = self.left.text.verticalScrollBar()
        bar.valueChanged.connect(lambda _value: self._update_overview_viewport())
        bar.rangeChanged.connect(lambda *_args: self._update_overview_viewport())

    @property
    def renderer(self) -> LatestOnly:
        return self._renderer

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()

        header.addWidget(QLabel("View:", self))
        self.level_combo = LevelPicker(self, "linear")
        self.level_combo.currentIndexChanged.connect(lambda _index: self._reload())
        header.addWidget(self.level_combo)
        header.addSpacing(16)

        self.summary = QLabel("", self)
        header.addWidget(self.summary)
        header.addSpacing(12)

        for label, color in theme.line_legend():
            swatch = QLabel(f" {label} ", self)
            if color is not None:
                swatch.setStyleSheet(
                    f"background-color: {color.name()}; border-radius: 2px; padding: 1px 4px;"
                )
            header.addWidget(swatch)

        header.addStretch(1)

        self.position = QLabel("", self)
        header.addWidget(self.position)

        self.prev_button = QPushButton("Previous change", self)
        self.prev_button.clicked.connect(lambda: self._go_to_change(-1))
        header.addWidget(self.prev_button)

        self.next_button = QPushButton("Next change", self)
        self.next_button.clicked.connect(lambda: self._go_to_change(1))
        header.addWidget(self.next_button)

        return header

    @property
    def level(self) -> RenderLevel:
        return self.level_combo.level

    def _connect_scroll(self, source: DiffTextPane, target: DiffTextPane) -> None:
        source.text.verticalScrollBar().valueChanged.connect(
            lambda value: self._mirror(value, target)
        )

    def _mirror(self, value: int, target: DiffTextPane) -> None:
        # Both documents have the same line count and font, so scrollbar
        # positions correspond one to one.
        if self._syncing:
            return
        self._syncing = True
        try:
            target.text.verticalScrollBar().setValue(value)
        finally:
            self._syncing = False

    def _update_overview_viewport(self) -> None:
        bar = self.left.text.verticalScrollBar()
        extent = bar.maximum() + bar.pageStep()
        if extent <= 0:
            self.overview.set_viewport(0.0, 1.0)
            return
        self.overview.set_viewport(bar.value() / extent, bar.pageStep() / extent)

    def _scroll_to_row(self, row: int) -> None:
        self._syncing = True
        try:
            self.left.text.scroll_to_line(row)
            self.right.text.scroll_to_line(row)
        finally:
            self._syncing = False

    def _index_changes(self) -> None:
        """Record which rows differ and refresh the header."""

        self._change_rows = [i for i, row in enumerate(self._rows) if row.status.is_difference]
        self._change_cursor = -1
        self.overview.set_rows(self._rows)

        counts: dict[str, int] = {}
        for row in self._rows:
            if row.status.is_difference:
                counts[row.status.value] = counts.get(row.status.value, 0) + 1

        if not self._rows:
            self.summary.setText("")
        elif not counts:
            self.summary.setText(f"{len(self._rows)} lines, identical")
        else:
            detail = "  ".join(f"{name}: {count}" for name, count in sorted(counts.items()))
            self.summary.setText(
                f"{len(self._change_rows)} of {len(self._rows)} lines differ   {detail}"
            )

        has_changes = bool(self._change_rows)
        self.prev_button.setEnabled(has_changes)
        self.next_button.setEnabled(has_changes)
        self.position.setText("" if has_changes else "no differences")

    def _go_to_change(self, direction: int) -> None:
        if not self._change_rows:
            return
        if self._change_cursor == -1:
            # Start from what is on screen rather than jumping to the top.
            top = self.left.text.top_line()
            ahead = [i for i, row in enumerate(self._change_rows) if row >= top]
            if direction > 0:
                self._change_cursor = ahead[0] if ahead else 0
            else:
                self._change_cursor = (ahead[0] - 1) if ahead else len(self._change_rows) - 1
        else:
            self._change_cursor = (self._change_cursor + direction) % len(self._change_rows)

        row = self._change_rows[self._change_cursor]
        self._scroll_to_row(max(row - 3, 0))
        self.position.setText(f"change {self._change_cursor + 1} of {len(self._change_rows)}")

    def _top_address(self) -> int | None:
        """The address the reader is looking at, to find again in another view."""

        top = self.left.text.top_line()
        for row in self._rows[top : top + 50]:
            for line in (row.left, row.right):
                if line is not None and (address := line_address(line)) is not None:
                    return address
        return None

    def _reload(self) -> None:
        """Re-render the same pair in the newly chosen view, keeping the place."""

        if any(func is not None for func in self._pair[1::2]):
            self.show_pair(*self._pair, anchor=self._top_address())

    def show_pair(self, left_bv, left_func, right_bv, right_func, anchor=None) -> None:
        self._pair = (left_bv, left_func, right_bv, right_func)
        if left_func is None and right_func is None:
            self.clear()
            return

        level = self.level
        self.summary.setText("Rendering\u2026")
        self.position.setText("")
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        # Greyed rather than cleared: a quick render then swaps text for text,
        # instead of flashing an empty pane on every row change.
        self.splitter.setEnabled(False)

        def compute():
            return _render_rows(left_bv, left_func, right_bv, right_func, level)

        def deliver(rendered) -> None:
            rows, left_title, right_title = rendered
            self._show_rows(rows, left_title, right_title, anchor)

        def fail(exc: BaseException) -> None:
            self.splitter.setEnabled(True)
            self.summary.setText(f"could not render: {exc}")

        self._renderer.submit(compute, deliver, fail)

    def _show_rows(self, rows, left_title: str, right_title: str, anchor) -> None:
        self._rows = rows
        self.left.set_title(left_title)
        self.right.set_title(right_title)
        self.left.set_rows(rows, "left")
        self.right.set_rows(rows, "right")
        self.splitter.setEnabled(True)
        self._index_changes()
        if anchor is not None and rows:
            self._scroll_to_row(anchor_row(rows, anchor))

    def clear(self) -> None:
        self._renderer.cancel()
        self._pair = (None, None, None, None)
        self._rows = []
        self.splitter.setEnabled(True)
        self.left.set_title("Primary")
        self.right.set_title("Secondary")
        self.left.clear()
        self.right.clear()
        self._index_changes()


def _render_rows(left_bv, left_func, right_bv, right_func, level: RenderLevel):
    """Rows and pane titles for a pair, either side of which may be missing.

    Runs on a worker thread: it touches Binary Ninja, never Qt.
    """

    if left_func is not None and right_func is not None:
        rows = align_function_text(left_bv, left_func, right_bv, right_func, level)
        return (
            rows,
            f"{left_func.name} @ {left_func.start:#x}",
            f"{right_func.name} @ {right_func.start:#x}",
        )
    if left_func is not None:
        lines = function_lines(left_bv, left_func, level)
        return (
            [AlignedRow(line, None, LineStatus.REMOVED) for line in lines],
            f"{left_func.name} @ {left_func.start:#x} (only in primary)",
            "no match",
        )
    lines = function_lines(right_bv, right_func, level)
    return (
        [AlignedRow(None, line, LineStatus.ADDED) for line in lines],
        "no match",
        f"{right_func.name} @ {right_func.start:#x} (only in secondary)",
    )
