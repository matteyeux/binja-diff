# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The plugin's persisted preferences, in one place.

Everything the reader arranges — the view each tab shows, whether the cursors
are synced, where the splitters sit, how wide the table's columns are — is
remembered here, under one QSettings scope, so a new diff and a new session
look the way the last one was left.
"""

from __future__ import annotations

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import QByteArray, QSettings

_ORGANIZATION = _APPLICATION = "binja-diff"


def settings() -> QSettings:
    return QSettings(_ORGANIZATION, _APPLICATION)


def _save_state(widget, key: str) -> None:
    """Store ``widget.saveState()`` under ``key``, unless the widget is gone.

    The signals this answers also fire while a widget tree is being torn down
    — a header's sections are resized as its table shrinks to nothing — and by
    then the Python wrapper is invalid: ``saveState`` raises rather than
    returning, and the traceback lands in the log on every tab close.
    """

    try:
        import shiboken6

        if not shiboken6.isValid(widget):
            return
    except ImportError:
        pass
    try:
        state = widget.saveState()
    except RuntimeError:
        return
    settings().setValue(f"layout/{key}", state)


def remember_splitter(splitter, key: str) -> None:
    """Restore a splitter's sizes from ``key`` and save them whenever they move.

    Restored only when something was saved: a fresh install keeps the layout's
    own proportions, which the caller has already set.
    """

    saved = settings().value(f"layout/{key}")
    if isinstance(saved, QByteArray) and not saved.isEmpty():
        splitter.restoreState(saved)
    splitter.splitterMoved.connect(lambda _pos, _index: _save_state(splitter, key))


def remember_header(header, key: str) -> None:
    """Restore a header view's column widths and sort indicator, and save them
    as they change. Called after sorting is enabled, so the restored indicator
    is the one the table then sorts by."""

    saved = settings().value(f"layout/{key}")
    if isinstance(saved, QByteArray) and not saved.isEmpty():
        header.restoreState(saved)
    header.sectionResized.connect(lambda *_args: _save_state(header, key))
    header.sortIndicatorChanged.connect(lambda *_args: _save_state(header, key))
