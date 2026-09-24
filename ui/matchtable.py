# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The function match table driving the rest of the diff view."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from functools import partial

import binaryninjaui  # noqa: F401  (must precede PySide6)
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..core import align
from ..core.align import BlockStatus, FunctionStatus, LineStatus
from ..core.engine import DiffResult
from . import theme
from .background import BatchRunner


class RowKind(str, Enum):
    MATCHED = "matched"
    PRIMARY_ONLY = "primary only"
    SECONDARY_ONLY = "secondary only"


@dataclass
class MatchRow:
    kind: RowKind
    primary_addr: int | None
    primary_name: str
    secondary_addr: int | None
    secondary_name: str
    similarity: float
    confidence: float

    @property
    def is_matched(self) -> bool:
        return self.kind is RowKind.MATCHED


#: The IL level the Status column describes, and the one its tooltip explains.
_LEVEL = "Disassembly"

#: Differing lines shown in a status tooltip before it is truncated.
_EXPLAIN_LINES = 6


_COLUMNS = (
    ("Primary", 200),
    ("Address", 100),
    ("Secondary", 200),
    ("Address", 100),
    ("Similarity", 90),
    ("Confidence", 90),
    ("Status", 185),
)


#: What the numeric columns actually measure. The similarity in particular
#: invites a reading it does not support: QBinDiff computes it as a MinHash
#: over basic blocks, one shingle per block holding that block's mnemonics, so
#: a single-block function scores 1.0 or 0.0 and nothing in between. A function
#: whose one block gained an instruction reads 0.000 while being the same code
#: — which is what the Status column is for.
_HEADER_TOOLTIPS = {
    4: (
        "How much of the function is the same code, counted per line: lines that\n"
        "are identical or differ only in an operand's spelling, over the longer\n"
        "side. Computed from the same comparison the panes draw."
    ),
    5: (
        "Belief-propagation confidence among the matcher's candidates. A high value"
        " does not prove the functions correspond; check the code similarity and status."
    ),
    6: "How the code compares, line by line. Hover a cell for the differing lines.",
}


#: Status column sort order, most interesting first. Rows not classified yet
#: sort after every verdict, whichever way the column is sorted.
_STATUS_RANK = {
    FunctionStatus.CHANGED: 0,
    FunctionStatus.UNKNOWN: 1,
    FunctionStatus.MINOR: 2,
    FunctionStatus.IDENTICAL: 3,
}
_UNCLASSIFIED_RANK = len(_STATUS_RANK)


def _classify_off_thread(result: DiffResult, key: tuple[int, int]):
    """(status, line similarity) for one pair, or None. Runs on a worker thread."""

    primary = result.primary_bv.get_function_at(key[0])
    secondary = result.secondary_bv.get_function_at(key[1])
    if primary is None or secondary is None:
        return None
    status, rows = align.classify_pair(primary, secondary, _LEVEL)
    if status is None:
        return None
    return (
        status,
        align.text_similarity(rows) if rows else None,
        align.edit_pattern(rows) if status is FunctionStatus.CHANGED else None,
    )


def _status_color(status: FunctionStatus | None):
    """Row tint. Matches the colour the text panes give the same distinction."""

    if status is FunctionStatus.IDENTICAL:
        return theme.block_color(BlockStatus.IDENTICAL)
    if status is FunctionStatus.MINOR:
        return theme.line_color(LineStatus.MINOR)
    if status is FunctionStatus.CHANGED:
        return theme.block_color(BlockStatus.CHANGED)
    # Unclassified or still unknown: leave the row alone rather than guess.
    return None


class MatchTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[MatchRow] = []
        self._result: DiffResult | None = None
        #: Function statuses, kept for as long as the result stands. Filled by
        #: the background pass, and by painting for rows it has not reached:
        #: classifying every pair *before* showing the table would mean
        #: disassembling both binaries in full first.
        self._status: dict[tuple[int, int], FunctionStatus] = {}
        #: Per-pair line similarity, filled by the same pass as the status.
        self._same: dict[tuple[int, int], float] = {}
        #: Repeated edits remain separate rows; this only annotates exact
        #: repetitions of a conservative, binary-agnostic fingerprint.
        self._patterns: dict[tuple[int, int], str] = {}
        self._pattern_counts: Counter[str] = Counter()
        #: Tooltip text per pair, filled on hover. See explain().
        self._explained: dict[tuple[int, int], str] = {}
        #: Classifies every matched pair in the background, so the summary and
        #: the status filter cover the whole table rather than what was painted.
        self.classifier = BatchRunner("Classifying matched functions")
        #: Called on the UI thread whenever statuses land, and once when done.
        self.on_progress = None

    def _classify(self, row: MatchRow):
        """(status, line similarity) for a pair, both from one comparison.

        Computed on first paint and cached for as long as the result stands.
        Classifying every pair up front would mean rendering both binaries
        before the table could appear; Qt only asks about the rows on screen.
        """

        if not row.is_matched or self._result is None:
            return None, None
        if row.primary_addr is None or row.secondary_addr is None:
            return None, None
        key = (row.primary_addr, row.secondary_addr)
        cached = self._status.get(key)
        if cached is not None:
            return cached, self._same.get(key)

        primary = self._result.primary_bv.get_function_at(row.primary_addr)
        secondary = self._result.secondary_bv.get_function_at(row.secondary_addr)
        if primary is None or secondary is None:
            return None, None
        try:
            status, rows = align.classify_pair(primary, secondary, _LEVEL)
        except Exception:
            # Painting must not fail over a function that will not render.
            status, rows = align.FunctionStatus.UNKNOWN, []
        if status is None:
            # Not drawn yet. Leave the cell blank and ask again on the next
            # repaint rather than record a verdict taken from half a function.
            return None, None
        self._remember(
            key,
            status,
            align.text_similarity(rows) if rows else None,
            align.edit_pattern(rows) if status is FunctionStatus.CHANGED else None,
        )
        return status, self._same.get(key)

    def _remember(
        self,
        key: tuple[int, int],
        status: FunctionStatus,
        same: float | None,
        pattern: str | None,
    ) -> None:
        self._status.setdefault(key, status)
        if same is not None:
            self._same.setdefault(key, same)
        if pattern is not None and key not in self._patterns:
            self._patterns[key] = pattern
            self._pattern_counts[pattern] += 1

    def pattern_count_of(self, row: MatchRow) -> int:
        if row.primary_addr is None or row.secondary_addr is None:
            return 0
        pattern = self._patterns.get((row.primary_addr, row.secondary_addr))
        return self._pattern_counts[pattern] if pattern is not None else 0

    def status_of(self, row: MatchRow) -> FunctionStatus | None:
        """How the pair actually compares, or None if it cannot be worked out."""

        return self._classify(row)[0]

    def line_similarity_of(self, row: MatchRow) -> float | None:
        """How much of the pair is the same code. None while unclassified.

        Deliberately not QBinDiff's similarity, which is a MinHash over whole
        basic blocks: a one-block function that gained an instruction shares no
        shingle with its own previous build and scores 0.000. That number is
        kept in the result (and in a saved diff) but is not what the table
        shows, because it answers a question nobody asked of this column.
        """

        return self._classify(row)[1]

    def explain(self, row: MatchRow) -> str:
        """The lines behind a row's status, for its tooltip.

        Computed only when the user hovers, and cached. A status that
        disagrees with what the panes show is otherwise impossible to
        investigate without a debugger.
        """

        if not row.is_matched or self._result is None:
            return ""
        if row.primary_addr is None or row.secondary_addr is None:
            return ""
        key = (row.primary_addr, row.secondary_addr)
        cached = self._explained.get(key)
        if cached is not None:
            return cached

        primary = self._result.primary_bv.get_function_at(row.primary_addr)
        secondary = self._result.secondary_bv.get_function_at(row.secondary_addr)
        if primary is None or secondary is None:
            return ""
        try:
            _status, rows = align.classify_pair(primary, secondary, _LEVEL)
        except Exception as exc:
            return f"could not render this pair: {exc}"

        differing = [aligned for aligned in rows if aligned.status.is_difference]
        if not differing:
            text = "no differing instructions"
        else:
            lines = [f"{len(differing)} differing line(s):"]
            for aligned in differing[:_EXPLAIN_LINES]:
                lines.append(f"{aligned.status.marker} {aligned.status.value}")
                lines.append(f"    {aligned.left if aligned.left is not None else '-'}")
                lines.append(f"    {aligned.right if aligned.right is not None else '-'}")
            if len(differing) > _EXPLAIN_LINES:
                lines.append(f"... and {len(differing) - _EXPLAIN_LINES} more")
            text = "\n".join(lines)
        self._explained[key] = text
        return text

    def cached_status(self, row: MatchRow) -> FunctionStatus | None:
        """The status if it is already known, without computing it.

        What sorting and filtering use: both ask about every row at once, and
        computing it there would classify the whole table on the UI thread. The
        background pass fills this in instead.
        """

        if row.primary_addr is None or row.secondary_addr is None:
            return None
        return self._status.get((row.primary_addr, row.secondary_addr))

    def cached_review(self, row: MatchRow) -> bool:
        """Whether completed classification found little evidence for the pair."""

        if not row.is_matched or row.primary_addr is None or row.secondary_addr is None:
            return False
        same = self._same.get((row.primary_addr, row.secondary_addr))
        return align.pairing_needs_review(row.similarity, same)

    def review_count(self) -> int:
        return sum(self.cached_review(row) for row in self._rows)

    def counts(self) -> dict[str, int]:
        """Rows per summary category; see `SummaryBar.CATEGORIES`."""

        counts = {name: 0 for name, _label, _tint in SummaryBar.CATEGORIES}
        for row in self._rows:
            if row.kind is RowKind.PRIMARY_ONLY:
                counts["primary"] += 1
            elif row.kind is RowKind.SECONDARY_ONLY:
                counts["secondary"] += 1
            else:
                status = self.cached_status(row)
                counts[status.value if status is not None else "pending"] += 1
        return counts

    def _start_classification(self) -> None:
        result = self._result
        if result is None:
            return
        keys = [
            (row.primary_addr, row.secondary_addr)
            for row in self._rows
            if row.is_matched and row.primary_addr is not None and row.secondary_addr is not None
        ]
        if not keys:
            return
        self.classifier.start(keys, partial(_classify_off_thread, result), self._absorb)

    def _absorb(self, batch: list, finished: bool) -> None:
        """Take a batch of verdicts from the background pass. UI thread."""

        for key, value in batch:
            if value is None:
                continue
            status, same, pattern = value
            self._remember(key, status, same, pattern)
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, len(_COLUMNS) - 1)
            )
        if self.on_progress is not None:
            self.on_progress(finished)

    def set_result(self, result: DiffResult | None) -> None:
        self.classifier.cancel()
        self.beginResetModel()
        self._rows = []
        self._result = result
        self._status.clear()
        self._same.clear()
        self._patterns.clear()
        self._pattern_counts.clear()
        self._explained.clear()
        if result is not None:
            for match in result.matches:
                self._rows.append(
                    MatchRow(
                        RowKind.MATCHED,
                        match.primary.addr,
                        match.primary.name,
                        match.secondary.addr,
                        match.secondary.name,
                        float(match.similarity),
                        float(match.confidence),
                    )
                )
            for func in result.primary_unmatched:
                self._rows.append(
                    MatchRow(RowKind.PRIMARY_ONLY, func.addr, func.name, None, "", 0.0, 0.0)
                )
            for func in result.secondary_unmatched:
                self._rows.append(
                    MatchRow(RowKind.SECONDARY_ONLY, None, "", func.addr, func.name, 0.0, 0.0)
                )
            self._rows.sort(key=lambda r: (-r.similarity, r.primary_addr or r.secondary_addr or 0))
        self.endResetModel()
        self._start_classification()

    def row_at(self, index: int) -> MatchRow | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.ToolTipRole:
            return _HEADER_TOOLTIPS.get(section)
        if role != Qt.DisplayRole:
            return None
        return _COLUMNS[section][0]

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return row.primary_name or "-"
            if column == 1:
                return f"{row.primary_addr:#x}" if row.primary_addr is not None else "-"
            if column == 2:
                return row.secondary_name or "-"
            if column == 3:
                return f"{row.secondary_addr:#x}" if row.secondary_addr is not None else "-"
            if column == 4:
                if not row.is_matched:
                    return "-"
                same = self.line_similarity_of(row)
                return f"{same * 100:.0f}%" if same is not None else ""
            if column == 5:
                return f"{row.confidence:.3f}" if row.kind is RowKind.MATCHED else "-"
            if column == 6:
                if not row.is_matched:
                    return row.kind.value
                status = self.status_of(row)
                warning = (
                    " · verify pair"
                    if align.pairing_needs_review(row.similarity, self.line_similarity_of(row))
                    else ""
                )
                if status is FunctionStatus.CHANGED:
                    count = self.pattern_count_of(row)
                    if count > 1:
                        return f"{status.value} · {count}\u00d7{warning}"
                return (status.value if status is not None else RowKind.MATCHED.value) + warning

        # Sort on the raw values so numeric columns order correctly. The status
        # column sorts on what has been classified already — sorting asks every
        # row at once, and classifying the whole table on the UI thread is
        # exactly what the laziness avoids — and the background pass fills in
        # the rest.
        if role == Qt.UserRole:
            # The Similarity column sorts on whatever has been classified already,
            # falling back to QBinDiff's score for rows nobody has looked at:
            # sorting asks every row at once, and classifying the whole table
            # on the UI thread is exactly what the laziness above avoids.
            same = self._same.get((row.primary_addr or 0, row.secondary_addr or 0))
            return (
                row.primary_name,
                row.primary_addr or 0,
                row.secondary_name,
                row.secondary_addr or 0,
                row.similarity if same is None else same,
                row.confidence,
                (
                    row.kind.value,
                    _STATUS_RANK.get(self.cached_status(row), _UNCLASSIFIED_RANK),
                    -row.similarity,
                ),
            )[column]

        if role == Qt.BackgroundRole:
            if not row.is_matched:
                return theme.block_color(BlockStatus.UNMATCHED)
            return _status_color(self.status_of(row))

        if role == Qt.ToolTipRole:
            if column == 6:
                detail = self.explain(row)
                if row.is_matched and align.pairing_needs_review(
                    row.similarity, self.line_similarity_of(row)
                ):
                    detail = (
                        "Pairing needs review: the graph matcher and line comparison both"
                        " found little common code. High matching confidence can still"
                        " occur when no good alternative was available.\n\n" + detail
                    )
                count = self.pattern_count_of(row)
                if count > 1:
                    detail += (
                        f"\n\nThe same non-minor edit pattern appears in {count} function pairs."
                        " Repetition does not establish cause or importance."
                    )
                return detail
            if column == 4 and row.is_matched:
                same = self.line_similarity_of(row)
                lines = [_HEADER_TOOLTIPS[4]]
                if same is not None:
                    lines.append(f"\nThis pair: {same * 100:.1f}% of its lines.")
                lines.append(f"QBinDiff's own score for it: {row.similarity:.3f}")
                return "\n".join(lines)

        if role == Qt.TextAlignmentRole and column in (1, 3, 4, 5):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None


class _FilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self._kind: RowKind | None = None
        self._status: FunctionStatus | None = None
        self._review = False
        self._text = ""

    @property
    def filters_on_status(self) -> bool:
        return self._status is not None or self._review

    def set_filter(
        self, kind: RowKind | None, status: FunctionStatus | None, review: bool = False
    ) -> None:
        self._kind = kind
        self._status = status
        self._review = review
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        row = model.row_at(source_row)
        if row is None:
            return False
        # Compared by value: these are str enums, and Qt hands one back from
        # itemData() as a plain str, which `is` silently never matches — the
        # filter simply emptied the table. Same trap as PortDirection.
        if self._kind is not None and row.kind != self._kind:
            return False
        if self._status is not None:
            if not row.is_matched:
                return False
            # Cached only: the background pass is classifying the table, and
            # rows join the filtered view as their verdicts land.
            if model.cached_status(row) != self._status:
                return False
        if self._review and not model.cached_review(row):
            return False
        if self._text:
            haystack = f"{row.primary_name} {row.secondary_name}".lower()
            if self._text not in haystack:
                return False
        return True


class SummaryBar(QWidget):
    """The whole diff in one bar: how many functions fall in each category.

    Filled in as the background pass classifies, with the unclassified rest
    shown as a neutral segment. Clicking a segment filters the table to it.
    """

    #: (key, label, reference tint). Keys are `FunctionStatus` values where
    #: there is one, so `MatchTableModel.counts` can index by status.
    CATEGORIES = (
        (FunctionStatus.CHANGED.value, "changed", theme.status_tint(LineStatus.CHANGED)),
        (FunctionStatus.MINOR.value, "offsets only", theme.status_tint(LineStatus.MINOR)),
        (FunctionStatus.IDENTICAL.value, "identical", QColor(70, 190, 90)),
        (FunctionStatus.UNKNOWN.value, "too large to classify", QColor(150, 150, 150)),
        ("primary", "only in primary", theme.status_tint(LineStatus.REMOVED)),
        ("secondary", "only in secondary", theme.status_tint(LineStatus.ADDED)),
        ("pending", "not classified yet", None),
    )

    HEIGHT = 10

    #: Emitted with a category key when its segment is clicked.
    categoryClicked = Signal(str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._counts: dict[str, int] = {}
        self._colors = {
            key: theme.solid(tint) if tint is not None else theme.background()
            for key, _label, tint in self.CATEGORIES
        }

    def set_counts(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self.update()

    def _segments(self) -> list[tuple[str, float, float]]:
        total = sum(self._counts.values())
        if not total:
            return []
        segments, x = [], 0.0
        for key, _label, _tint in self.CATEGORIES:
            count = self._counts.get(key, 0)
            if count:
                width = count / total * self.width()
                segments.append((key, x, width))
                x += width
        return segments

    def _segment_at(self, x: float) -> str | None:
        for key, start, width in self._segments():
            if start <= x < start + width:
                return key
        return None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.background())
        for key, start, width in self._segments():
            painter.fillRect(QRectF(start, 0, max(width, 1.0), self.height()), self._colors[key])

    def mouseMoveEvent(self, event) -> None:
        key = self._segment_at(event.position().x())
        labels = {k: label for k, label, _tint in self.CATEGORIES}
        if key is None:
            QToolTip.hideText()
            return
        total = sum(self._counts.values())
        count = self._counts.get(key, 0)
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"{count} {labels[key]} ({count / total * 100:.1f}%)",
            self,
        )

    def mousePressEvent(self, event) -> None:
        key = self._segment_at(event.position().x())
        if key is not None:
            self.categoryClicked.emit(key)


class MatchTable(QWidget):
    """Table of function matches. Emits the current row, and menu requests."""

    selectionChanged = Signal(object)
    #: Right-click, with the global position to pop a menu at. The rows it
    #: applies to come from selected_rows(); what may be done with them depends
    #: on the two views, which the diff view owns rather than this widget.
    contextMenuRequested = Signal(object)
    #: Double-click, with the row. What "open" means is the diff view's call.
    rowActivated = Signal(object)

    def __init__(self, parent: QWidget):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        controls = QHBoxLayout()
        # Item data is a plain string rather than the enum itself: Qt converts
        # a str enum to str on the way through QVariant, so storing the member
        # only invites the identity comparison that broke this filter before.
        self.filter_combo = QComboBox(self)
        self.filter_combo.addItem("All", None)
        for kind in RowKind:
            self.filter_combo.addItem(kind.value.title(), f"kind:{kind.value}")
        self.filter_combo.insertSeparator(self.filter_combo.count())
        for status in (
            FunctionStatus.IDENTICAL,
            FunctionStatus.MINOR,
            FunctionStatus.CHANGED,
            FunctionStatus.UNKNOWN,
        ):
            self.filter_combo.addItem(status.value.title(), f"status:{status.value}")
        self.filter_combo.addItem("Verify pair", "review:pair")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        controls.addWidget(QLabel("Show:", self))
        controls.addWidget(self.filter_combo)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Filter by function name...")
        self.search.textChanged.connect(lambda text: self.proxy.set_text(text))
        controls.addWidget(self.search, 1)

        self.stats = QLabel("", self)
        controls.addWidget(self.stats)
        layout.addLayout(controls)

        summary = QHBoxLayout()
        self.summary_bar = SummaryBar(self)
        self.summary_bar.categoryClicked.connect(self._filter_to_category)
        summary.addWidget(self.summary_bar, 1)
        self.summary_text = QLabel("", self)
        summary.addWidget(self.summary_text)
        layout.addLayout(summary)

        self.model = MatchTableModel(self)
        self.model.on_progress = self._on_classified
        self.proxy = _FilterProxy(self)
        # Statuses land in batches while the reader is looking at the table; a
        # proxy re-sorting on every batch would pull rows out from under them.
        # Filters are re-applied explicitly in _on_classified instead.
        self.proxy.setDynamicSortFilter(False)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Extended rather than single: porting a name is worth doing to a
        # hundred functions at once, and the panes follow the *current* row
        # regardless of how many are selected.
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._request_menu)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        header = self.table.horizontalHeader()
        for column, (_name, width) in enumerate(_COLUMNS):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.selectionModel().selectionChanged.connect(self._emit_selection)
        self.table.doubleClicked.connect(self._emit_activated)
        layout.addWidget(self.table, 1)

    def _apply_filter(self, index: int) -> None:
        """Split the chosen entry into a pairing filter and a status filter.

        A status filter shows what the background pass has classified so far,
        and grows as it goes; see _on_classified.
        """

        selected = self.filter_combo.itemData(index)
        kind = status = None
        review = False
        if isinstance(selected, str):
            group, _, value = selected.partition(":")
            if group == "kind":
                kind = RowKind(value)
            elif group == "status":
                status = FunctionStatus(value)
            elif group == "review":
                review = True
        self.proxy.set_filter(kind, status, review)

    def _filter_to_category(self, key: str) -> None:
        """Point the Show: combo at a summary-bar segment."""

        wanted = {
            "primary": f"kind:{RowKind.PRIMARY_ONLY.value}",
            "secondary": f"kind:{RowKind.SECONDARY_ONLY.value}",
            FunctionStatus.IDENTICAL.value: f"status:{FunctionStatus.IDENTICAL.value}",
            FunctionStatus.MINOR.value: f"status:{FunctionStatus.MINOR.value}",
            FunctionStatus.CHANGED.value: f"status:{FunctionStatus.CHANGED.value}",
            FunctionStatus.UNKNOWN.value: f"status:{FunctionStatus.UNKNOWN.value}",
        }.get(key)
        if wanted is None:
            return
        index = self.filter_combo.findData(wanted)
        if index >= 0:
            self.filter_combo.setCurrentIndex(index)

    def _on_classified(self, finished: bool) -> None:
        counts = self.model.counts()
        self.summary_bar.set_counts(counts)
        classified = sum(counts.values()) - counts["primary"] - counts["secondary"]
        pending = counts["pending"]
        parts = [
            f"{counts[FunctionStatus.CHANGED.value]} changed",
            f"{counts[FunctionStatus.MINOR.value]} offsets only",
            f"{counts[FunctionStatus.IDENTICAL.value]} identical",
        ]
        if pending and not finished:
            parts.append(f"classifying {classified - pending}/{classified}")
        review_count = self.model.review_count()
        if review_count:
            parts.append(f"{review_count} verify pair")
        self.summary_text.setText("   ".join(parts))
        if self.proxy.filters_on_status:
            self.proxy.invalidateFilter()

    def _emit_activated(self, index) -> None:
        if index.isValid():
            self.rowActivated.emit(self.model.row_at(self.proxy.mapToSource(index).row()))

    def _emit_selection(self, *_args) -> None:
        # The panes show one pair, so they follow the current row: with several
        # selected, "the one the user last touched" is the only sensible pick.
        index = self.table.currentIndex()
        if not index.isValid():
            self.selectionChanged.emit(None)
            return
        self.selectionChanged.emit(self.model.row_at(self.proxy.mapToSource(index).row()))

    def _request_menu(self, pos) -> None:
        """Ask for a menu, having first made sure the click is on a selection.

        Right-clicking a row outside the selection selects it, which is what
        every other table does; without it the menu would silently act on rows
        the user cannot see any more.
        """

        index = self.table.indexAt(pos)
        if index.isValid() and index not in self.table.selectionModel().selectedRows(
            index.column()
        ):
            self.table.setCurrentIndex(index)
            self.table.selectRow(index.row())
        if self.selected_rows():
            self.contextMenuRequested.emit(self.table.viewport().mapToGlobal(pos))

    def selected_rows(self) -> list[MatchRow]:
        """Every selected row, in the order the table shows them."""

        rows = []
        for index in self.table.selectionModel().selectedRows():
            row = self.model.row_at(self.proxy.mapToSource(index).row())
            if row is not None:
                rows.append(row)
        return rows

    def set_result(self, result: DiffResult | None) -> None:
        self.model.set_result(result)
        self._on_classified(False)
        if result is None:
            self.stats.setText("")
            self.stats.setToolTip("")
            self.summary_text.setText("")
            return
        self.stats.setText(
            f"{result.nb_match} matched   "
            f"{result.nb_unmatched_primary} primary-only   "
            f"{result.nb_unmatched_secondary} secondary-only   "
            f"graph score {result.similarity:.3f}"
        )
        self.stats.setToolTip(
            "QBinDiff's aggregate graph score. It does not measure matching accuracy;"
            " review pairs marked 'verify pair'."
        )
        if self.proxy.rowCount() > 0:
            self.table.selectRow(0)
