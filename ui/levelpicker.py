# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The "View:" selector shared by the graph and linear tabs.

The choice is remembered across diffs and sessions: someone who reads HLIL or
Pseudo C reads it on every function, and a selector that falls back to
disassembly on each new diff has to be reset every time.
"""

from __future__ import annotations

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QComboBox, QWidget

from ..core.align import RenderLevel, available_levels

_ORGANIZATION = "binja-diff"
_APPLICATION = "binja-diff"


class LevelPicker(QComboBox):
    """A combo over `available_levels()`, persisting its choice under ``key``.

    The name is what is stored, not the index: which languages are offered
    depends on the plugins loaded, so an index would point at a different view
    on the next start.
    """

    def __init__(self, parent: QWidget, key: str):
        super().__init__(parent)
        self._key = f"views/{key}"
        self._levels = available_levels()
        self.addItems([level.name for level in self._levels])

        saved = QSettings(_ORGANIZATION, _APPLICATION).value(self._key)
        index = self.findText(saved) if isinstance(saved, str) else -1
        if index >= 0:
            self.setCurrentIndex(index)
        self.currentIndexChanged.connect(self._save)

    @property
    def level(self) -> RenderLevel:
        return self._levels[max(self.currentIndex(), 0)]

    def _save(self, _index: int) -> None:
        QSettings(_ORGANIZATION, _APPLICATION).setValue(self._key, self.level.name)
