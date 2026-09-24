# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""A render layer that paints a diff into Binary Ninja's own linear view.

The native ``LinearView`` is what makes the Linear tab interactive — token
highlighting, following a call, renaming, commenting — but it draws lines the
core produced, so the only way to colour them is to change them on their way
to the screen. That is what a render layer is for.

It only touches functions a diff is currently showing, and forgets them as soon
as the selection moves on. While a pair is on screen, another tab showing one of
those two functions shows the diff too; the layer can be switched off there from
the view's render layer menu.

No Qt here: the core calls this from whatever thread renders, and everything it
reads was resolved on the UI thread beforehand.
"""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass, field

from binaryninja import (
    DisassemblyTextLine,
    HighlightColor,
    InstructionTextToken,
    LinearDisassemblyLine,
    RenderLayer,
)
from binaryninja.enums import (
    InstructionTextTokenType,
    LinearDisassemblyLineType,
    RenderLayerDefaultEnableState,
)

from ..core.align import compare_line, instruction_text, line_address

NAME = "Binary Diff"


def line_key(line) -> tuple[int | None, str]:
    """What identifies a rendered line across two renderings of one function.

    The diff is aligned from lines rendered with default settings, and the
    widget renders with the reader's; the address and the instruction text,
    annotations removed, survive that.
    """

    contents = getattr(line, "contents", None)
    return line_address(line), compare_line(instruction_text(contents if contents else line))


@dataclass
class Paint:
    """One side of one aligned function, as the layer needs it."""

    #: Highlight per line key, first occurrence wins; ``None`` for a line that
    #: is known and left plain, which must not fall back to its address.
    colors: dict[tuple, HighlightColor | None] = field(default_factory=dict)
    #: Highlight per address, for a line whose text the reader's settings changed.
    by_address: dict[int, HighlightColor] = field(default_factory=dict)
    #: Padding lines to emit before a line, standing opposite lines only the
    #: other side has, so both sides stay row for row.
    gaps_before: dict[tuple, int] = field(default_factory=dict)
    #: The same, for padding after the function's last line.
    gaps_after: dict[tuple, int] = field(default_factory=dict)
    gap_color: HighlightColor | None = None


def build_paint(side_lines: list, statuses: list, colors: dict, gap_color) -> Paint:
    """A `Paint` from one side's column of aligned rows.

    ``side_lines`` holds this side's line in each row, or ``None`` where the row
    is padding; ``statuses`` the row statuses. Padding is attached to the next
    real line. A line whose key repeats — a blank line, mostly — cannot carry
    it, since the layer could not tell which occurrence it belongs to and
    padding every one would push the sides further apart, so the padding goes
    after the last line that can.
    """

    keys = [line_key(line) if line is not None else None for line in side_lines]
    seen: dict[tuple, int] = {}
    for key in keys:
        if key is not None:
            seen[key] = seen.get(key, 0) + 1

    paint = Paint(gap_color=gap_color)
    pending = 0
    last_unique = None

    def place(before: tuple | None) -> None:
        # Before the next line when it is unambiguous, else after the last
        # line that was: slightly off where it lands, but never lost, and
        # losing it is what shifts every row below it.
        if before is not None and seen[before] == 1:
            paint.gaps_before[before] = paint.gaps_before.get(before, 0) + pending
        elif last_unique is not None:
            paint.gaps_after[last_unique] = paint.gaps_after.get(last_unique, 0) + pending

    for key, status in zip(keys, statuses, strict=True):
        if key is None:
            pending += 1
            continue
        if pending:
            place(key)
        pending = 0
        if seen[key] == 1:
            last_unique = key
        color = colors.get(status)
        paint.colors.setdefault(key, color)
        if color is not None and key[0] is not None:
            paint.by_address.setdefault(key[0], color)
    if pending:
        place(None)
    return paint


_lock = threading.Lock()
#: (view, function start) -> Paint. Keyed by the address of the core object,
#: which every Python wrapper of one view shares.
_paints: dict[tuple[int | None, int], Paint] = {}


def _view_key(bv) -> int | None:
    handle = getattr(bv, "handle", None)
    if handle is None:
        return None
    return ctypes.cast(handle, ctypes.c_void_p).value


#: Painted functions whose lines have actually come through the layer, which is
#: the only way to tell whether a widget has it switched on: the UI keeps that
#: state itself and offers no getter.
_applied: set[tuple[int | None, int]] = set()


def set_paint(bv, func, paint: Paint | None) -> None:
    key = (_view_key(bv), func.start)
    with _lock:
        if paint is None:
            _paints.pop(key, None)
        else:
            _paints[key] = paint
        _applied.discard(key)


def was_applied(bv, func) -> bool:
    with _lock:
        return (_view_key(bv), func.start) in _applied


def clear_paints() -> None:
    with _lock:
        _paints.clear()


def _paint_for(func) -> Paint | None:
    if func is None:
        return None
    with _lock:
        return _paints.get((_view_key(func.view), func.start))


class DiffRenderLayer(RenderLayer):
    name = NAME
    # Enabled everywhere rather than switched on per widget: where the UI keeps
    # that switch is undocumented, and toggling it for the second pane turned
    # it off for that pane. Scoping comes from the registry instead — only a
    # function the diff is showing is ever touched.
    default_enable_state = (
        RenderLayerDefaultEnableState.EnabledByDefaultRenderLayerDefaultEnableState
    )

    def apply_to_linear_view_object(self, obj, prev, next, lines):
        # Every level arrives here, HLIL bodies and language representations
        # included, which the per-block hooks would each need handling apart.
        out = []
        for line in lines:
            paint = _paint_for(line.function)
            if paint is None or line.contents is None:
                out.append(line)
                continue
            with _lock:
                _applied.add((_view_key(line.function.view), line.function.start))
            key = line_key(line)
            out.extend(
                _gap_line(line, paint.gap_color) for _ in range(paint.gaps_before.get(key, 0))
            )
            if key in paint.colors:
                color = paint.colors[key]
            else:
                color = paint.by_address.get(key[0]) if key[0] is not None else None
            if color is not None:
                line.contents.highlight = color
            out.append(line)
            out.extend(
                _gap_line(line, paint.gap_color) for _ in range(paint.gaps_after.get(key, 0))
            )
        return out


def _gap_line(after, color) -> LinearDisassemblyLine:
    """A blank line standing opposite a line only the other side has."""

    contents = DisassemblyTextLine(
        [InstructionTextToken(InstructionTextTokenType.TextToken, " ")],
        address=getattr(after.contents, "address", None),
        color=color,
    )
    return LinearDisassemblyLine(
        LinearDisassemblyLineType.CodeDisassemblyLineType,
        after.function,
        after.block,
        contents,
        after.view,
    )


_registered = False


def register() -> None:
    """Register the layer once."""

    global _registered
    if not _registered:
        DiffRenderLayer.register()
        _registered = True


def is_registered() -> bool:
    return _registered
