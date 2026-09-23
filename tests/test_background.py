"""Cover the runners that move rendering and classification off the UI thread.

What they must guarantee is narrow and easy to break: a render that was
superseded is never drawn over a newer one, a cancelled job delivers nothing,
and a stopped runner has *finished* — the diff view closes the secondary
BinaryView right after, and a worker still walking it would crash the process.

The stub's execute_on_main_thread runs the callback in place, on the worker, so
delivery order here is the order the worker posts in.

    .venv/bin/python tests/test_background.py
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()

from binja_diff.ui import background  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def test_only_the_latest_render_is_delivered():
    print("only the latest render is delivered")

    runner = background.LatestOnly("test")
    release = threading.Event()
    started = threading.Event()
    delivered: list[str] = []

    def slow():
        started.set()
        release.wait(5)
        return "first"

    runner.submit(slow, delivered.append)
    started.wait(5)
    # Queued behind the running job; the second replaces it before it starts.
    runner.submit(lambda: "second", delivered.append)
    runner.submit(lambda: "third", delivered.append)
    release.set()
    runner.wait()

    check("the superseded render is dropped", "first" not in delivered, f"{delivered}")
    check("the queued-over request never runs", "second" not in delivered, f"{delivered}")
    check("the newest lands", delivered == ["third"], f"{delivered}")


def test_a_failed_render_reports_instead_of_delivering():
    print("a failed render goes to its failure handler")

    runner = background.LatestOnly("test")
    delivered: list = []
    failed: list = []

    def broken():
        raise ValueError("no such function")

    runner.submit(broken, delivered.append, failed.append)
    runner.wait()
    check("nothing delivered", delivered == [])
    check("the error is handed over", len(failed) == 1 and isinstance(failed[0], ValueError))


def test_cancel_drops_what_is_in_flight():
    print("cancelling drops an in-flight render")

    runner = background.LatestOnly("test")
    release = threading.Event()
    started = threading.Event()
    delivered: list = []

    def slow():
        started.set()
        release.wait(5)
        return "stale"

    runner.submit(slow, delivered.append)
    started.wait(5)
    runner.cancel()
    release.set()
    runner.wait()
    check("a cancelled render is not drawn", delivered == [], f"{delivered}")


def test_batches_cover_every_item_and_finish_once():
    print("the batch runner covers every item")

    runner = background.BatchRunner("test")
    runner.INTERVAL = 0.0
    seen: list = []
    finishes: list[bool] = []

    def deliver(batch, finished):
        seen.extend(batch)
        finishes.append(finished)

    def compute(item):
        if item == 3:
            raise RuntimeError("will not render")
        return item * 10

    runner.start(range(6), compute, deliver)
    runner.wait()
    check("every item is reported", [item for item, _ in seen] == list(range(6)), f"{seen}")
    check("a failing item is None, not the end", dict(seen)[3] is None and dict(seen)[5] == 50)
    check("finished exactly once, last", finishes.count(True) == 1 and finishes[-1] is True)


def test_stopping_a_batch_waits_for_the_worker():
    print("stopping the batch runner waits for its thread")

    runner = background.BatchRunner("test")
    entered = threading.Event()
    walked: list = []
    delivered: list = []

    def compute(item):
        entered.set()
        walked.append(item)
        threading.Event().wait(0.01)
        return item

    runner.start(range(1000), compute, lambda batch, finished: delivered.append(finished))
    entered.wait(5)
    background.stop_all()
    count = len(walked)
    threading.Event().wait(0.05)
    check("nothing runs after stop_all returns", len(walked) == count, f"{count} -> {len(walked)}")
    check("the walk was cut short", count < 1000, f"{count}")
    check("a stopped pass never reports finishing", True not in delivered)


def main() -> int:
    for test in (
        test_only_the_latest_render_is_delivered,
        test_a_failed_render_reports_instead_of_delivering,
        test_cancel_drops_what_is_in_flight,
        test_batches_cover_every_item_and_finish_once,
        test_stopping_a_batch_waits_for_the_worker,
    ):
        test()
    print()
    if check.failures:
        print(f"{check.failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
