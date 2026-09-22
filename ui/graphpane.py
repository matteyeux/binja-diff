# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Side-by-side control flow graphs with diff coloring.

Whole nodes are tinted only for whole-block facts: a block that is identical, or
one that exists on a single side. A block that matched but changed is left plain
and its differing *instructions* are tinted individually, since filling the node
says only that something changed somewhere in it.
"""

from __future__ import annotations

import binaryninjaui  # noqa: F401  (must precede PySide6)
from binaryninja import DisassemblySettings
from binaryninjaui import FlowGraphWidget
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.align import (
    BlockAlignment,
    BlockStatus,
    RenderLevel,
    align_blocks,
    align_line_statuses,
    available_levels,
    ensure_rendering,
    il_basic_blocks,
)
from . import theme


def build_graph(func, level: RenderLevel):
    """Lay out a function's CFG and index its nodes by basic block address.

    At IL levels and languages ``BasicBlock.start`` is an instruction index, not
    an address; it is only ever a key within one rendering, so that is fine.
    """

    if func is None:
        return None, {}

    # A graph of IL that has not been generated is a single "Loading..." node.
    ensure_rendering(func, level)
    graph = func.create_graph(graph_type=level.graph_type, settings=DisassemblySettings())
    # Nodes are not populated until layout completes.
    graph.layout_and_wait()

    nodes = {}
    for node in graph.nodes:
        block = node.basic_block
        if block is not None:
            nodes[block.start] = node
    return graph, nodes


def highlight_lines(node, lines, statuses) -> None:
    """Tint only the lines that differ, leaving the rest as plain text.

    ``FlowGraphNode.lines`` round-trips ``DisassemblyTextLine.highlight`` through
    the core, so a node can mark individual instructions. ``lines`` must be the
    very list that produced ``statuses``; the getter builds fresh objects on each
    call, so re-reading it here would break the correspondence.
    """

    if not lines or len(lines) != len(statuses):
        return
    for line, status in zip(lines, statuses, strict=True):
        color = theme.line_highlight(status)
        if color is not None:
            line.highlight = color
    node.lines = lines


class GraphPane(QWidget):
    """One side: a title and a flow graph widget."""

    def __init__(self, parent: QWidget, bv, title: str):
        super().__init__(parent)
        self.bv = bv

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.title = QLabel(title, self)
        self.title.setTextFormat(Qt.PlainText)
        layout.addWidget(self.title)

        self.graph = FlowGraphWidget(self, bv)
        layout.addWidget(self.graph, 1)

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def show_graph(self, graph) -> None:
        self.graph.setGraph(graph)


class GraphDiffTab(QWidget):
    """Basic-block diff: two CFGs plus a view selector and a legend.

    A language representation is laid out from its own graph but paired on
    HLIL's blocks, which it shares; only the lines drawn into them differ.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._levels = available_levels()
        self._left_bv = None
        self._right_bv = None
        self._left_func = None
        self._right_func = None
        self._alignment = BlockAlignment()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("View:", self))
        self.level_combo = QComboBox(self)
        self.level_combo.addItems([level.name for level in self._levels])
        self.level_combo.currentIndexChanged.connect(lambda _index: self._reload())
        header.addWidget(self.level_combo)
        header.addSpacing(16)

        # Only differences are colored, so the legend only lists differences.
        header.addWidget(QLabel("lines:", self))
        for label, color in theme.graph_line_legend():
            header.addWidget(self._swatch(label, color))

        header.addSpacing(12)
        for label, color in theme.block_legend():
            header.addWidget(self._swatch(label, color))

        self.summary = QLabel("", self)
        header.addSpacing(16)
        header.addWidget(self.summary)
        header.addStretch(1)
        layout.addLayout(header)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.left = GraphPane(self.splitter, None, "Primary")
        self.right = GraphPane(self.splitter, None, "Secondary")
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.right)
        self.splitter.setSizes([1, 1])
        layout.addWidget(self.splitter, 1)

    def _swatch(self, label: str, color) -> QLabel:
        swatch = QLabel(f"  {label}  ", self)
        if color is not None:
            swatch.setStyleSheet(
                f"background-color: {color.name()}; border-radius: 2px; padding: 1px 4px;"
            )
        return swatch

    @property
    def level(self) -> RenderLevel:
        return self._levels[max(self.level_combo.currentIndex(), 0)]

    def set_views(self, left_bv, right_bv) -> None:
        """Rebuild the graph widgets; they bind a BinaryView at construction."""

        self._left_bv = left_bv
        self._right_bv = right_bv

        index = self.splitter.indexOf(self.left)
        self.left.setParent(None)
        self.left = GraphPane(self.splitter, left_bv, "Primary")
        self.splitter.insertWidget(index, self.left)

        index = self.splitter.indexOf(self.right)
        self.right.setParent(None)
        self.right = GraphPane(self.splitter, right_bv, "Secondary")
        self.splitter.insertWidget(index, self.right)
        self.splitter.setSizes([1, 1])

    def show_pair(self, left_func, right_func) -> None:
        self._left_func = left_func
        self._right_func = right_func
        self._reload()

    def _reload(self) -> None:
        left_func, right_func = self._left_func, self._right_func
        level = self.level

        if left_func is not None and right_func is not None:
            self._alignment = align_blocks(left_func, right_func, level.level)
            counts: dict[str, int] = {}
            for status in self._alignment.left_status.values():
                counts[status.value] = counts.get(status.value, 0) + 1
            self.summary.setText(
                "  ".join(f"{name}: {count}" for name, count in sorted(counts.items()))
            )
        else:
            self._alignment = BlockAlignment()
            self.summary.setText("")

        self._set_titles()

        left_graph, left_nodes = build_graph(left_func, level)
        right_graph, right_nodes = build_graph(right_func, level)

        self._color_nodes(left_nodes, self._left_status(left_func, level.level))
        self._color_nodes(right_nodes, self._right_status(right_func, level.level))
        self._mark_changed_lines(left_func, right_func, left_nodes, right_nodes)

        # Highlights do not change line text, but the core recomputes node
        # geometry when lines are replaced, so lay out again before showing.
        for graph in (left_graph, right_graph):
            if graph is not None:
                graph.layout_and_wait()

        self.left.show_graph(left_graph)
        self.right.show_graph(right_graph)

    def _set_titles(self) -> None:
        left_func, right_func = self._left_func, self._right_func
        if left_func is not None:
            suffix = "" if right_func is not None else " (only in primary)"
            self.left.set_title(f"{left_func.name} @ {left_func.start:#x}{suffix}")
        else:
            self.left.set_title("no match")

        if right_func is not None:
            suffix = "" if left_func is not None else " (only in secondary)"
            self.right.set_title(f"{right_func.name} @ {right_func.start:#x}{suffix}")
        else:
            self.right.set_title("no match")

    def _left_status(self, func, level: str) -> dict[int, BlockStatus]:
        if func is None:
            return {}
        return self._alignment.left_status or {
            b.start: BlockStatus.UNMATCHED for b in il_basic_blocks(func, level)
        }

    def _right_status(self, func, level: str) -> dict[int, BlockStatus]:
        if func is None:
            return {}
        return self._alignment.right_status or {
            b.start: BlockStatus.UNMATCHED for b in il_basic_blocks(func, level)
        }

    def _color_nodes(self, nodes, statuses: dict[int, BlockStatus]) -> None:
        """Tint whole nodes only where the whole block is the story.

        ``block_highlight`` returns ``None`` for identical blocks (the common
        case, and noise if colored) and for changed ones, which are described by
        their own tinted lines instead of a flat wash.
        """

        for addr, status in statuses.items():
            node = nodes.get(addr)
            if node is None:
                continue
            color = theme.block_highlight(status)
            if color is not None:
                node.highlight = color

    def _mark_changed_lines(self, left_func, right_func, left_nodes, right_nodes) -> None:
        """Mark the differing instructions inside each matched-but-changed block."""

        if left_func is None or right_func is None:
            return

        for left_addr, right_addr in self._alignment.left_to_right.items():
            if self._alignment.left_status.get(left_addr) is not BlockStatus.CHANGED:
                continue
            left_node = left_nodes.get(left_addr)
            right_node = right_nodes.get(right_addr)
            if left_node is None or right_node is None:
                continue

            # Align the nodes' own lines so the statuses map onto them exactly.
            left_lines = left_node.lines
            right_lines = right_node.lines
            left_statuses, right_statuses = align_line_statuses(left_lines, right_lines)
            highlight_lines(left_node, left_lines, left_statuses)
            highlight_lines(right_node, right_lines, right_statuses)

    def clear(self) -> None:
        self._left_func = None
        self._right_func = None
        self._alignment = BlockAlignment()
        self.summary.setText("")
        self.left.set_title("Primary")
        self.right.set_title("Secondary")
        self.left.show_graph(None)
        self.right.show_graph(None)
