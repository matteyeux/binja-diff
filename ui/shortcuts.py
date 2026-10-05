# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Keyboard shortcuts the three diff tabs share.

F8 and Shift+F8 step through the changes, the way most diff tools do. Bound
with ``WidgetWithChildrenShortcut`` so they fire only while the tab or one of
its panes has focus: a Binary Ninja tab next door keeps its own F8.
"""

from __future__ import annotations

from collections.abc import Callable

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QPushButton, QWidget

NEXT_CHANGE = "F8"
PREVIOUS_CHANGE = "Shift+F8"


def bind_change_navigation(
    widget: QWidget,
    go: Callable[[int], None],
    prev_button: QPushButton | None = None,
    next_button: QPushButton | None = None,
) -> None:
    """Bind the next/previous shortcuts on ``widget``, and say so on the buttons."""

    for keys, direction in ((NEXT_CHANGE, 1), (PREVIOUS_CHANGE, -1)):
        shortcut = QShortcut(QKeySequence(keys), widget)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda direction=direction: go(direction))
    if next_button is not None:
        next_button.setToolTip(f"Next differing line or block ({NEXT_CHANGE})")
    if prev_button is not None:
        prev_button.setToolTip(f"Previous differing line or block ({PREVIOUS_CHANGE})")
