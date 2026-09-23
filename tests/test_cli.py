"""Cover the headless CLI's own logic: argument contract, and the report.

Running a diff needs Binary Ninja; what is checked here is everything around
it — that ``--list`` takes one binary and a diff takes two, that a pair's
verdict reaches the right column, and that identical functions stay out of the
output unless asked for. The classification itself comes from ``core.align``
and is covered there.

    .venv-qbindiff-312/bin/python binja_diff/tests/test_cli.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()

from binja_diff.core import align  # noqa: E402
from binja_diff.core.engine import DiffResult, FunctionRef, MatchRecord  # noqa: E402

_stubs = _bootstrap.stubs()
BasicBlock, BinaryView, Function, Token = (
    _stubs.BasicBlock,
    _stubs.BinaryView,
    _stubs.Function,
    _stubs.Token,
)
TT = _stubs.InstructionTextTokenType


def _load_cli():
    """The CLI is a script with a hyphen in its name, so import it by path.

    That hyphen is deliberate: the file sits in the plugin directory, and a
    name Python could import is a name that can shadow a real package.
    """

    path = Path(__file__).resolve().parent.parent / "binja-diff.py"
    spec = importlib.util.spec_from_file_location("_binja_diff_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def func(bv, addr: int, name: str, texts: list[str]) -> Function:
    block = BasicBlock(addr, [([Token(TT.InstructionToken, text)], 4) for text in texts])
    function = Function(bv, addr, name, [block])
    bv.functions.append(function)
    return function


def result_with(pairs) -> DiffResult:
    """A DiffResult over stub views, one match per (name, left, right) triple."""

    primary, secondary = BinaryView("/tmp/a.bin"), BinaryView("/tmp/b.bin")
    matches = []
    for index, (name, left_texts, right_texts) in enumerate(pairs):
        addr = 0x1000 + index * 0x100
        func(primary, addr, name, left_texts)
        func(secondary, addr, name, right_texts)
        matches.append(
            MatchRecord(
                primary=FunctionRef(addr, name),
                secondary=FunctionRef(addr, name),
                similarity=1.0,
                confidence=1.0,
            )
        )
    return DiffResult(primary_bv=primary, secondary_bv=secondary, similarity=1.0, matches=matches)


def test_argument_contract():
    print("one binary lists, two binaries diff")
    args = cli.parse_args(["--list", "kernelcache"])
    check("list takes one", args.list and args.primary == "kernelcache" and not args.secondary)

    args = cli.parse_args(["--part", "SEPD", "a.bin", "b.bin"])
    check("a part can be chosen", args.part == "SEPD", f"{args.part}")
    check("both sides kept", (args.primary, args.secondary) == ("a.bin", "b.bin"))
    check("classification is the default", not args.no_classify)

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            cli.parse_args(["only-one.bin"])
            check("one binary without --list is refused", False)
        except SystemExit as exc:
            check("one binary without --list is refused", exc.code == 2, f"exit {exc.code}")


def test_only_differences_are_listed():
    """The interesting rows are the ones that differ; identical pairs are the
    bulk of any real diff and would bury them."""

    print("identical pairs are counted, not printed")
    result = result_with(
        [
            ("same", ["mov x0, x1", "ret"], ["mov x0, x1", "ret"]),
            ("moved", ["bl 0x1234", "ret"], ["bl 0x5678", "ret"]),
            ("rewritten", ["mov x0, x1", "ret"], ["add x0, x1, x2", "bl 0x99", "ret"]),
        ]
    )

    counts, rows = cli.classify(result, align, limit=0, show_all=False)
    check("every pair is counted", sum(counts.values()) == 3, f"{counts}")
    check("identical recognized", counts.get("identical") == 1, f"{counts}")
    check("offsets recognized", counts.get("offsets only") == 1, f"{counts}")
    check("change recognized", counts.get("changed") == 1, f"{counts}")

    printed = [match.primary.name for _status, match, _same, _review in rows]
    check("only what differs is listed", printed == ["rewritten", "moved"], f"{printed}")
    check("changed comes first", rows[0][0] == "changed", f"{rows[0][0]}")
    # The reported share is the line comparison, not QBinDiff's score: the
    # "moved" pair is two lines, one of which only changed an address.
    moved = next(row for row in rows if row[1].primary.name == "moved")
    check("a real similarity travels with the row", moved[2] == 1.0, f"{moved[2]}")

    _counts, everything = cli.classify(result, align, limit=0, show_all=True)
    check("--all lists identical too", len(everything) == 3, f"{len(everything)}")
    _counts, capped = cli.classify(result, align, limit=1, show_all=True)
    check("--limit caps the list", len(capped) == 1, f"{len(capped)}")


def test_report_says_what_happened():
    print("the report carries the counts, the timings and the rows")
    result = result_with([("moved", ["bl 0x1234"], ["bl 0x5678"])])
    result.timings = [("Extracting features", 4.25)]
    counts, rows = cli.classify(result, align, limit=0, show_all=False)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.report(result, counts, rows, show_all=False)
    text = out.getvalue()

    check("matched count", "matched    : 1" in text, text)
    check("what was compared", "compared   : 1 primary, 1 secondary" in text, text)
    check("status counts", "offsets only 1" in text, text)
    check("timings", "Extracting features" in text and "4.2s" in text, text)
    check("the differing pair", "moved" in text and "0x1000" in text, text)
    check("as a percentage of unchanged lines", "100%" in text, text)

    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        cli.report(result_with([("same", ["ret"], ["ret"])]), {"identical": 1}, [], show_all=False)
    check("nothing to show says so", "no differences" in quiet.getvalue(), quiet.getvalue())


def test_low_evidence_pair_is_visible():
    print("low-evidence matches are visible even when confidence is high")
    result = result_with([("doubt", ["mov x0, x1"], ["brk #0x1"])])
    old = result.matches[0]
    result.matches[0] = MatchRecord(old.primary, old.secondary, 0.0, 0.99)
    counts, rows = cli.classify(result, align, limit=0, show_all=False)
    check("review counted", counts.get("_review") == 1)
    check("row carries review flag", rows[0][3] is True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.report(result, counts, rows, show_all=False)
    check("review marker explained", "verify pair: 1" in out.getvalue())
    check("row marked", "changed ?" in out.getvalue())


def main() -> int:
    for test in (
        test_argument_contract,
        test_only_differences_are_listed,
        test_report_says_what_happened,
        test_low_evidence_pair_is_visible,
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
