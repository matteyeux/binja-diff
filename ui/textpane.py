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
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
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
    available_levels,
    function_lines,
)
from . import theme


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


class TextDiffTab(QWidget):
    """A full tab: two panes, synchronized scrolling, and a view selector."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._levels = available_levels()
        #: The last pair shown, so switching the view can re-render it.
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

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.left = DiffTextPane(self.splitter, "Primary")
        self.right = DiffTextPane(self.splitter, "Secondary")
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.right)
        self.splitter.setSizes([1, 1])
        layout.addWidget(self.splitter, 1)

        self._connect_scroll(self.left, self.right)
        self._connect_scroll(self.right, self.left)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()

        header.addWidget(QLabel("View:", self))
        self.level_combo = QComboBox(self)
        self.level_combo.addItems([level.name for level in self._levels])
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

    def _index_changes(self) -> None:
        """Record which rows differ and refresh the header."""

        self._change_rows = [i for i, row in enumerate(self._rows) if row.status.is_difference]
        self._change_cursor = -1

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
        target = max(row - 3, 0)
        self._syncing = True
        try:
            self.left.text.scroll_to_line(target)
            self.right.text.scroll_to_line(target)
        finally:
            self._syncing = False
        self.position.setText(f"change {self._change_cursor + 1} of {len(self._change_rows)}")

    @property
    def level(self) -> RenderLevel:
        return self._levels[max(self.level_combo.currentIndex(), 0)]

    def _reload(self) -> None:
        if any(func is not None for func in self._pair[1::2]):
            self.show_pair(*self._pair)

    def show_pair(self, left_bv, left_func, right_bv, right_func) -> None:
        self._pair = (left_bv, left_func, right_bv, right_func)
        if left_func is None or right_func is None:
            self.show_single(left_bv, left_func, right_bv, right_func)
            return

        self._rows = align_function_text(left_bv, left_func, right_bv, right_func, self.level)
        self.left.set_title(f"{left_func.name} @ {left_func.start:#x}")
        self.right.set_title(f"{right_func.name} @ {right_func.start:#x}")
        self.left.set_rows(self._rows, "left")
        self.right.set_rows(self._rows, "right")
        self._index_changes()

    def show_single(self, left_bv, left_func, right_bv, right_func) -> None:
        """Render an unmatched function on whichever side has it."""

        if left_func is not None:
            lines = function_lines(left_bv, left_func, self.level)
            self._rows = [AlignedRow(line, None, LineStatus.REMOVED) for line in lines]
            self.left.set_title(f"{left_func.name} @ {left_func.start:#x} (only in primary)")
            self.right.set_title("no match")
        elif right_func is not None:
            lines = function_lines(right_bv, right_func, self.level)
            self._rows = [AlignedRow(None, line, LineStatus.ADDED) for line in lines]
            self.left.set_title("no match")
            self.right.set_title(f"{right_func.name} @ {right_func.start:#x} (only in secondary)")
        else:
            self.clear()
            return

        self.left.set_rows(self._rows, "left")
        self.right.set_rows(self._rows, "right")
        self._index_changes()

    def clear(self) -> None:
        self._pair = (None, None, None, None)
        self._rows = []
        self.left.set_title("Primary")
        self.right.set_title("Secondary")
        self.left.clear()
        self.right.clear()
        self._index_changes()
