# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The diff view: a full Binary Ninja tab hosting the whole workflow."""

from __future__ import annotations

import atexit
import os
from dataclasses import replace
from functools import partial

import binaryninjaui  # must precede PySide6; see ui/__init__
from binaryninja import execute_on_main_thread, log_error, log_warn
from binaryninjaui import UIActionHandler, View, ViewFrame, ViewType
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import persist, scope, symbols
from ..core.engine import DiffOptions, DiffResult, DiffTask, format_duration
from . import background
from .dropzone import FILE_FILTER, DropZone, dimmed, local_files
from .graphpane import GraphDiffTab
from .matchtable import MatchTable
from .progresspanel import ProgressPanel
from .scopedialog import ScopeDialog
from .textpane import TextDiffTab
from . import difflayer, nativelinear

#: Stack pages.
_PAGE_DROP, _PAGE_BUSY, _PAGE_RESULTS = 0, 1, 2

_SAVED_DIFF_FILTER = f"Saved diffs (*{persist.FILE_SUFFIX} *.json);;All files (*)"

#: Identifies the restore entry inside its menu, so it can be found again
#: rather than held onto. See _update_load_menu.
_RESTORE_ACTION = "binjaDiffRestoreAction"


#: Secondary views the plugin has opened and not yet closed. Binary Ninja holds
#: a lock on an open database, and a plugin that never closes one leaves that
#: lock behind: the file cannot be reopened even after Binary Ninja exits.
#: QWidget.destroyed covers a tab being closed, but not the process going away
#: — Qt tears the application down without destroying every widget — so the
#: views are tracked here and closed from the two shutdown hooks below.
_OPEN_SECONDARIES: set = set()


def _close_secondary(bv) -> None:
    """Close one view and forget it. Closing is what releases a `.bndb` lock."""

    if bv is None:
        return
    _OPEN_SECONDARIES.discard(bv)
    try:
        bv.file.close()
    except Exception:
        pass


def _stop_workers(workers) -> None:
    """Cancel background renders and wait for them, before a view is closed.

    They read the secondary view's functions, and closing a view under a thread
    that is still walking it crashes the process rather than raising.
    """

    for worker in workers:
        worker.cancel()
    for worker in workers:
        worker.wait()


def _release_holder(holder: dict, *_args) -> None:
    """Close whatever a view was holding, given only its holder.

    Deliberately takes a plain dict rather than the DiffView: this is what runs
    on ``QWidget.destroyed``, and a bound method of the widget being destroyed
    is exactly what PySide may never call — the Python wrapper is invalidated
    as the C++ object goes away. Nothing here touches the widget; the workers
    are plain Python objects, not Qt ones.
    """

    _stop_workers(holder.get("workers", ()))
    _close_secondary(holder.get("bv"))
    holder["bv"] = None


def _close_open_secondaries() -> None:
    background.stop_all()
    for bv in list(_OPEN_SECONDARIES):
        _close_secondary(bv)
    _OPEN_SECONDARIES.clear()


def _install_shutdown_hooks() -> None:
    """Close what we hold when the process ends, however it ends.

    Both hooks, because neither is guaranteed: aboutToQuit does not fire if the
    interpreter is torn down without the Qt event loop stopping cleanly, and
    atexit does not fire if the host exits through Qt alone.
    """

    atexit.register(_close_open_secondaries)
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(_close_open_secondaries)


_install_shutdown_hooks()


class DiffView(QWidget, View):
    """Side-by-side binary diff, selectable from the view dropdown."""

    def __init__(self, parent, data):
        QWidget.__init__(self, parent)
        View.__init__(self)
        View.setBinaryDataNavigable(self, False)
        self.setupView(self)

        self.data = data
        self.current_offset = data.entry_point if data is not None else 0
        self.secondary_bv = None
        self.result: DiffResult | None = None
        self._task: DiffTask | None = None
        self._owns_secondary = False
        self._selected_row = None
        #: The running symbol port, if any. Held so a second one cannot start
        #: on top of it, and so the thread is not collected mid-rename.
        self._port_task = None
        #: Which file that port is writing into, for the report it shows
        #: afterwards — the task itself carries only addresses.
        self._port_target_name = ""
        self._last_progress: tuple[str, int] | None = None
        self._options = DiffOptions()
        #: Kext / SEP module to diff, chosen when the primary is a container.
        self._region_name: str | None = None

        self.actionHandler = UIActionHandler()
        self.actionHandler.setupActionHandler(self)

        self.setAcceptDrops(True)
        #: What this view has open, reachable without the widget. See
        #: _release_holder for why the destroyed handler must not touch self.
        self._owned: dict = {"bv": None}
        # shiboken objects never see __del__, so release the secondary here.
        self.destroyed.connect(partial(_release_holder, self._owned))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(self._build_header())

        self.stack = QStackedWidget(self)
        self.dropzone = DropZone(self.stack, self._primary_name())
        self.dropzone.fileChosen.connect(self.start_diff)
        self.dropzone.restoreRequested.connect(self.restore_from_database)
        self._refresh_saved_offer()
        self.busy = ProgressPanel(self.stack)
        self.busy.cancelled.connect(self.cancel)
        self.stack.addWidget(self.dropzone)
        self.stack.addWidget(self.busy)
        self.stack.addWidget(self._build_results())
        layout.addWidget(self.stack, 1)
        self._owned["workers"] = [
            self.graph_tab.renderer,
            self.text_tab.renderer,
            self.table.model.classifier,
        ]

    # -- construction ------------------------------------------------------

    def _build_linear_tab(self):
        """The native linear view where this Binary Ninja can host it.

        The text rendering stays as the fallback: it has none of the native
        view's interaction, but it needs nothing beyond PySide.
        """

        if nativelinear.available() and difflayer.is_registered():
            try:
                return nativelinear.NativeLinearTab(self.tabs)
            except Exception as exc:
                log_warn(f"Native linear diff unavailable, using text: {exc}", "QBinDiff")
        return TextDiffTab(self.tabs)

    def _release_linear_views(self) -> None:
        release = getattr(self.text_tab, "release_views", None)
        if release is not None:
            release()

    def _primary_name(self) -> str | None:
        return self.data.file.filename if self.data is not None else None

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(4, 2, 4, 2)
        header.setSpacing(8)

        self.primary_label = QLabel(f"Primary: {self._primary_name() or '?'}", self)
        self.primary_label.setTextFormat(Qt.PlainText)
        header.addWidget(self.primary_label)

        # A separator; without one the two paths run together into one string.
        header.addWidget(dimmed(QLabel("\u2194", self)))

        self.secondary_label = QLabel("Secondary: none", self)
        self.secondary_label.setTextFormat(Qt.PlainText)
        header.addWidget(self.secondary_label)

        header.addStretch(1)

        # Progress lives in the centered busy panel, not up here.
        self.status = QLabel("", self)
        self.status.setTextFormat(Qt.PlainText)
        header.addWidget(self.status)

        self.open_button = QPushButton("Open secondary...", self)
        self.open_button.clicked.connect(self.dropzone_browse)
        header.addWidget(self.open_button)

        self.close_button = QPushButton("Close secondary", self)
        self.close_button.setEnabled(False)
        self.close_button.setToolTip(
            "Discard the diff and release the second binary. Binary Ninja locks an "
            "open database, so a .bndb diffed here cannot be opened elsewhere until "
            "this releases it."
        )
        self.close_button.clicked.connect(self.close_secondary)
        header.addWidget(self.close_button)

        self.save_button = QPushButton("Save diff", self)
        self.save_button.setEnabled(False)
        save_menu = QMenu(self.save_button)
        save_menu.addAction("Save into this database", self.save_to_database)
        save_menu.addAction("Export to file...", self.export_to_file)
        self.save_button.setMenu(save_menu)
        header.addWidget(self.save_button)

        self.load_button = QPushButton("Load diff", self)
        load_menu = QMenu(self.load_button)
        load_menu.addAction("Restore from this database", self.restore_from_database).setObjectName(
            _RESTORE_ACTION
        )
        load_menu.addAction("Import from file...", self.import_from_file)
        load_menu.aboutToShow.connect(self._update_load_menu)
        self.load_button.setMenu(load_menu)
        header.addWidget(self.load_button)

        return header

    def _update_load_menu(self) -> None:
        """Grey out "restore" when the database holds nothing.

        The action is found again on every show instead of being kept in an
        attribute. Binary Ninja destroys and rebuilds a View's widgets as tabs
        and view types change — and reloading the plugin does the same — so a
        shiboken wrapper held across that raises "Internal C++ object already
        deleted" the next time it is touched, from a lambda deep in Qt where it
        is anything but obvious. Whether a diff is stored has to be re-checked
        on every show anyway, since the user may have just saved one.
        """

        menu = self.sender()
        if not isinstance(menu, QMenu):
            return
        enabled = self.data is not None and persist.has_saved_diff(self.data)
        for action in menu.actions():
            if action.objectName() == _RESTORE_ACTION:
                action.setEnabled(enabled)

    def _build_results(self) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Vertical, container)

        self.table = MatchTable(splitter)
        self.table.selectionChanged.connect(self._on_row_selected)
        self.table.contextMenuRequested.connect(self._show_match_menu)
        self.table.rowActivated.connect(self.navigate_to_primary)
        splitter.addWidget(self.table)

        self.tabs = QTabWidget(splitter)
        # One tab per layout; each picks its IL level or language itself.
        self.graph_tab = GraphDiffTab(self.tabs)
        self.tabs.addTab(self.graph_tab, "Graph")
        self.text_tab = self._build_linear_tab()
        self.tabs.addTab(self.text_tab, "Linear")
        self.tabs.currentChanged.connect(lambda _index: self._refresh_current_tab())
        splitter.addWidget(self.tabs)

        splitter.setSizes([250, 650])
        layout.addWidget(splitter)
        return container

    # -- diff lifecycle ----------------------------------------------------

    def dropzone_browse(self) -> None:
        self.dropzone.browse()

    def start_diff(self, path_or_bv) -> None:
        if self._task is not None:
            QMessageBox.information(self, "Binary diff", "A diff is already running.")
            return
        if self.data is None:
            return

        # Asked before anything is loaded: the region list comes from the
        # primary, and a name identifies the same part on the other side.
        if not self._choose_region():
            return

        # _release_secondary closes the previous secondary BinaryView, which the
        # old result and panes still point at, so they have to go with it.
        self._release_secondary()
        self._clear_results()

        label = path_or_bv if isinstance(path_or_bv, str) else path_or_bv.file.filename
        self.secondary_label.setText(f"Secondary: {label}")
        self.status.setText("")
        self._last_progress = None

        self.busy.begin(self._primary_name(), label)
        self.stack.setCurrentIndex(_PAGE_BUSY)

        self._start_task(
            DiffTask(
                self.data,
                path_or_bv,
                on_done=lambda result: execute_on_main_thread(lambda: self._on_done(result)),
                on_error=lambda message: execute_on_main_thread(lambda: self._on_error(message)),
                options=self._options,
                on_progress=self._post_progress,
                on_cancelled=lambda: execute_on_main_thread(self._on_cancelled),
                region_name=self._region_name,
            )
        )

    def _choose_region(self) -> bool:
        """Offer the parts of a container to diff. False if the user cancelled.

        A kernelcache holds hundreds of kexts and matching is quadratic, so
        diffing the whole thing is not a slower version of the same job — it is
        one that does not finish.
        """

        self._region_name = None
        if self.data is None:
            return True
        regions = scope.available_regions(self.data)
        if not regions:
            return True

        container = "kernelcache" if self.data.view_type == scope.KERNELCACHE_VIEW else "SEP image"
        dialog = ScopeDialog(self, regions, container)
        if dialog.exec() != QDialog.Accepted:
            return False
        self._region_name = dialog.region_name()
        return True

    def _start_task(self, task) -> None:
        self._task = task
        self._owns_secondary = task.owns_secondary
        # Nothing that would start a second run stays clickable: start_diff and
        # _restore both refuse while one is going, and a dialog saying so is a
        # worse answer than a button that is plainly unavailable.
        self._set_sources_enabled(False)
        task.start()

    def _set_sources_enabled(self, enabled: bool) -> None:
        self.open_button.setEnabled(enabled)
        self.load_button.setEnabled(enabled)

    def _post_progress(self, label: str, fraction: float) -> None:
        """Called from the worker thread.

        Feature extraction reports over a thousand times; posting every one
        would swamp the main thread, so only whole-percent changes get through.
        The label is part of the key so phase changes are never swallowed.
        """

        state = (label, int(fraction * 100))
        if state == self._last_progress:
            return
        self._last_progress = state
        execute_on_main_thread(lambda: self._set_progress(*state))

    def _set_progress(self, label: str, percent: int) -> None:
        self.busy.set_progress(label, percent)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _finish_task(self) -> None:
        self._task = None
        self.busy.finish()
        self._set_sources_enabled(True)

    def _on_done(self, result: DiffResult, note: str = "") -> None:
        self._finish_task()
        self.result = result
        self.secondary_bv = result.secondary_bv
        if self._owns_secondary:
            _OPEN_SECONDARIES.add(result.secondary_bv)
            self._owned["bv"] = result.secondary_bv
        self.secondary_label.setText(f"Secondary: {result.secondary_bv.file.filename}")
        self.status.setText(
            f"{note}similarity {result.similarity:.3f} in {format_duration(result.duration)}"
        )
        # The header has room for one number; the breakdown is a hover away,
        # and the same lines are in the log for anyone who wants to keep them.
        self.status.setToolTip(result.timing_report)
        self.save_button.setEnabled(True)
        self.close_button.setEnabled(self._owns_secondary)

        self.graph_tab.set_views(result.primary_bv, result.secondary_bv)
        set_views = getattr(self.text_tab, "set_views", None)
        if set_views is not None:
            set_views(result.primary_bv, result.secondary_bv)
        self.table.set_result(result)
        self.stack.setCurrentIndex(_PAGE_RESULTS)

    def _clear_results(self) -> None:
        self.result = None
        self._selected_row = None
        self.save_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.table.set_result(None)
        self.graph_tab.clear()
        self.text_tab.clear()
        self._release_linear_views()

    def _reset_to_dropzone(self, status: str) -> None:
        self._finish_task()
        # The task closed whatever it had opened; drop the reference with the
        # ownership flag, or _release_secondary would later close it twice.
        if self.secondary_bv is not None and self._owns_secondary:
            _OPEN_SECONDARIES.discard(self.secondary_bv)
        self._owned["bv"] = None
        self.secondary_bv = None
        self._owns_secondary = False
        self.status.setText(status)
        self.status.setToolTip("")
        self.secondary_label.setText("Secondary: none")
        self.stack.setCurrentIndex(_PAGE_DROP)

    def _on_cancelled(self) -> None:
        # The task already closed any secondary it had loaded.
        self._reset_to_dropzone("cancelled")

    def _on_error(self, message: str) -> None:
        self._reset_to_dropzone("failed")
        log_error(f"Diff failed: {message}", "QBinDiff")
        QMessageBox.critical(self, "Binary diff failed", message)

    def _release_secondary(self) -> None:
        """Close a BinaryView we loaded ourselves; the UI owns the others.

        Closing it is what releases Binary Ninja's lock on a `.bndb`, so this
        has to happen whenever the plugin stops needing the view — not only
        when someone gets round to destroying the widget.
        """

        if self.secondary_bv is not None and self._owns_secondary:
            _stop_workers(self._owned.get("workers", ()))
            # A native linear view listens to the view it holds.
            self._release_linear_views()
            _close_secondary(self.secondary_bv)
        self._owned["bv"] = None
        self.secondary_bv = None
        self._owns_secondary = False

    # -- porting symbols ---------------------------------------------------

    def close_secondary(self) -> None:
        """Release the second binary, and with it any lock on its database.

        The diff goes with it: every pane points at that view. Worth having as
        its own action because the alternative is closing the whole tab, and a
        `.bndb` held open here cannot be opened anywhere else.
        """

        if self._task is not None or self.secondary_bv is None:
            return
        name = os.path.basename(self.secondary_bv.file.filename or "")
        self._release_secondary()
        self._clear_results()
        self.secondary_label.setText("Secondary: none")
        self.status.setText(f"released {name}" if name else "secondary released")
        self.status.setToolTip("")
        self.stack.setCurrentIndex(_PAGE_DROP)

    def navigate_to_primary(self, row) -> None:
        """Show a row's primary function in Binary Ninja's own views.

        Only the primary: it is the view this tab belongs to. The secondary is
        not open in any tab, so there is nothing to navigate to on that side.
        """

        if row is None or row.primary_addr is None or self.data is None:
            return
        frame = ViewFrame.viewFrameForWidget(self)
        if frame is None:
            return
        try:
            kind = "Graph" if frame.isGraphViewPreferred() else "Linear"
        except Exception:
            kind = "Graph"
        frame.navigate(f"{kind}:{frame.getCurrentDataType()}", row.primary_addr)

    def _show_match_menu(self, position) -> None:
        """Offer navigation to the current row, and porting names for the selection."""

        if self.result is None:
            return
        menu = QMenu(self)
        menu.setToolTipsVisible(True)

        current = self._selected_row
        if current is not None and current.primary_addr is not None:
            menu.addAction(
                "Go to primary function", lambda: self.navigate_to_primary(current)
            ).setToolTip("Leaves the diff; switch back with the view selector")
            menu.addSeparator()

        rows = [row for row in self.table.selected_rows() if row.is_matched]
        if not rows:
            menu.addAction("No matched function selected").setEnabled(False)
            menu.exec(position)
            return

        primary = os.path.basename(self._primary_name() or "primary")
        secondary = os.path.basename(self.result.secondary_bv.file.filename or "secondary")

        to_primary = menu.addAction(f"Port symbol(s) to primary  ({primary})")
        to_primary.triggered.connect(
            lambda: self._port_selected(symbols.PortDirection.TO_PRIMARY, rows)
        )
        to_secondary = menu.addAction(f"Port symbol(s) to secondary  ({secondary})")
        to_secondary.triggered.connect(
            lambda: self._port_selected(symbols.PortDirection.TO_SECONDARY, rows)
        )
        if not symbols.is_persistent(self.result.secondary_bv):
            # Renames there would live only in this session: the secondary is
            # not open in a tab, so nothing would ever offer to save it.
            to_secondary.setEnabled(False)
            to_secondary.setToolTip("The secondary has no database to save the names into")
        if self._task is not None or self._port_task is not None:
            for action in (to_primary, to_secondary):
                action.setEnabled(False)
        menu.exec(position)

    def _port_selected(self, direction, rows) -> None:
        """Port the names of the selected pairs, one undo step for the batch.

        A hand-picked selection is an instruction, so the similarity floor that
        guards a whole-table port does not apply here: the user is looking at
        the pair. Names already on the receiving side are still kept unless
        they say otherwise, since those are usually theirs.
        """

        if self.result is None or self._task is not None or self._port_task is not None:
            return

        addresses = [row.primary_addr for row in rows if row.primary_addr is not None]
        options = symbols.PortOptions(direction=direction, min_similarity=0.0)
        plan = symbols.plan_port(self.result, options, addresses)

        if not plan.renames:
            named = plan.skipped.get(symbols.SkipReason.ALREADY_NAMED, 0)
            if named and self._confirm_overwrite(named):
                options = replace(options, overwrite=True)
                plan = symbols.plan_port(self.result, options, addresses)
            else:
                QMessageBox.information(
                    self, "Port symbols", f"Nothing to port.\n\n{plan.summary()}"
                )
                return

        self.status.setText("porting symbols...")
        target = symbols.target_view(self.result, direction)
        task = symbols.PortSymbolsTask(
            self.result,
            options,
            on_done=lambda plan, applied: execute_on_main_thread(
                lambda: self._on_ported(options, plan, applied)
            ),
            on_error=lambda message: execute_on_main_thread(lambda: self._on_port_failed(message)),
            # The secondary is nobody else's tab, so an unsaved rename there is
            # a rename that disappears when this view closes.
            save=direction == symbols.PortDirection.TO_SECONDARY,
            only=addresses,
        )
        self._port_target_name = os.path.basename(target.file.filename or "")
        self._port_task = task
        task.start()

    def _confirm_overwrite(self, count: int) -> bool:
        return (
            QMessageBox.question(
                self,
                "Port symbols",
                f"{count} of the selected function(s) already have a name on the "
                "receiving side.\n\nReplace them?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    def _on_ported(self, options, plan, applied: int) -> None:
        self._port_task = None
        self.status.setText(f"ported {applied} symbol(s)")
        if applied == 0:
            QMessageBox.information(self, "Port symbols", f"Nothing to port.\n\n{plan.summary()}")
            return

        target = symbols.target_view(self.result, options.direction)
        if options.direction == symbols.PortDirection.TO_SECONDARY:
            follow_up = f"Saved into {self._port_target_name}."
        elif symbols.is_persistent(target):
            follow_up = "Save the database to keep the new names."
        else:
            follow_up = (
                "This binary has no analysis database, so the names only live in this "
                "session. Save it as a .bndb to keep them."
            )
        QMessageBox.information(
            self,
            "Port symbols",
            f"Renamed {applied} function(s).\n\n{plan.summary()}\n\n"
            f"{follow_up}\nThe whole port is a single undo step.",
        )
        # The table shows the names captured when the diff ran; those on the
        # renamed side are now stale.
        symbols.refresh_names(self.result)
        self.table.set_result(self.result)

    def _on_port_failed(self, message: str) -> None:
        self._port_task = None
        self.status.setText("port failed")
        QMessageBox.critical(self, "Port symbols", message)

    # -- saving and restoring ----------------------------------------------

    def _saved_in_database(self) -> persist.SavedDiff | None:
        return persist.load_from_database(self.data) if self.data is not None else None

    def _refresh_saved_offer(self) -> None:
        saved = self._saved_in_database()
        self.dropzone.set_saved_diff(saved.summary if saved is not None else None)

    def _to_saved(self) -> persist.SavedDiff | None:
        if self.result is None:
            return None
        return persist.SavedDiff.from_result(self.result, self._options)

    def save_to_database(self) -> None:
        saved = self._to_saved()
        if saved is None:
            return
        try:
            persist.store_in_database(self.data, saved)
        except Exception as exc:
            QMessageBox.critical(self, "Binary diff", f"Could not save the diff: {exc}")
            return
        self._refresh_saved_offer()
        # Metadata is only written out when the database is, and a view that was
        # never saved as a .bndb has nowhere to write it at all. Saying so here
        # is the difference between a diff that survives a restart and one the
        # user believes survived.
        if persist.is_persistent(self.data):
            follow_up = "Save the analysis database to keep it across restarts."
        else:
            follow_up = (
                "This binary has no analysis database yet, so the diff only lives in "
                "this session. Use File > Save Analysis Database As... to create one."
            )
        QMessageBox.information(
            self, "Binary diff", f"Diff stored in the analysis database.\n\n{follow_up}"
        )

    def export_to_file(self) -> None:
        saved = self._to_saved()
        if saved is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export diff results", persist.default_filename(self.result), _SAVED_DIFF_FILTER
        )
        if not path:
            return
        try:
            persist.write_file(saved, path)
        except Exception as exc:
            QMessageBox.critical(self, "Binary diff", str(exc))
            return
        self.status.setText(f"exported to {os.path.basename(path)}")

    def restore_from_database(self) -> None:
        saved = self._saved_in_database()
        if saved is None:
            QMessageBox.information(
                self, "Binary diff", "This database does not contain a saved diff."
            )
            return
        self._restore(saved)

    def import_from_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import diff results", "", _SAVED_DIFF_FILTER
        )
        if not path:
            return
        try:
            saved = persist.read_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Binary diff", str(exc))
            return
        self._restore(saved)

    def _restore(self, saved: persist.SavedDiff) -> None:
        """Bring a saved diff back, re-opening the secondary binary it names."""

        if self._task is not None:
            QMessageBox.information(self, "Binary diff", "A diff is already running.")
            return
        if self.data is None:
            return

        drift = saved.primary.differences(self.data)
        if drift and not self._confirm_drift("primary", drift):
            return
        path = self._locate_secondary(saved)
        if path is None:
            return

        self._release_secondary()
        self._clear_results()
        self.secondary_label.setText(f"Secondary: {path}")
        self.status.setText("")
        self._last_progress = None

        # Only the loading half of the work is left, but that is the slow half.
        self.busy.begin(self._primary_name(), path, title="Restoring saved diff")
        self.stack.setCurrentIndex(_PAGE_BUSY)

        self._start_task(
            persist.RestoreTask(
                self.data,
                saved,
                path,
                on_done=lambda result: execute_on_main_thread(
                    lambda: self._on_done(result, note="restored, ")
                ),
                on_error=lambda message: execute_on_main_thread(lambda: self._on_error(message)),
                on_progress=self._post_progress,
                on_cancelled=lambda: execute_on_main_thread(self._on_cancelled),
            )
        )

    def _confirm_drift(self, side: str, drift: list[str]) -> bool:
        answer = QMessageBox.warning(
            self,
            "Binary diff",
            f"The {side} binary has changed since this diff was saved:\n\n"
            + "\n".join(f"  • {note}" for note in drift)
            + "\n\nA saved diff records addresses only, so matches may point at the "
            "wrong functions. Restore it anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _locate_secondary(self, saved: persist.SavedDiff) -> str | None:
        """The secondary binary's path, asking the user if it has moved."""

        path = saved.secondary.filename
        if os.path.isfile(path):
            return path
        answer = QMessageBox.question(
            self,
            "Binary diff",
            f"The secondary binary is no longer at:\n{path}\n\nLocate it?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return None
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Locate the secondary binary", os.path.dirname(path), FILE_FILTER
        )
        return chosen or None

    # -- selection ---------------------------------------------------------

    def _on_row_selected(self, row) -> None:
        self._selected_row = row
        self._refresh_current_tab()

    def _resolve_functions(self, row):
        if row is None or self.result is None:
            return None, None
        primary = (
            self.result.primary_bv.get_function_at(row.primary_addr)
            if row.primary_addr is not None
            else None
        )
        secondary = (
            self.result.secondary_bv.get_function_at(row.secondary_addr)
            if row.secondary_addr is not None
            else None
        )
        return primary, secondary

    def _refresh_current_tab(self) -> None:
        row = self._selected_row
        widget = self.tabs.currentWidget()
        if self.result is None or widget is None:
            return

        primary, secondary = self._resolve_functions(row)
        try:
            if widget is self.graph_tab:
                self.graph_tab.show_pair(primary, secondary)
            else:
                widget.show_pair(
                    self.result.primary_bv, primary, self.result.secondary_bv, secondary
                )
        except Exception as exc:
            log_error(f"Failed to render diff: {exc}", "QBinDiff")

    # -- drag and drop -----------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if local_files(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if local_files(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = local_files(event.mimeData())
        if not paths:
            return
        event.acceptProposedAction()
        self.start_diff(paths[0])

    # -- View protocol -----------------------------------------------------

    def getData(self):
        return self.data

    def getCurrentOffset(self) -> int:
        return self.current_offset

    def getSelectionOffsets(self):
        return (self.current_offset, self.current_offset)

    def setCurrentOffset(self, offset: int) -> None:
        self.current_offset = offset

    def getFont(self):
        return binaryninjaui.getMonospaceFont(self)

    def navigate(self, addr: int) -> bool:
        if self.result is None:
            return False
        match = self.result.by_primary.get(addr)
        if match is None:
            return False
        self.current_offset = addr
        return True


class DiffViewType(ViewType):
    def __init__(self):
        super().__init__("Diff", "Binary Diff")

    def getPriority(self, data, filename) -> int:
        # Available for any analyzed binary, but never the default view.
        # Custom firmware views routinely forget perform_is_executable (it
        # defaults to False), so a non-Raw view type — some loader recognized
        # the file — qualifies too; plain hexdumps stay excluded.
        return 1 if (data.executable or data.view_type != "Raw") else 0

    def create(self, data, view_frame):
        return DiffView(view_frame, data)
