"""Cover saving a diff and restoring it without re-running QBinDiff.

The point of the format is that a result survives a Binary Ninja restart, so
what matters here is that a round trip through JSON is lossless, that a payload
which is not ours is rejected rather than half-read, and that a binary which
changed underneath the saved diff is noticed — a restore that silently pairs
the wrong functions is worse than no restore at all.

    .venv/bin/python tests/test_persist.py
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import ModuleType
from typing import cast

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()

from binja_diff.core import persist  # noqa: E402
from binja_diff.core.engine import DiffOptions, DiffResult, FunctionRef, MatchRecord  # noqa: E402


check = _bootstrap.check


def make_view(name: str, size: int = 64, functions: int = 3):
    stubs = _bootstrap.stubs()
    bv = stubs.BinaryView(name, b"\x00" * size)
    bv.functions = [object()] * functions
    return bv


def make_result():
    primary, secondary = make_view("/tmp/a.bin"), make_view("/tmp/b.bin", size=72)
    return DiffResult(
        primary_bv=primary,
        secondary_bv=secondary,
        similarity=0.875,
        matches=[
            MatchRecord(FunctionRef(0x1000, "main"), FunctionRef(0x2000, "main"), 1.0, 0.9),
            MatchRecord(FunctionRef(0x1100, "sub_1100"), FunctionRef(0x2100, "check"), 0.5, 0.25),
        ],
        primary_unmatched=[FunctionRef(0x1200, "gone")],
        secondary_unmatched=[FunctionRef(0x2200, "added"), FunctionRef(0x2300, "also_added")],
    )


def test_round_trip():
    print("JSON round trip")
    result = make_result()
    saved = persist.SavedDiff.from_result(result, DiffOptions())
    back = persist.SavedDiff.from_json(saved.to_json())

    check("similarity preserved", back.similarity == result.similarity)
    check("matches preserved", back.matches == result.matches, f"got {back.matches}")
    check("primary-only preserved", back.primary_unmatched == result.primary_unmatched)
    check("secondary-only preserved", back.secondary_unmatched == result.secondary_unmatched)
    check("secondary path preserved", back.secondary.filename == "/tmp/b.bin")
    check("options recorded", back.options.get("distance") == "haussmann", f"{back.options}")
    check("creation time recorded", bool(back.created))
    check("an unscoped diff saves an empty scope", back.scope == [], f"{back.scope}")
    check(
        "matcher-made rows carry no manual flag",
        all(len(row) == 6 for row in saved.to_dict()["matches"]),
    )

    result.set_match(0x1200, 0x2200)
    manual = persist.SavedDiff.from_json(persist.SavedDiff.from_result(result).to_json())
    check(
        "a hand-made pair survives the round trip as manual",
        any(m.manual and m.primary.addr == 0x1200 for m in manual.matches),
        f"{manual.matches}",
    )

    result.scope = ["AppleSEPManager"]
    scoped = persist.SavedDiff.from_json(persist.SavedDiff.from_result(result).to_json())
    check(
        "the scope survives the round trip", scoped.scope == ["AppleSEPManager"], f"{scoped.scope}"
    )
    check(
        "and reaches the restored result",
        scoped.to_result(result.primary_bv, result.secondary_bv).scope == ["AppleSEPManager"],
    )
    # A file written before the key existed is still ours to read.
    legacy = persist.SavedDiff.from_result(result).to_dict()
    del legacy["scope"]
    check(
        "a payload without a scope reads as unscoped",
        persist.SavedDiff.from_dict(legacy).scope == [],
    )

    restored = back.to_result(result.primary_bv, result.secondary_bv)
    check("indexes rebuilt", 0x1000 in restored.by_primary and 0x2100 in restored.by_secondary)
    check(
        "counts match the original",
        (restored.nb_match, restored.nb_unmatched_primary, restored.nb_unmatched_secondary)
        == (2, 1, 2),
    )


def test_rejects_foreign_payloads():
    print("foreign payloads are rejected")
    for label, text in (
        ("empty string", ""),
        ("not json", "not json at all"),
        ("json but not ours", '{"hello": "world"}'),
        ("a bare list", "[1, 2, 3]"),
    ):
        try:
            persist.SavedDiff.from_json(text)
        except ValueError:
            check(label, True)
        except Exception as exc:
            check(label, False, f"raised {exc!r} instead of ValueError")
        else:
            check(label, False, "no exception")


def test_rejects_newer_version():
    print("a newer format version is refused, not misread")
    saved = persist.SavedDiff.from_result(make_result())
    payload = saved.to_dict()
    payload["version"] = persist.VERSION + 1
    try:
        persist.SavedDiff.from_dict(payload)
    except ValueError as exc:
        check("raises ValueError", True)
        check("names the version", str(persist.VERSION + 1) in str(exc), str(exc))
    else:
        check("raises ValueError", False, "no exception")


def test_truncated_payload():
    print("a malformed match row is a ValueError, not a crash")
    payload = persist.SavedDiff.from_result(make_result()).to_dict()
    payload["matches"][0] = [0x1000, "main"]
    try:
        persist.SavedDiff.from_dict(payload)
    except ValueError:
        check("raises ValueError", True)
    except Exception as exc:
        check("raises ValueError", False, f"raised {exc!r}")
    else:
        check("raises ValueError", False, "no exception")


def test_database_sink():
    print("database sink")
    result = make_result()
    bv = result.primary_bv

    check("nothing stored yet", persist.load_from_database(bv) is None)

    persist.store_in_database(bv, persist.SavedDiff.from_result(result))
    saved = persist.load_from_database(bv)
    check("round trips through metadata", saved is not None and len(saved.matches) == 2)

    persist.remove_from_database(bv)
    check("removal works", persist.load_from_database(bv) is None)

    bv.store_metadata(persist.METADATA_KEY, "{}")
    check("junk metadata is ignored, not raised", persist.load_from_database(bv) is None)


def test_file_sink():
    print("file sink")
    result = make_result()
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / persist.default_filename(result))
        check("suggested name mentions both sides", "a.bin-vs-b.bin" in path, path)

        persist.write_file(persist.SavedDiff.from_result(result), path)
        saved = persist.read_file(path)
        check("matches survive the file", len(saved.matches) == 2)
        check("stored as plain JSON", isinstance(json.loads(Path(path).read_text()), dict))

        try:
            persist.read_file(str(Path(tmp) / "missing.json"))
        except RuntimeError:
            check("missing file raises RuntimeError", True)
        except Exception as exc:
            check("missing file raises RuntimeError", False, repr(exc))
        else:
            check("missing file raises RuntimeError", False, "no exception")


def test_drift_detection():
    print("drift detection")
    result = make_result()
    saved = persist.SavedDiff.from_result(result)

    check("same view has no drift", saved.primary.differences(result.primary_bv) == [])

    rebuilt = make_view("/tmp/a.bin", size=64, functions=5)
    check("new functions noticed", len(saved.primary.differences(rebuilt)) == 1)

    resized = make_view("/tmp/a.bin", size=128, functions=3)
    check("size change noticed", len(saved.primary.differences(resized)) == 1)

    renamed = make_view("/tmp/elsewhere/other.bin", size=64, functions=3)
    check("different file noticed", len(saved.primary.differences(renamed)) == 1)

    # A file that merely moved is the same file; only the directory changed.
    moved = make_view("/elsewhere/a.bin", size=64, functions=3)
    check("a moved file is not drift", saved.primary.differences(moved) == [])


def test_summary():
    print("summary line")
    saved = persist.SavedDiff.from_result(make_result())
    summary = saved.summary
    check("names the secondary", "b.bin" in summary, summary)
    check("counts the matches", "2 matches" in summary, summary)


def test_minimal_payload_reads():
    """Additive keys are read with ``get``: a file from an older plugin, which
    has none of them, still restores."""

    print("a payload with only the required keys reads")
    saved = persist.SavedDiff.from_dict({"format": persist.FORMAT, "version": persist.VERSION})
    check("no matches is fine", saved.matches == [] and saved.scope == [] and saved.options == {})
    check(
        "a missing version reads as the oldest",
        persist.SavedDiff.from_dict({"format": persist.FORMAT}).version == 0,
    )


def test_database_presence_queries():
    print("has_saved_diff and is_persistent answer without parsing")
    bv = make_view("/tmp/a.bin")
    check("nothing saved yet", not persist.has_saved_diff(bv))
    persist.store_in_database(bv, persist.SavedDiff.from_result(make_result()))
    check("a stored diff is seen", persist.has_saved_diff(bv))
    check("an unsaved view is not persistent", not persist.is_persistent(bv))
    bv.file.has_database = True
    check("a database is", persist.is_persistent(bv))

    class Broken:
        @property
        def file(self):
            raise RuntimeError("closed")

    check("a view that cannot answer is not persistent", not persist.is_persistent(Broken()))


def test_timings_are_not_saved():
    print("timings describe the run, not the result")
    result = make_result()
    result.timings = [("Matching functions", 720.0)]
    check(
        "no timings key in the payload",
        "timings" not in persist.SavedDiff.from_result(result).to_dict(),
    )


def _sep_pair(api_names):
    """Two SEP container views, empty until a part is mapped, plus the fake loader."""

    from binja_diff.core import scope

    stubs = _bootstrap.stubs()
    views = []
    for name in ("/tmp/sep-a.bin", "/tmp/sep-b.bin"):
        bv = stubs.BinaryView(name, b"\x00" * 64)
        bv.view_type = scope.SEP_VIEW
        views.append(bv)
    return views


class _FakeSepApi:
    API_VERSION = 3

    def __init__(self, names):
        self.names = names
        self.loaded: list[tuple[str, list[str]]] = []

    def module_names(self, bv):
        return list(self.names)

    def load_modules(self, bv, names):
        names = list(names)
        if any(name not in self.names for name in names):
            return False
        self.loaded.append((bv.file.filename, names))
        stubs = _bootstrap.stubs()
        for name in names:
            section = f"{name}:__TEXT:__text"
            bv.sections[section] = stubs.Section(section, 0x1000, 0x2000)
        return True


def test_restoring_a_container_diff_maps_its_parts_back():
    """A saved diff of one SEP module names it, and a restore has to map that
    module into both views before a single saved address resolves."""

    print("restoring a scoped diff loads the parts on both sides")
    import sys

    from binja_diff.core import scope

    api = _FakeSepApi(["SEPOS", "SEPD"])
    # Published the way sep-binja publishes it: as an entry in sys.modules that
    # is looked up, never imported, so it need not be a module.
    sys.modules[scope._SEP_API_KEY] = cast(ModuleType, api)
    try:
        primary, secondary = _sep_pair(api.names)
        saved = persist.SavedDiff.from_result(make_result())
        saved.scope = ["SEPD"]
        events: list = []
        task = persist.RestoreTask(
            primary,
            saved,
            secondary,
            on_done=lambda result: events.append(("done", result)),
            on_error=lambda message: events.append(("error", message)),
        )
        task.start()
        check("the restore completed", [kind for kind, _ in events] == ["done"], f"{events}")
        check(
            "the part was mapped into both views",
            api.loaded == [("/tmp/sep-a.bin", ["SEPD"]), ("/tmp/sep-b.bin", ["SEPD"])],
            f"{api.loaded}",
        )
        check(
            "both views were analyzed once",
            (primary.analysis_waits, secondary.analysis_waits) == (1, 1),
        )
        if events and events[0][0] == "done":
            check("the result carries the scope", events[0][1].scope == ["SEPD"])

        # A part the file does not have is an error the user can act on, not a
        # table full of missing functions.
        primary, secondary = _sep_pair(api.names)
        saved.scope = ["AESS"]
        events.clear()
        persist.RestoreTask(
            primary,
            saved,
            secondary,
            on_done=lambda result: events.append(("done", result)),
            on_error=lambda message: events.append(("error", message)),
        ).start()
        check(
            "a missing part fails the restore",
            bool(events) and events[0][0] == "error",
            f"{events}",
        )
        check("and names it", bool(events) and "AESS" in events[0][1], f"{events}")

        # Unscoped saves still restore without touching the loader.
        plain_saved = persist.SavedDiff.from_result(make_result())
        before = list(api.loaded)
        events.clear()
        persist.RestoreTask(
            make_view("/tmp/a.bin"),
            plain_saved,
            make_view("/tmp/b.bin"),
            on_done=lambda result: events.append(("done", result)),
            on_error=lambda message: events.append(("error", message)),
        ).start()
        check("an unscoped restore completes", bool(events) and events[0][0] == "done", f"{events}")
        check("without loading anything", api.loaded == before)
    finally:
        sys.modules.pop(scope._SEP_API_KEY, None)


def main() -> int:
    return _bootstrap.run(
        [
            test_round_trip,
            test_rejects_foreign_payloads,
            test_rejects_newer_version,
            test_truncated_payload,
            test_database_sink,
            test_file_sink,
            test_drift_detection,
            test_summary,
            test_minimal_payload_reads,
            test_database_presence_queries,
            test_timings_are_not_saved,
            test_restoring_a_container_diff_maps_its_parts_back,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
