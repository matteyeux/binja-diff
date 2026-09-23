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

Containers (a kernelcache, a SEP image) hold no code until a part is mapped in,
so ``--part`` is how a single kext or module gets diffed, and it is what makes
the diff finish: matching is quadratic. With no ``--part``, the parts already
loaded in the primary are mirrored onto the secondary; when neither side has
any, the whole file is loaded where that is feasible.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
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
INTERESTING = ("changed", "differs", "offsets only")


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
        else:
            verdict, aligned = align.classify_pair(left, right)
            status = verdict.value if verdict is not None else "unknown"
            if aligned:
                same = align.text_similarity(aligned)
        counts[status] = counts.get(status, 0) + 1
        if show_all or status in INTERESTING:
            rows.append((status, match, same))
    order = {status: index for index, status in enumerate(INTERESTING)}
    rows.sort(key=lambda row: (order.get(row[0], len(order)), row[1].primary.addr))
    return counts, rows[:limit] if limit else rows


def report(result, counts: dict[str, int], rows: list[tuple], show_all: bool) -> None:
    total = len(result.matches)
    print()
    print(f"similarity : {result.similarity:.3f}")
    print(f"matched    : {total}")
    if counts:
        summary = "  ".join(f"{status} {count}" for status, count in sorted(counts.items()))
        print(f"             {summary}")
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
        for status, match, same in rows:
            name = match.primary.name
            if match.secondary.name != name:
                name = f"{name} -> {match.secondary.name}"
            share = f"{same * 100:4.0f}%" if same is not None else "   -"
            print(
                f"{status:<14} 0x{match.primary.addr:<16x} 0x{match.secondary.addr:<16x} "
                f"{share:>5}  {name}"
            )
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
        "--all", action="store_true", help="list every matched pair and the unmatched functions"
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N", help="print at most N pairs (0 = no limit)"
    )
    parser.add_argument(
        "--no-classify",
        action="store_true",
        help="skip per-function comparison: matches only, much faster on a large pair",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="no progress on stderr")

    tuning = parser.add_argument_group("matching")
    tuning.add_argument(
        "--sparsity",
        type=float,
        metavar="F",
        help="sparsity ratio (default 0.15, raised for large binaries)",
    )
    tuning.add_argument("--tradeoff", type=float, metavar="F", help="feature/structure tradeoff")
    tuning.add_argument("--maxiter", type=int, metavar="N", help="belief propagation iterations")
    tuning.add_argument("--distance", metavar="NAME", help="distance function (default haussmann)")
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
        import binaryninja  # noqa: F401
    except ImportError as exc:
        print(f"Binary Ninja is not importable: {exc}", file=sys.stderr)
        print("Run this with the interpreter Binary Ninja's API is installed in.", file=sys.stderr)
        return 1

    _package()
    engine = importlib.import_module("binja_diff.core.engine")
    scope = importlib.import_module("binja_diff.core.scope")
    align = importlib.import_module("binja_diff.core.align")
    persist = importlib.import_module("binja_diff.core.persist")

    parser_name = Path(sys.argv[0]).name or "binja-diff.py"
    progress = Progress(not args.quiet)
    primary = secondary = None
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

        report(result, counts, rows, args.all)
        print(f"\nelapsed    : {time.monotonic() - started:.1f}s")

        if args.json:
            persist.write_file(persist.SavedDiff.from_result(result, options), args.json)
            print(f"written    : {args.json}")
        return 0
    except KeyboardInterrupt:
        progress.done()
        print("cancelled", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError) as exc:
        progress.done()
        print(f"error: {exc}", file=sys.stderr)
        # The engine's advice is "load it in the primary", which is what a UI
        # user does; here the equivalent is choosing a part on the command line.
        if not args.part and primary is not None and scope.available_regions(primary):
            print(
                f"try: {parser_name} --list {args.primary}   then --part NAME",
                file=sys.stderr,
            )
        return 1
    finally:
        # The views are ours: nothing else will close them.
        for view in (primary, secondary):
            if view is not None:
                view.file.close()


if __name__ == "__main__":
    raise SystemExit(main())
