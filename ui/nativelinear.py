# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The Linear tab on Binary Ninja's own ``LinearView``, one function per side.

``textpane`` renders into a ``QTextEdit``, which can colour a row but knows
nothing of what is on it: no token highlighting, no following a call, no
renaming. The native view has all of that. It cannot be told what colour a
line is, so the diff reaches it through a render layer (``difflayer``), which
also pads each side with blank lines so the two stay row for row.

``textpane`` stays as the fallback for a Binary Ninja whose bindings lack
``setSingleFunctionView``.
"""

from __future__ import annotations

# binaryninjaui must be imported before PySide6; see ui/__init__.
from binaryninjaui import LinearView, ViewFrame
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSplitter, QVBoxLayout, QWidget

from binaryninja import FunctionViewType, HighlightColor, log_warn

from ..core.align import LineStatus, RenderLevel, address_pairs, line_address
from . import difflayer, theme
from .background import LatestOnly
from .cursorsync import CursorSync
from .levelpicker import LevelPicker
from .textpane import _render_rows


def available() -> bool:
    """Whether this Binary Ninja can host the native tab."""

    return all(
        hasattr(LinearView, name) for name in ("setSingleFunctionView", "navigateToFunction")
    )


def _highlight(color) -> HighlightColor | None:
    if color is None:
        return None
    return HighlightColor(red=color.red(), green=color.green(), blue=color.blue())


def _palette() -> tuple[dict, HighlightColor | None]:
    """Row colours as highlights, read from the theme on the UI thread."""

    colors = {status: _highlight(theme.line_color(status)) for status in LineStatus}
    return colors, colors.pop(LineStatus.GAP, None)


class NativePane(QWidget):
    """One side: a title, and a ``LinearView`` bound to that side's view."""

    def __init__(self, parent: QWidget, title: str):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)

        self.title = QLabel(title, self)
        self.title.setTextFormat(Qt.PlainText)
        self._layout.addWidget(self.title)

        self.placeholder = QLabel("", self)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self.placeholder, 1)

        self.view = None
        self.bv = None
        self.func = None
        #: Whether this widget's render layer switch has been flipped already.
        self.layer_toggled = False
        #: Flipping it did not help either; leave the switch alone from now on.
        self.layer_gave_up = False

    def bind(self, bv, frame) -> None:
        self.release()
        self.bv = bv
        self.layer_toggled = False
        self.layer_gave_up = False
        view = LinearView(bv, frame)
        view.setSingleFunctionView(True)
        self._layout.addWidget(view, 1)
        view.hide()
        self.view = view

    def release(self) -> None:
        """Destroy the widget now, not at the next event loop turn.

        It holds a reference to its BinaryView and listens for its changes, and
        the caller is usually about to close that view.
        """

        view, self.view, self.func = self.view, None, None
        if view is None:
            return
        view.hide()
        view.setParent(None)
        try:
            import shiboken6

            shiboken6.delete(view)
        except Exception:
            view.deleteLater()

    def show_function(self, func, level: RenderLevel, address: int | None = None) -> None:
        self.func = func
        if self.view is None or func is None:
            if self.view is not None:
                self.view.hide()
            self.placeholder.setText("no match" if func is None else "")
            self.placeholder.show()
            return
        self.placeholder.hide()
        try:
            self.view.setILViewType(FunctionViewType(level.graph_type))
        except Exception as exc:
            log_warn(f"Linear diff: cannot switch to {level.name}: {exc}", "QBinDiff")
        self.view.navigateToFunction(func, func.start if address is None else address)
        self.view.refreshContents()
        self.view.show()

    def layer_missing(self) -> bool:
        """Showing a painted function that the diff layer has not reached."""

        return (
            self.view is not None
            and self.func is not None
            and self.view.isVisible()
            and not difflayer.was_applied(self.bv, self.func)
        )

    def toggle_layer(self) -> None:
        self.layer_toggled = not self.layer_toggled
        self.view.toggleRenderLayer(difflayer.NAME)
        self.view.refreshContents()

    def go_to(self, address: int | None) -> None:
        if self.view is not None and self.func is not None and address is not None:
            self.view.navigateToFunction(self.func, address)

    def current_offset(self) -> int | None:
        if self.view is None or self.func is None:
            return None
        try:
            return self.view.getCurrentOffset()
        except Exception:
            return None

    def scroll_bar(self):
        return self.view.verticalScrollBar() if self.view is not None else None


class NativeLinearTab(QWidget):
    """Two native linear views, painted by the diff layer."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._renderer = LatestOnly("Rendering the linear diff")
        #: Bumped per shown pair, so a pending layer check for an old one drops.
        self._generation = 0
        self._pair: tuple = (None, None, None, None)
        self._painted: list[tuple] = []
        self._rows: list = []
        self._change_rows: list[int] = []
        self._change_cursor = -1
        self._syncing = False
        self.sync = CursorSync(
            self,
            lambda side: self._pane(side).current_offset(),
            lambda side, address: self._pane(side).go_to(address),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(self._build_header())

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.left = NativePane(self.splitter, "Primary")
        self.right = NativePane(self.splitter, "Secondary")
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.right)
        self.splitter.setSizes([1, 1])
        layout.addWidget(self.splitter, 1)

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
        header.addWidget(self.sync.button)
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

    # -- binding -----------------------------------------------------------

    def set_views(self, left_bv, right_bv) -> None:
        """Build both widgets; a ``LinearView`` binds its BinaryView for life."""

        frame = ViewFrame.viewFrameForWidget(self)
        self.left.bind(left_bv, frame)
        self.right.bind(right_bv, frame)
        self.sync.watch(self.left.view, "left")
        self.sync.watch(self.right.view, "right")
        for source, target in ((self.left, self.right), (self.right, self.left)):
            bar = source.scroll_bar()
            if bar is not None:
                bar.valueChanged.connect(lambda value, target=target: self._mirror(value, target))

    def _pane(self, side: str) -> NativePane:
        return self.left if side == "left" else self.right

    def release_views(self) -> None:
        """Drop both widgets, before the views they hold are closed."""

        self._renderer.cancel()
        self._unpaint()
        self.left.release()
        self.right.release()

    def _mirror(self, value: int, target: NativePane) -> None:
        # Padding makes the two sides the same length, so positions correspond.
        bar = target.scroll_bar()
        if self._syncing or bar is None:
            return
        self._syncing = True
        try:
            bar.setValue(value)
        finally:
            self._syncing = False

    # -- showing a pair ----------------------------------------------------

    def _unpaint(self) -> None:
        for bv, func in self._painted:
            difflayer.set_paint(bv, func, None)
        self._painted = []

    def _reload(self) -> None:
        if any(func is not None for func in self._pair[1::2]):
            self.show_pair(*self._pair, anchor=self.left.current_offset())

    def show_pair(self, left_bv, left_func, right_bv, right_func, anchor=None) -> None:
        self._pair = (left_bv, left_func, right_bv, right_func)
        if left_func is None and right_func is None:
            self.clear()
            return

        level = self.level
        colors, gap_color = _palette()
        self.summary.setText("Rendering…")
        self.position.setText("")
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

        def compute():
            rows, left_title, right_title = _render_rows(
                left_bv, left_func, right_bv, right_func, level
            )
            statuses = [row.status for row in rows]
            left = difflayer.build_paint([r.left for r in rows], statuses, colors, gap_color)
            right = difflayer.build_paint([r.right for r in rows], statuses, colors, gap_color)
            return rows, left_title, right_title, left, right

        def deliver(rendered) -> None:
            rows, left_title, right_title, left_paint, right_paint = rendered
            self._generation += 1
            self._unpaint()
            # Painted before the widgets are pointed at the functions, so the
            # first render the layer sees already has the diff in it.
            for bv, func, paint in (
                (left_bv, left_func, left_paint),
                (right_bv, right_func, right_paint),
            ):
                if func is not None:
                    difflayer.set_paint(bv, func, paint)
                    self._painted.append((bv, func))
            self._rows = rows
            self.sync.set_pairs(address_pairs(rows))
            self.left.title.setText(left_title)
            self.right.title.setText(right_title)
            left_at, right_at = self._anchor_addresses(anchor)
            self.left.show_function(left_func, level, left_at)
            self.right.show_function(right_func, level, right_at)
            self._index_changes()
            self._check_layer([self.left, self.right])

        def fail(exc: BaseException) -> None:
            self.summary.setText(f"could not render: {exc}")

        self._renderer.submit(compute, deliver, fail)

    #: How long a widget gets to render before its layer is judged missing.
    LAYER_CHECK_MS = 400

    def _check_layer(self, panes: list, flipped: NativePane | None = None) -> None:
        """Make sure each pane actually runs the diff layer, one pane at a time.

        The layer is on by default, but a view's switch can be off — the UI
        remembers it, and nothing reads it back. So whether it ran is asked of
        the layer itself, and a pane it never reached gets its switch flipped
        once; if that did not help either, it is flipped back. Strictly one pane
        at a time: should the switch turn out to be shared, flipping both at
        once would cancel out.
        """

        generation = self._generation

        def later(action) -> None:
            QTimer.singleShot(
                self.LAYER_CHECK_MS, lambda: action() if generation == self._generation else None
            )

        if flipped is not None:
            if flipped.layer_missing():
                flipped.toggle_layer()
                flipped.layer_gave_up = True
                log_warn(
                    f"{flipped.title.text()}: the {difflayer.NAME} render layer does not run; "
                    "enable it from this pane's render layer menu",
                    "QBinDiff",
                )
            later(lambda: self._check_layer(panes))
            return
        if not panes:
            return
        pane, rest = panes[0], panes[1:]

        def check() -> None:
            if pane.layer_missing() and not pane.layer_toggled and not pane.layer_gave_up:
                pane.toggle_layer()
                later(lambda: self._check_layer(rest, flipped=pane))
            else:
                self._check_layer(rest)

        later(check)

    def _anchor_addresses(self, anchor: int | None) -> tuple[int | None, int | None]:
        """Where each side should land to keep the reader's place on the left."""

        if anchor is None:
            return None, None
        for row in self._rows:
            if row.left is not None and line_address(row.left) == anchor:
                right = line_address(row.right) if row.right is not None else None
                return anchor, right
        return anchor, None

    def _index_changes(self) -> None:
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
        self._change_cursor = (
            (0 if direction > 0 else len(self._change_rows) - 1)
            if self._change_cursor == -1
            else (self._change_cursor + direction) % len(self._change_rows)
        )
        row = self._rows[self._change_rows[self._change_cursor]]
        left = line_address(row.left) if row.left is not None else None
        right = line_address(row.right) if row.right is not None else None
        self.left.go_to(left if left is not None else right)
        self.right.go_to(right if right is not None else left)
        self.position.setText(f"change {self._change_cursor + 1} of {len(self._change_rows)}")

    def clear(self) -> None:
        self._renderer.cancel()
        self._unpaint()
        self._pair = (None, None, None, None)
        self._rows = []
        self.sync.set_pairs([])
        self.left.title.setText("Primary")
        self.right.title.setText("Secondary")
        self.left.show_function(None, self.level)
        self.right.show_function(None, self.level)
        self.left.placeholder.setText("")
        self.right.placeholder.setText("")
        self._index_changes()
