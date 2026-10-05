# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The matching parameters, for the next diff this view runs.

The same knobs the CLI exposes, over the same ``DiffOptions``; the defaults
are the measured ones (see CLAUDE.md), so this is for the reader who knows
why they want something else — a lower sparsity on a small pair, a different
distance, an extra feature.
"""

from __future__ import annotations

from dataclasses import replace

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.engine import DISTANCES, EXTRA_FEATURE_KEYS, DiffOptions


class OptionsDialog(QDialog):
    def __init__(self, parent: QWidget, options: DiffOptions):
        super().__init__(parent)
        self.setWindowTitle("Matching options")
        self._defaults = DiffOptions()

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.sparsity = QDoubleSpinBox(self)
        self.sparsity.setRange(0.0, 1.0)
        self.sparsity.setSingleStep(0.05)
        self.sparsity.setDecimals(3)
        self.sparsity.setValue(options.sparsity_ratio)
        self.sparsity.setToolTip(
            "Share of the least likely candidate pairs discarded before matching.\n"
            "Lower is more accurate and slower."
        )
        form.addRow("Sparsity", self.sparsity)

        self.auto_sparsity = QCheckBox("Raise it automatically for large binaries", self)
        self.auto_sparsity.setChecked(options.auto_sparsity)
        form.addRow("", self.auto_sparsity)

        self.tradeoff = QDoubleSpinBox(self)
        self.tradeoff.setRange(0.0, 1.0)
        self.tradeoff.setSingleStep(0.05)
        self.tradeoff.setDecimals(2)
        self.tradeoff.setValue(options.tradeoff)
        self.tradeoff.setToolTip("1 trusts the function features alone, 0 the call graph alone.")
        form.addRow("Feature / structure tradeoff", self.tradeoff)

        self.maxiter = QSpinBox(self)
        self.maxiter.setRange(1, 100_000)
        self.maxiter.setValue(options.maxiter)
        self.maxiter.setToolTip("Belief propagation usually converges long before this.")
        form.addRow("Max iterations", self.maxiter)

        self.distance = QComboBox(self)
        self.distance.addItems(list(DISTANCES))
        index = self.distance.findText(options.distance)
        self.distance.setCurrentIndex(max(index, 0))
        form.addRow("Distance", self.distance)

        self.features = QLineEdit(self)
        self.features.setText(" ".join(options.features))
        self.features.setPlaceholderText(" ".join(EXTRA_FEATURE_KEYS))
        self.features.setToolTip(
            "Extra QBinDiff feature keys, space separated, on top of the default set.\n"
            f"Known extras: {', '.join(EXTRA_FEATURE_KEYS)}"
        )
        form.addRow("Extra features", self.features)
        layout.addLayout(form)

        note = QLabel(
            "Applies to the next diff started from this tab. The defaults were "
            "measured against symbol ground truth; see the documentation before "
            "trusting a change to improve the matching.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore_defaults)
        layout.addWidget(buttons)

    def _restore_defaults(self) -> None:
        defaults = self._defaults
        self.sparsity.setValue(defaults.sparsity_ratio)
        self.auto_sparsity.setChecked(defaults.auto_sparsity)
        self.tradeoff.setValue(defaults.tradeoff)
        self.maxiter.setValue(defaults.maxiter)
        self.distance.setCurrentIndex(max(self.distance.findText(defaults.distance), 0))
        self.features.setText(" ".join(defaults.features))

    def options(self, base: DiffOptions) -> DiffOptions:
        """``base`` with the dialog's values; what it does not show is kept."""

        return replace(
            base,
            sparsity_ratio=self.sparsity.value(),
            auto_sparsity=self.auto_sparsity.isChecked(),
            tradeoff=self.tradeoff.value(),
            maxiter=self.maxiter.value(),
            distance=self.distance.currentText(),
            features=tuple(self.features.text().split()),
        )
