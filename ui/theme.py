# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Diff colors, derived from the active Binary Ninja theme.

Kept in one place so the graph panes and the text panes agree, and so nothing
is hardcoded to a light or dark palette.
"""

from __future__ import annotations

import binaryninjaui  # must precede PySide6; see ui/__init__
from binaryninja import HighlightColor
from binaryninja.enums import HighlightStandardColor, ThemeColor
from PySide6.QtGui import QColor

from ..core.align import BlockStatus, LineStatus


def _theme(color: ThemeColor, fallback: tuple[int, int, int]) -> QColor:
    try:
        result = binaryninjaui.getThemeColor(color)
        if isinstance(result, QColor) and result.isValid():
            return result
    except Exception:
        pass
    return QColor(*fallback)


def _blend(base: QColor, tint: QColor, strength: float) -> QColor:
    """Mix a tint into the background so highlights stay readable in any theme."""

    return QColor(
        round(base.red() * (1 - strength) + tint.red() * strength),
        round(base.green() * (1 - strength) + tint.green() * strength),
        round(base.blue() * (1 - strength) + tint.blue() * strength),
    )


def background() -> QColor:
    return _theme(ThemeColor.LinearDisassemblyBlockColor, (40, 40, 40))


def graph_background() -> QColor:
    """Flow graph node fill, which is lighter than the linear view's."""

    return _theme(ThemeColor.GraphNodeDarkColor, (56, 56, 56))


#: Saturated reference tints; blended against the theme background before use.
_TINTS = {
    LineStatus.EQUAL: None,
    LineStatus.MINOR: QColor(80, 150, 220),
    LineStatus.CHANGED: QColor(210, 170, 40),
    LineStatus.ADDED: QColor(70, 190, 90),
    LineStatus.REMOVED: QColor(220, 70, 70),
    LineStatus.GAP: QColor(120, 120, 120),
    LineStatus.COMMENT: None,
}

#: How strongly each tint is mixed into the background. Differences that matter
#: are pushed harder; gaps and literal-only changes stay quiet.
_STRENGTH = {
    LineStatus.MINOR: 0.16,
    LineStatus.CHANGED: 0.30,
    LineStatus.ADDED: 0.28,
    LineStatus.REMOVED: 0.28,
    LineStatus.GAP: 0.07,
}

_BLOCK_TINTS = {
    BlockStatus.IDENTICAL: QColor(70, 190, 90),
    BlockStatus.CHANGED: QColor(210, 170, 40),
    BlockStatus.UNMATCHED: QColor(220, 70, 70),
}

#: Node-level tints. Only ``UNMATCHED`` gets one: an identical block is the
#: common case and filling it just adds noise, while a changed block is described
#: by its own tinted lines rather than by a flat wash over everything.
_BLOCK_HIGHLIGHTS = {
    BlockStatus.UNMATCHED: HighlightStandardColor.RedHighlightColor,
}


#: How far secondary text is pulled toward the background: far enough to
#: recede, not so far that it stops being text.
_DIM_STRENGTH = 0.42


def muted_text(widget) -> QColor:
    """A readable colour for secondary text, in whichever theme is active.

    Qt's own answer is ``palette(mid)``, and in Binary Ninja's dark themes that
    is a shade off the window colour — dimmed labels came out near-black on
    near-black. Blending the widget's own foreground into its own background
    instead lands correctly whichever way the theme goes.
    """

    palette = widget.palette()
    return _blend(palette.window().color(), palette.windowText().color(), 1.0 - _DIM_STRENGTH)


def line_color(status: LineStatus) -> QColor | None:
    """Row background for a diff status, or ``None`` to leave it untouched."""

    tint = _TINTS.get(status)
    if tint is None:
        return None
    return _blend(background(), tint, _STRENGTH[status])


#: How far a mark's tint is pushed over the background. Row tints are pale
#: because they sit behind text; a two-pixel mark on the overview strip or a
#: segment of the summary bar carries no text and has to read on its own.
_SOLID_STRENGTH = 0.75


def solid(tint: QColor) -> QColor:
    """A tint strong enough to stand alone, as a mark rather than a background."""

    return _blend(background(), tint, _SOLID_STRENGTH)


def marker_color(status: LineStatus) -> QColor | None:
    """Overview-strip mark for a line status, or ``None`` for one not worth marking."""

    tint = _TINTS.get(status)
    if tint is None or status is LineStatus.GAP:
        return None
    return solid(tint)


def status_tint(status: LineStatus) -> QColor | None:
    """The saturated reference tint for a status, before any blending."""

    return _TINTS.get(status)


#: Graph nodes have no room for the gutter markers the text panes use, so color
#: is the only signal there. Splitting modified instructions into two colors just
#: asks the reader to decode a distinction they cannot see the key for, so an
#: operand change reads the same as any other modification.
_GRAPH_EQUIVALENT = {LineStatus.MINOR: LineStatus.CHANGED}


def graph_line_color(status: LineStatus) -> QColor | None:
    """Background for one line of a flow graph node, or ``None`` to leave it be.

    Leaving unchanged lines alone is what makes the changed ones legible inside an
    otherwise untouched block.
    """

    status = _GRAPH_EQUIVALENT.get(status, status)
    tint = _TINTS.get(status)
    if tint is None:
        return None
    return _blend(graph_background(), tint, _STRENGTH[status])


def line_highlight(status: LineStatus) -> HighlightColor | None:
    """``graph_line_color`` as a Binary Ninja highlight, for FlowGraphNode.lines."""

    color = graph_line_color(status)
    if color is None:
        return None
    return HighlightColor(red=color.red(), green=color.green(), blue=color.blue())


def block_color(status: BlockStatus) -> QColor:
    return _blend(background(), _BLOCK_TINTS[status], 0.22)


def block_highlight(status: BlockStatus) -> HighlightStandardColor | None:
    """Whole-node highlight, or ``None`` to leave the node untinted."""

    return _BLOCK_HIGHLIGHTS.get(status)


def block_legend() -> list[tuple[str, QColor]]:
    """Whole-node states worth a color. Identical blocks are left plain."""

    return [("block only here", block_color(BlockStatus.UNMATCHED))]


def graph_line_legend() -> list[tuple[str, QColor | None]]:
    """Line states inside a matched block, as rendered in the graph."""

    return [
        ("modified", graph_line_color(LineStatus.CHANGED)),
        ("added", graph_line_color(LineStatus.ADDED)),
        ("removed", graph_line_color(LineStatus.REMOVED)),
    ]


def line_legend() -> list[tuple[str, QColor | None]]:
    """Legend for the text panes, in increasing order of significance."""

    return [
        (f"{LineStatus.MINOR.marker} operands", line_color(LineStatus.MINOR)),
        (f"{LineStatus.CHANGED.marker} changed", line_color(LineStatus.CHANGED)),
        (f"{LineStatus.ADDED.marker} added", line_color(LineStatus.ADDED)),
        (f"{LineStatus.REMOVED.marker} removed", line_color(LineStatus.REMOVED)),
    ]
