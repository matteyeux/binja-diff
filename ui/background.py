# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Work done off the UI thread, with its results handed back onto it.

Nothing here imports Qt: results come back through ``execute_on_main_thread``,
and the callbacks that touch widgets run there. Both runners can be stopped and
*waited on*, which is the part that matters. They read the secondary
``BinaryView``, and closing that view under a worker that is still walking its
functions is a crash, not an exception — so everything that closes it stops
these first (see ``stop_all`` and ``DiffView._release_secondary``).
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable, Sequence
from typing import Any

from binaryninja import execute_on_main_thread, log_error

#: How long closing a view waits for a worker to notice it was cancelled. Each
#: unit of work is one function, bounded by `align.MAX_CLASSIFY_INSTRUCTIONS`,
#: so this is only ever reached by something badly wrong.
STOP_TIMEOUT = 10.0

_RUNNERS: weakref.WeakSet = weakref.WeakSet()


def stop_all(timeout: float = STOP_TIMEOUT) -> None:
    """Cancel every live runner and wait for its thread, for process shutdown."""

    runners = list(_RUNNERS)
    for runner in runners:
        runner.cancel()
    for runner in runners:
        runner.wait(timeout)


def _report(name: str, exc: BaseException) -> None:
    log_error(f"{name} failed: {exc}", "QBinDiff")


class LatestOnly:
    """Runs one job at a time, and delivers only the newest request's result.

    Scrolling through the match table asks for a render per row, and only the
    last is still wanted. A request made while one is running replaces whatever
    was queued behind it, and a result landing after a newer request is dropped
    rather than drawn over it.
    """

    def __init__(self, name: str):
        self._name = name
        self._lock = threading.Lock()
        self._generation = 0
        self._pending: tuple | None = None
        self._thread: threading.Thread | None = None
        _RUNNERS.add(self)

    def submit(
        self,
        compute: Callable[[], Any],
        deliver: Callable[[Any], None],
        fail: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Run ``compute`` in the background, then ``deliver`` its value on the UI thread."""

        with self._lock:
            self._generation += 1
            job = (self._generation, compute, deliver, fail)
            if self._thread is not None:
                self._pending = job
                return
            self._thread = threading.Thread(
                target=self._run, args=(job,), name=self._name, daemon=True
            )
            self._thread.start()

    def _current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _run(self, job: tuple | None) -> None:
        while job is not None:
            generation, compute, deliver, fail = job
            # Superseded while it sat in the queue: nobody wants it any more.
            if self._current(generation):
                try:
                    self._post(generation, deliver, compute())
                except Exception as exc:
                    self._post(generation, fail or (lambda e: _report(self._name, e)), exc)
            with self._lock:
                job, self._pending = self._pending, None
                if job is None:
                    self._thread = None

    def _post(self, generation: int, callback: Callable[[Any], None], value: Any) -> None:
        def land() -> None:
            if not self._current(generation):
                return
            try:
                callback(value)
            except Exception as exc:
                _report(self._name, exc)

        execute_on_main_thread(land)

    def cancel(self) -> None:
        """Drop the queued job and anything still in flight; neither is delivered."""

        with self._lock:
            self._generation += 1
            self._pending = None

    def wait(self, timeout: float = STOP_TIMEOUT) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


class BatchRunner:
    """Walks a list off the UI thread, posting results back a batch at a time.

    One post per item would flood the main thread's queue on a table of tens of
    thousands of functions; one post at the end would show nothing for minutes.
    """

    #: Seconds between posts. Short enough to look live, long enough that the
    #: table repaints a few times a second rather than per function.
    INTERVAL = 0.3

    def __init__(self, name: str):
        self._name = name
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        _RUNNERS.add(self)

    def start(
        self,
        items: Sequence[Any],
        compute: Callable[[Any], Any],
        deliver: Callable[[list[tuple[Any, Any]], bool], None],
    ) -> None:
        """Compute each item; ``deliver(batch, finished)`` runs on the UI thread."""

        self.cancel()
        self.wait()
        stop = threading.Event()
        self._stop = stop
        self._thread = threading.Thread(
            target=self._run,
            args=(list(items), compute, deliver, stop),
            name=self._name,
            daemon=True,
        )
        self._thread.start()

    def _run(self, items, compute, deliver, stop: threading.Event) -> None:
        batch: list[tuple[Any, Any]] = []
        last = time.monotonic()
        for item in items:
            if stop.is_set():
                return
            try:
                value = compute(item)
            except Exception:
                # One function that will not render must not end the pass.
                value = None
            batch.append((item, value))
            if time.monotonic() - last >= self.INTERVAL:
                self._post(deliver, batch, False, stop)
                batch = []
                last = time.monotonic()
        self._post(deliver, batch, True, stop)

    def _post(self, deliver, batch, finished: bool, stop: threading.Event) -> None:
        def land() -> None:
            if stop.is_set():
                return
            try:
                deliver(batch, finished)
            except Exception as exc:
                _report(self._name, exc)

        execute_on_main_thread(land)

    def cancel(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def wait(self, timeout: float = STOP_TIMEOUT) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
