"""Cover DiffTask's completion, cancellation and failure paths.

These are the paths that otherwise only surface in the GUI: a cancelled or
failed run must still release the secondary binary and notify the UI, or the
view is left stuck with its Cancel button showing.

    .venv-qbindiff/bin/python binja_diff/tests/test_engine.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()

from binja_diff.core import engine  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def fake_mapping():
    """Stand-in for qbindiff's Mapping: duck-typed the way DiffResult reads it."""

    class Func:
        def __init__(self, addr, name):
            self.addr = addr
            self.name = name

    class Match:
        def __init__(self, primary, secondary):
            self.primary = primary
            self.secondary = secondary
            self.similarity = 0.9
            self.confidence = 0.8

    class FakeMapping:
        def __init__(self):
            self.primary_unmatched = [Func(0x30, "c"), Func(0x20, "b")]
            self.secondary_unmatched = [Func(0x40, "d")]
            self.normalized_similarity = 0.5

        def __iter__(self):
            return iter([Match(Func(0x10, "a"), Func(0x11, "a"))])

    return FakeMapping()


class FakeFile:
    def __init__(self):
        self.filename = "secondary"
        self.original_filename = "secondary"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBV:
    def __init__(self):
        self.file = FakeFile()


class Harness:
    """A DiffTask with the BackgroundTaskThread base and run_diff stubbed out."""

    def __init__(self, *, cancel_after_load=False, diff_result=None, raise_in_diff=False):
        self.loaded = FakeBV()
        self.events: list[str] = []
        self.cancel_after_load = cancel_after_load
        self.diff_result = diff_result
        self.raise_in_diff = raise_in_diff

        # Built by hand, bypassing __init__, because BackgroundTaskThread is
        # stubbed out. Every field DiffTask.run touches has to be set here —
        # adding one to the class without adding it below fails every test in
        # this module with an AttributeError.
        self.task = engine.DiffTask.__new__(engine.DiffTask)
        self.task.primary_bv = object()
        self.task._secondary = "/tmp/secondary"
        self.task._options = engine.DiffOptions()
        self.task.owns_secondary = True
        self.task.load_timings = []
        self.task._region_name = None
        self.task.cancelled = False
        self.task.progress = ""
        self.task._on_done = lambda r: self.events.append("done")
        self.task._on_error = lambda m: self.events.append(f"error:{m}")
        self.task._on_cancelled = lambda: self.events.append("cancelled")
        self.task._on_progress = None

    def run(self) -> None:
        original_load, original_diff = engine.load_secondary, engine.run_diff

        def fake_load(path, progress=None, cancelled=None):
            if self.cancel_after_load:
                self.task.cancelled = True
            return self.loaded

        def fake_diff(
            primary, secondary, options=None, progress=None, cancelled=None, region_name=None
        ):
            if self.raise_in_diff:
                raise RuntimeError("boom")
            return self.diff_result

        engine.load_secondary, engine.run_diff = fake_load, fake_diff
        try:
            self.task.run()
        finally:
            engine.load_secondary, engine.run_diff = original_load, original_diff


def test_cancel_before_diff():
    print("cancelled during load")
    harness = Harness(cancel_after_load=True)
    harness.run()
    check("UI notified", harness.events == ["cancelled"], f"got {harness.events}")
    check("secondary closed", harness.loaded.file.closed)


def test_cancel_during_diff():
    print("cancelled during diff")
    # run_diff returning None is how a cancelled diff reports itself.
    harness = Harness(diff_result=None)
    harness.run()
    check("UI notified", harness.events == ["cancelled"], f"got {harness.events}")
    check("secondary closed", harness.loaded.file.closed)


def test_failure():
    print("diff raises")
    harness = Harness(raise_in_diff=True)
    harness.run()
    check("error reported", harness.events == ["error:boom"], f"got {harness.events}")
    check("secondary closed", harness.loaded.file.closed)


def test_success_keeps_secondary():
    print("successful diff")

    class FakeResult:
        nb_match = 3
        similarity = 0.75
        duration = 1.0

        def __init__(self):
            # DiffTask splices the load timing into this, so it has to be a
            # real per-instance list.
            self.timings = [("Extracting features", 1.0)]

    harness = Harness(diff_result=FakeResult())
    harness.run()
    check("done reported", harness.events == ["done"], f"got {harness.events}")
    check("secondary kept open for the UI", not harness.loaded.file.closed)


def test_borrowed_secondary_never_closed():
    print("borrowed secondary")
    harness = Harness(diff_result=None)
    # A BinaryView handed in by the UI is not ours to close.
    harness.task.owns_secondary = False
    harness.run()
    check("UI notified", harness.events == ["cancelled"], f"got {harness.events}")
    check("borrowed view left open", not harness.loaded.file.closed)


def test_progress_reporting():
    print("progress reporting")
    harness = Harness(diff_result=None)
    seen: list[tuple[str, float]] = []
    harness.task._on_progress = lambda label, fraction: seen.append((label, fraction))

    harness.task._report("Extracting features", 0.42)
    check("callback invoked", seen == [("Extracting features", 0.42)], f"got {seen}")
    check(
        "status string set",
        harness.task.progress == "Binary diff: Extracting features (42%)",
        f"got {harness.task.progress!r}",
    )

    harness.task._on_progress = None
    harness.task._report("Matching functions", 1.0)
    check("no callback is fine", harness.task.progress.endswith("(100%)"))

    # A phase with no measurable progress must not claim a percentage.
    harness.task._report("Building the similarity matrix", engine.INDETERMINATE)
    check(
        "indeterminate phases report no percentage",
        harness.task.progress == "Binary diff: Building the similarity matrix",
        f"got {harness.task.progress!r}",
    )


def test_feature_phase_labels():
    print("feature phase labels")
    label, value = engine.feature_phase(0.0)
    check("starts as extraction", label == "Extracting features" and value == 0.0)

    label, value = engine.feature_phase(0.5)
    check("mid-extraction keeps the fraction", (label, value) == ("Extracting features", 0.5))

    # QBinDiff's visitor tops out a hair under 1.0; everything past that point
    # is the similarity matrix, which reports nothing at all.
    label, value = engine.feature_phase(0.999)
    check("switches before exactly 1.0", label == "Building the similarity matrix", label)
    check("and goes indeterminate", value == engine.INDETERMINATE, f"got {value}")

    label, _value = engine.feature_phase(1.0)
    check("stays switched at 1.0", label == "Building the similarity matrix", label)

    label, _value = engine.feature_phase(0.9)
    check("does not switch early", label == "Extracting features", label)


def test_database_detection():
    print("database detection")
    check("plain bndb", engine.is_database("/tmp/x.bndb"))
    check("uppercase bndb", engine.is_database("/tmp/X.BNDB"))
    check("path with dots", engine.is_database("/tmp/a.b.c.bndb"))
    check("raw binary", not engine.is_database("/bin/ls"))
    check("bndb in the middle", not engine.is_database("/tmp/x.bndb.bak"))
    check("empty", not engine.is_database(""))


def test_load_missing_file():
    print("missing file is reported clearly")
    try:
        engine.load_secondary("/nonexistent/path/to/binary")
    except RuntimeError as exc:
        check("raises RuntimeError", True)
        check("names the path", "/nonexistent/path/to/binary" in str(exc), str(exc))
    except Exception as exc:
        check("raises RuntimeError", False, repr(exc))
    else:
        check("raises RuntimeError", False, "no exception")


def test_sparsity_scaling():
    print("sparsity scaling by size")
    options = engine.DiffOptions()
    default = options.sparsity_ratio

    small = engine.scale_options_for_size(options, 2_500, 2_500)
    check("small diff untouched", small.sparsity_ratio == default and not small.sparse_row)

    # 5000 x 8000 is exactly the candidate budget: still within it.
    budget = engine.scale_options_for_size(options, 5_000, 8_000)
    check("within the budget, untouched", budget.sparsity_ratio == default)

    medium = engine.scale_options_for_size(options, 8_000, 9_000)
    kept = (1 - medium.sparsity_ratio) * 8_000 * 9_000
    check(
        "past the budget, raised just enough",
        default < medium.sparsity_ratio < 0.6 and kept <= engine.CANDIDATE_BUDGET * 1.001,
        f"{medium.sparsity_ratio}, {kept:.0f} pairs",
    )
    check("medium stays global", not medium.sparse_row)

    # The budget is what 0.6 admitted at the threshold, so a diff below it is
    # never sparser -- never less accurate -- than it was with that default.
    boundary = engine.scale_options_for_size(options, 10_000, 10_000)
    check("the threshold lands on the old default", boundary.sparsity_ratio == 0.6)

    large = engine.scale_options_for_size(options, 50_000, 60_000)
    check("sparsity raised", large.sparsity_ratio == engine.LARGE_DIFF_SPARSITY)
    check("row-wise sparsification enabled", large.sparse_row)
    check(
        "caller's options not mutated",
        options.sparsity_ratio == default and not options.sparse_row,
    )

    explicit = engine.scale_options_for_size(
        engine.DiffOptions(sparsity_ratio=0.995), 50_000, 60_000
    )
    check("explicit higher sparsity respected", explicit.sparsity_ratio == 0.995)
    explicit = engine.scale_options_for_size(engine.DiffOptions(sparsity_ratio=0.9), 8_000, 9_000)
    check("explicit sparsity above the budget kept", explicit.sparsity_ratio == 0.9)

    for count in (50_000, 9_000):
        disabled = engine.scale_options_for_size(
            engine.DiffOptions(auto_sparsity=False), count, count
        )
        check(
            f"opt-out respected at {count}",
            disabled.sparsity_ratio == default and not disabled.sparse_row,
        )


def test_feature_selection():
    print("feature selection")
    keys = [f.key for f in engine.feature_extractors()]
    check("Address left out by default", "addr" not in keys, f"{keys}")
    check("import calls registered", "imp" in keys, f"{keys}")
    check("the other QBinDiff defaults kept", {"fname", "dat", "cst"} <= set(keys), f"{keys}")
    check("structural extras kept", set(engine.DEFAULT_EXTRA_FEATURES) <= set(keys))
    check("no feature twice", len(keys) == len(set(keys)), f"{keys}")

    from binja_diff.core.backend import ImportCalls

    check(
        "imp is the guarded ImportCalls, not QBinDiff's ImpName",
        ImportCalls in engine.feature_extractors(),
    )
    keys = [f.key for f in engine.feature_extractors(("addr", "nonsense"))]
    check("Address can still be asked for", "addr" in keys, f"{keys}")
    check("an unknown key is skipped", "nonsense" not in keys)


def test_result_indexing():
    print("DiffResult indexing")

    result = engine.DiffResult.build(object(), object(), fake_mapping())
    check("primary index", 0x10 in result.by_primary)
    check("secondary index", 0x11 in result.by_secondary)
    check("unmatched sorted by address", [f.addr for f in result.primary_unmatched] == [0x20, 0x30])
    check("similarity exposed", result.similarity == 0.5)
    check("counts", (result.nb_match, result.nb_unmatched_primary) == (1, 2))
    check(
        "match fields copied off qbindiff's objects",
        result.matches[0].primary.name == "a" and result.matches[0].confidence == 0.8,
    )


def test_wait_for_analysis():
    print("waiting for analysis")
    stubs = _bootstrap.stubs()
    State, Progress = stubs.AnalysisState, stubs.AnalysisProgress

    class AnalyzingView:
        """A view that reports each queued state once, then stays idle."""

        def __init__(self, *states):
            self.file = stubs.FileMetadata("/tmp/primary")
            self.analysis_waits = 0
            self._states = list(states)

        @property
        def analysis_progress(self):
            return self._states.pop(0) if self._states else Progress(State.IdleState, 0, 0)

        def update_analysis_and_wait(self):
            self.analysis_waits += 1

    steps: list[tuple[str, float]] = []
    busy = AnalyzingView(
        Progress(State.DisassembleState, 1, 4),
        Progress(State.AnalyzeState, 3, 4),
    )
    engine.ANALYSIS_POLL_SECONDS = 0.0
    try:
        done = engine.wait_for_analysis(
            busy, progress=lambda label, fraction: steps.append((label, fraction))
        )
        check("returns True", done)
        check("polled while busy", len(steps) >= 2, f"got {steps}")
        check("reported a fraction", steps[0][1] == 0.25, f"got {steps[0]}")
        check("named the file", steps[0][0].endswith("primary"), steps[0][0])
        check("forced a final analysis pass", busy.analysis_waits == 1, f"{busy.analysis_waits}")

        # An already-idle view still gets the blocking call: idle means nothing
        # is running, not that everything pending has run.
        idle = AnalyzingView()
        check("idle view returns True", engine.wait_for_analysis(idle))
        check("idle view still analyzed", idle.analysis_waits == 1)

        # Cancelling must not fall through to the blocking call.
        cancelling = AnalyzingView(*([Progress(State.AnalyzeState, 1, 2)] * 3))
        check(
            "cancel returns False",
            not engine.wait_for_analysis(cancelling, cancelled=lambda: True),
        )
        check("no blocking wait after cancel", cancelling.analysis_waits == 0)
    finally:
        engine.ANALYSIS_POLL_SECONDS = 0.2


def test_log_bridge():
    print("qbindiff logging reaches Binary Ninja once, not twice")
    import io
    import logging

    forwarded: list[str] = []
    root = logging.getLogger()
    console = logging.StreamHandler(io.StringIO())
    root.addHandler(console)
    previous_level = root.level
    root.setLevel(logging.WARNING)

    # _LEVELS binds the log functions at class definition time, so the sinks
    # have to be replaced there rather than on the module.
    original_levels = engine._BinjaLogHandler._LEVELS
    engine._BinjaLogHandler._LEVELS = tuple(
        (level, lambda message, tag=None: forwarded.append(message))
        for level, _sink in original_levels
    )
    try:
        with engine._log_bridge():
            logging.info("[+] Converged after 62 iterations")
        check(
            "forwarded to the Binary Ninja log", forwarded == ["[+] Converged after 62 iterations"]
        )
        check(
            "not echoed to the console as well",
            console.stream.getvalue() == "",
            repr(console.stream.getvalue()),
        )

        # Everything is put back: the bridge is scoped to one diff, and the
        # console belongs to whoever set it up.
        check("root level restored", root.level == logging.WARNING)
        logging.warning("after the bridge")
        check("console handler usable again", "after the bridge" in console.stream.getvalue())
    finally:
        engine._BinjaLogHandler._LEVELS = original_levels
        root.removeHandler(console)
        root.setLevel(previous_level)


def test_duration_formatting():
    print("duration formatting")
    cases = (
        (0.0, "0ms"),
        (0.0412, "41ms"),
        (1.0, "1.0s"),
        (59.94, "59.9s"),
        (60.0, "1m 00s"),
        (95.0, "1m 35s"),
        (3600.0, "1h 00m 00s"),
        (3725.0, "1h 02m 05s"),
    )
    for seconds, expected in cases:
        got = engine.format_duration(seconds)
        check(f"{seconds}s -> {expected}", got == expected, f"got {got}")


def test_phase_timings():
    print("phase timings")
    timings: list[tuple[str, float]] = []
    with engine._timed(timings, "Matching functions"):
        pass
    check("phase recorded", [label for label, _ in timings] == ["Matching functions"])
    check("duration is a number", timings[0][1] >= 0.0)

    # A phase that raises is the one whose duration matters most.
    try:
        with engine._timed(timings, "Extracting features"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("failed phase still timed", len(timings) == 2, f"got {timings}")

    result = engine.DiffResult.build(object(), object(), fake_mapping())
    result.timings = [("Matching functions", 90.0), ("Scoring matches", 1.5)]
    check("total is the sum", result.duration == 91.5)
    check(
        "report lists every phase in order",
        result.timing_report == "Matching functions: 1m 30s\nScoring matches: 1.5s",
        repr(result.timing_report),
    )
    untimed = engine.DiffResult(object(), object(), 0.5)
    check("an untimed result reports no time", untimed.duration == 0)


def main() -> int:
    for test in (
        test_cancel_before_diff,
        test_cancel_during_diff,
        test_failure,
        test_success_keeps_secondary,
        test_borrowed_secondary_never_closed,
        test_progress_reporting,
        test_feature_phase_labels,
        test_database_detection,
        test_load_missing_file,
        test_sparsity_scaling,
        test_feature_selection,
        test_result_indexing,
        test_wait_for_analysis,
        test_log_bridge,
        test_duration_formatting,
        test_phase_timings,
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
