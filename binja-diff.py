#!/usr/bin/env python3
# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Diff two binaries without the UI.

The same engine the plugin runs, driven from a shell: useful over SSH, in a
build, or against a pair of firmware images too large to sit and watch. It
needs a *headless* Binary Ninja licence, which the Personal edition does not
grant, and the interpreter must be one QBinDiff is installed in.

    binja-diff.py a.bin b.bin
    binja-diff.py --list kernelcache            # what is in it
    binja-diff.py --part AppleSEPManager kc.a kc.b
    binja-diff.py --json out.bndiff.json a.bndb b.bndb
    binja-diff.py --load out.bndiff.json a.bndb b.bndb   # re-report, no matching
    binja-diff.py --format csv --exit-code a.bin b.bin   # for a build

Containers (a kernelcache, a SEP image) hold no code until a part is mapped in,
so ``--part`` is how a single kext or module gets diffed, and it is what makes
the diff finish: matching is quadratic. With no ``--part``, the parts already
loaded in the primary are mirrored onto the secondary; when neither side has
any, the whole file is loaded where that is feasible.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Python puts a script's own directory on sys.path, and this script lives in the
# plugin directory: core/, ui/ and tests/ would then shadow any top-level
# package of those names. That is not hypothetical — qbindiff imports a package
# called `bindiff`, which this file shadowed when it was named bindiff.py. The
# package below is registered by path and needs no sys.path entry, so drop it.
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != HERE]

#: Statuses worth printing without --all: what the reader is looking for.
INTERESTING = ("changed", "unclassified", "offsets only")

#: QBinDiff's distance functions, spelled out here because the parser runs
#: before anything that could import qbindiff. test_cli checks the list against
#: the real enum.
DISTANCES = ("haussmann", "canberra", "cosine", "euclidean")


def _ratio(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"{text} is not between 0 and 1")
    return value


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text} is not a positive count")
    return value


def _count(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return value


def _package():
    """Register the checkout as ``binja_diff`` and return it.

    The directory name is whatever the plugin folder was called — inside Binary
    Ninja that is the package name, and from here it may not even be a valid
    identifier ("binja-diff-2"). Registering it by path sidesteps both.
    """

    if "binja_diff" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "binja_diff", HERE / "__init__.py", submodule_search_locations=[str(HERE)]
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules["binja_diff"] = package
        spec.loader.exec_module(package)
    return sys.modules["binja_diff"]


class Progress:
    """Phase reporting on stderr, so stdout stays a clean report.

    Overwrites one line on a terminal and prints each phase once when piped,
    which keeps a log from filling with a thousand percentages.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.tty = sys.stderr.isatty()
        self.label = ""
        self.shown = -1.0
        self.last = 0.0

    def __call__(self, label: str, fraction: float = -1.0) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        new_phase = label != self.label
        if not new_phase and (now - self.last < 0.2 or abs(fraction - self.shown) < 0.02):
            return
        self.label, self.shown, self.last = label, fraction, now
        percent = f" {fraction * 100:5.1f}%" if fraction >= 0 else ""
        if self.tty:
            sys.stderr.write(f"\r\033[K{label}{percent}")
            sys.stderr.flush()
        elif new_phase:
            print(f"{label}{percent}", file=sys.stderr, flush=True)

    def done(self) -> None:
        if self.enabled and self.tty and self.label:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        self.label = ""


def describe(bv, scope) -> str:
    regions = scope.available_regions(bv)
    if not regions:
        return f"{bv.view_type}, {len(bv.functions)} functions"
    loaded = sum(region.loaded for region in regions)
    return f"{bv.view_type}, {len(regions)} parts ({loaded} loaded), {len(bv.functions)} functions"


def list_parts(bv, scope) -> int:
    regions = scope.available_regions(bv)
    if not regions:
        print(f"{bv.file.filename}: not a container ({bv.view_type})")
        return 0
    print(f"{bv.file.filename}: {len(regions)} parts")
    for region in regions:
        extent = f"0x{region.start:x}" if region.start else "-"
        print(f"  {'*' if region.loaded else ' '} {region.name:<48} {extent}")
    print("\n  * = mapped in already. Any part can be diffed with --part NAME.")
    hint = scope.missing_region_hint(bv)
    if hint:
        print(f" {hint.strip()}")
    return 0


def classify(result, align, limit: int, show_all: bool) -> tuple[dict[str, int], list[tuple]]:
    """Per-pair verdicts, and the rows worth printing.

    Statuses come from the basic blocks, the same source the UI classifies
    from, so a headless report and the match table agree.
    """

    counts: dict[str, int] = {}
    rows: list[tuple] = []
    for match in result.matches:
        left = result.primary_bv.get_function_at(match.primary.addr)
        right = result.secondary_bv.get_function_at(match.secondary.addr)
        same = None
        if left is None or right is None:
            status = "missing"
            review = False
        else:
            verdict, aligned = align.classify_pair(left, right)
            status = verdict.value if verdict is not None else "unknown"
            if aligned:
                same = align.text_similarity(aligned)
            review = align.pairing_needs_review(match.similarity, same)
        if review:
            counts["_review"] = counts.get("_review", 0) + 1
        counts[status] = counts.get(status, 0) + 1
        if show_all or status in INTERESTING:
            rows.append((status, match, same, review))
    order = {status: index for index, status in enumerate(INTERESTING)}
    rows.sort(key=lambda row: (order.get(row[0], len(order)), row[1].primary.addr))
    return counts, rows[:limit] if limit else rows


def _row_record(status: str, match, same, review: bool) -> dict:
    """One matched pair as the machine-readable formats print it."""

    return {
        "status": status,
        "primary_addr": match.primary.addr,
        "primary_name": match.primary.name,
        "secondary_addr": match.secondary.addr,
        "secondary_name": match.secondary.name,
        "line_similarity": None if same is None else round(same, 4),
        "verify_pair": review,
        "qbindiff_similarity": round(match.similarity, 6),
        "confidence": round(match.confidence, 6),
        "manual": match.manual,
    }


def report_json(result, counts: dict[str, int], rows: list[tuple], show_all: bool) -> str:
    """The report as one JSON document, for a script to read.

    Not the saved-diff format (``--json`` writes that): this carries the
    verdicts and the line similarity, which a saved diff deliberately does not.
    """

    document = {
        "primary": result.primary_bv.file.filename,
        "secondary": result.secondary_bv.file.filename,
        "similarity": round(result.similarity, 6),
        "matched": len(result.matches),
        "counts": {k: v for k, v in sorted(counts.items()) if not k.startswith("_")},
        "verify_pair": counts.get("_review", 0),
        "rows": [_row_record(*row) for row in rows],
        "primary_only": [{"addr": ref.addr, "name": ref.name} for ref in result.primary_unmatched],
        "secondary_only": [
            {"addr": ref.addr, "name": ref.name} for ref in result.secondary_unmatched
        ],
        "timings": [
            {"phase": label, "seconds": round(seconds, 3)} for label, seconds in result.timings
        ],
    }
    return json.dumps(document, indent=2)


CSV_COLUMNS = (
    "status",
    "primary_addr",
    "primary_name",
    "secondary_addr",
    "secondary_name",
    "line_similarity",
    "verify_pair",
    "qbindiff_similarity",
    "confidence",
    "manual",
)


def report_csv(result, rows: list[tuple], show_all: bool, out) -> None:
    """One line per pair, plus the unmatched functions with an empty other side."""

    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for status, match, same, review in rows:
        record = _row_record(status, match, same, review)
        record["primary_addr"] = f"0x{match.primary.addr:x}"
        record["secondary_addr"] = f"0x{match.secondary.addr:x}"
        writer.writerow(record)
    if show_all:
        for ref in result.primary_unmatched:
            writer.writerow(
                {
                    "status": "primary only",
                    "primary_addr": f"0x{ref.addr:x}",
                    "primary_name": ref.name,
                }
            )
        for ref in result.secondary_unmatched:
            writer.writerow(
                {
                    "status": "secondary only",
                    "secondary_addr": f"0x{ref.addr:x}",
                    "secondary_name": ref.name,
                }
            )


def has_differences(result, counts: dict[str, int]) -> bool:
    """What ``--exit-code`` reports: a real change, a pair too big to grade, or
    a function on one side only. Offsets-only pairs are not differences."""

    return bool(
        counts.get("changed")
        or counts.get("unknown")
        or counts.get("missing")
        or result.primary_unmatched
        or result.secondary_unmatched
    )


def report(result, counts: dict[str, int], rows: list[tuple], show_all: bool, context=None) -> None:
    total = len(result.matches)
    print()
    print(f"similarity : {result.similarity:.3f} (QBinDiff graph score, not match precision)")
    print(f"matched    : {total}")
    if counts:
        summary = "  ".join(
            f"{status} {count}"
            for status, count in sorted(counts.items())
            if not status.startswith("_")
        )
        print(f"             {summary}")
        if counts.get("_review"):
            print(f"verify pair: {counts['_review']} low-evidence match(es), marked ? below")
    print(
        f"unmatched  : {len(result.primary_unmatched)} primary, "
        f"{len(result.secondary_unmatched)} secondary"
    )
    # Matched plus unmatched is what was actually compared, which is not the
    # view's function count when the diff was scoped to one part — and was not
    # it either, for a while, when a container had been analyzed before its
    # parts were mapped in. Printing it keeps that arithmetic checkable.
    print(
        f"compared   : {total + len(result.primary_unmatched)} primary, "
        f"{total + len(result.secondary_unmatched)} secondary functions"
    )
    if result.timings:
        print("timings    :")
        for label, seconds in result.timings:
            print(f"    {label:<32} {seconds:7.1f}s")
        print(f"    {'total':<32} {result.duration:7.1f}s")

    if rows:
        print()
        # The percentage is the share of lines that did not change, not QBinDiff's
        # score: that one is a MinHash over whole basic blocks and reads 0.000
        # for a one-block function that gained a single instruction.
        print(f"{'status':<14} {'primary':<18} {'secondary':<18} {'sim%':>5}  name")
        for status, match, same, review in rows:
            name = match.primary.name
            if match.secondary.name != name:
                name = f"{name} -> {match.secondary.name}"
            share = f"{same * 100:4.0f}%" if same is not None else "   -"
            status_label = status + (" ?" if review else "") + (" (manual)" if match.manual else "")
            print(
                f"{status_label:<14} 0x{match.primary.addr:<16x} 0x{match.secondary.addr:<16x} "
                f"{share:>5}  {name}"
            )
            if context is not None and status == "changed":
                delta = context(match)
                if delta is not None and delta.differs:
                    for line in delta.summary().splitlines():
                        print(f"{'':<14} {line}")
    elif counts:
        print("\nno differences in any matched function")

    if show_all:
        for label, refs in (
            ("primary only", result.primary_unmatched),
            ("secondary only", result.secondary_unmatched),
        ):
            if refs:
                print(f"\n{label}:")
                for ref in refs:
                    print(f"  0x{ref.addr:<16x} {ref.name}")


def restore(path: str, primary, secondary, engine, scope, persist, progress):
    """A saved diff rebuilt against two freshly opened views, no matching.

    What the UI's RestoreTask does, minus the task: the parts a container diff
    covered are mapped back into both views before any saved address can
    resolve, then both are analyzed once.
    """

    saved = persist.read_file(path)
    for label, view in (("primary", primary), ("secondary", secondary)):
        drift = (saved.primary if label == "primary" else saved.secondary).differences(view)
        if drift:
            print(
                f"warning: the {label} binary differs from the saved diff: {'; '.join(drift)}",
                file=sys.stderr,
            )
    if saved.scope:
        progress(f"Loading {', '.join(saved.scope)}")
        for label, view in (("primary", primary), ("secondary", secondary)):
            missing = scope.ensure_named_loaded(view, saved.scope)
            if missing:
                raise RuntimeError(f"The {label} binary has no part named {', '.join(missing)}")
        for view in (primary, secondary):
            engine.wait_for_analysis(view, progress=progress)
    return saved.to_result(primary, secondary)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="binja-diff.py",
        description=__doc__.split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Containers hold no code until a part is mapped in, so --part is what\n"
            "makes a kernelcache diff finish at all: one kext out of 256, rather\n"
            "than 256 against 256. Both raw binaries and .bndb databases work.\n"
        ),
    )
    parser.add_argument("primary", help="first binary, or .bndb")
    parser.add_argument("secondary", nargs="?", help="second binary, or .bndb")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the parts of a container (kexts, SEP modules) and exit",
    )
    parser.add_argument(
        "--part",
        metavar="NAME",
        help="diff only this kext or SEP module, loading it on both sides",
    )
    parser.add_argument("--json", metavar="PATH", help="write the result as .bndiff.json")
    parser.add_argument(
        "--load",
        metavar="PATH",
        help="report a saved .bndiff.json against the two binaries instead of matching again",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="report format on stdout (default text)",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 1 when functions changed or are unmatched, 0 when not, 2 on error",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="under each changed pair, which callees and strings only one side has",
    )
    parser.add_argument(
        "--all", action="store_true", help="list every matched pair and the unmatched functions"
    )
    parser.add_argument(
        "--limit", type=_count, default=0, metavar="N", help="print at most N pairs (0 = no limit)"
    )
    parser.add_argument(
        "--no-classify",
        action="store_true",
        help="skip per-function comparison: matches only, much faster on a large pair",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="no progress, and no engine warnings, on stderr"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="the engine's info-level log on stderr too"
    )

    tuning = parser.add_argument_group("matching")
    tuning.add_argument(
        "--sparsity",
        type=_ratio,
        metavar="F",
        help="sparsity ratio in [0, 1] (default 0.15, raised for large binaries)",
    )
    tuning.add_argument(
        "--tradeoff", type=_ratio, metavar="F", help="feature/structure tradeoff in [0, 1]"
    )
    tuning.add_argument(
        "--maxiter", type=_positive, metavar="N", help="belief propagation iterations"
    )
    tuning.add_argument(
        "--distance",
        metavar="NAME",
        choices=DISTANCES,
        help=f"distance function (default haussmann): {', '.join(DISTANCES)}",
    )
    tuning.add_argument(
        "--feature", action="append", default=[], metavar="KEY", help="extra feature, repeatable"
    )

    args = parser.parse_args(argv)
    if not args.list and not args.secondary:
        parser.error("two binaries are required (or --list with one)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        import binaryninja
    except ImportError as exc:
        print(f"Binary Ninja is not importable: {exc}", file=sys.stderr)
        print("Run this with the interpreter Binary Ninja's API is installed in.", file=sys.stderr)
        return 1

    package = _package()
    reason = package.dependency_error()
    if reason is not None:
        print(f"error: {reason}", file=sys.stderr)
        print(
            "QBinDiff must be importable by this interpreter: pip install qbindiff",
            file=sys.stderr,
        )
        return 1

    # Headless Binary Ninja writes its log to stderr only when stderr is a
    # terminal. In CI, or with `2>log`, every warning the engine raises — a part
    # missing from one side, a feature nobody knows, the large-diff memory
    # note — would otherwise vanish, and the report would print a clean result
    # over a quietly truncated diff.
    if not args.quiet:
        level = binaryninja.LogLevel.InfoLog if args.verbose else binaryninja.LogLevel.WarningLog
        binaryninja.log_to_stderr(level)

    engine = importlib.import_module("binja_diff.core.engine")
    scope = importlib.import_module("binja_diff.core.scope")
    align = importlib.import_module("binja_diff.core.align")
    persist = importlib.import_module("binja_diff.core.persist")

    context = importlib.import_module("binja_diff.core.context")

    parser_name = Path(sys.argv[0]).name or "binja-diff.py"
    progress = Progress(not args.quiet and args.format == "text")
    primary = secondary = None
    error_code = 2 if args.exit_code else 1
    try:
        primary = engine.load_secondary(args.primary, progress=lambda text: progress(text))
        if primary is None:
            return 1
        if args.list:
            progress.done()
            return list_parts(primary, scope)

        secondary = engine.load_secondary(args.secondary, progress=lambda text: progress(text))
        if secondary is None:
            return 1
        progress.done()

        text = args.format == "text"
        if text:
            print(f"primary    : {args.primary}\n             {describe(primary, scope)}")
            print(f"secondary  : {args.secondary}\n             {describe(secondary, scope)}")
            if args.part:
                print(f"scope      : {args.part}")
            elif scope.available_regions(primary):
                print("scope      : everything loaded in the primary")

        options = engine.DiffOptions(
            **{
                name: value
                for name, value in (
                    ("sparsity_ratio", args.sparsity),
                    ("tradeoff", args.tradeoff),
                    ("maxiter", args.maxiter),
                    ("distance", args.distance),
                    ("features", tuple(args.feature) or None),
                )
                if value is not None
            }
        )

        started = time.monotonic()
        if args.load:
            result = restore(args.load, primary, secondary, engine, scope, persist, progress)
        else:
            result = engine.run_diff(
                primary, secondary, options=options, progress=progress, region_name=args.part
            )
        progress.done()
        if result is None:
            return 1

        counts: dict[str, int] = {}
        rows: list[tuple] = []
        if not args.no_classify:
            progress("Comparing functions")
            counts, rows = classify(result, align, args.limit, args.all)
            progress.done()

        if args.format == "json":
            print(report_json(result, counts, rows, args.all))
        elif args.format == "csv":
            report_csv(result, rows, args.all, sys.stdout)
        else:
            delta = None
            if args.context:

                def delta(match):
                    left = result.primary_bv.get_function_at(match.primary.addr)
                    right = result.secondary_bv.get_function_at(match.secondary.addr)
                    if left is None or right is None:
                        return None
                    return context.context_delta(result, left, right)

            report(result, counts, rows, args.all, context=delta)
            print(f"\nelapsed    : {time.monotonic() - started:.1f}s")

        if args.json:
            persist.write_file(persist.SavedDiff.from_result(result, options), args.json)
            if text:
                print(f"written    : {args.json}")
        if args.exit_code and not args.no_classify:
            return 1 if has_differences(result, counts) else 0
        return 0
    except KeyboardInterrupt:
        progress.done()
        print("cancelled", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, ImportError, KeyError) as exc:
        progress.done()
        print(f"error: {exc}", file=sys.stderr)
        # The engine's advice is "load it in the primary", which is what a UI
        # user does; here the equivalent is choosing a part on the command line.
        if not args.part and primary is not None and scope.available_regions(primary):
            print(
                f"try: {parser_name} --list {args.primary}   then --part NAME",
                file=sys.stderr,
            )
        return error_code
    finally:
        # The views are ours: nothing else will close them.
        for view in (primary, secondary):
            if view is not None:
                view.file.close()


if __name__ == "__main__":
    raise SystemExit(main())
