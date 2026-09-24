# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Driving QBinDiff from inside Binary Ninja.

Everything here runs off the UI thread. The only contract with the UI is the
``on_done`` / ``on_error`` callbacks, which are marshalled back to the main
thread by the caller.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import logging
import re
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable

import binaryninja
from binaryninja import BinaryView, BackgroundTaskThread
from binaryninja import log_debug, log_error, log_info, log_warn
from binaryninja.enums import AnalysisState

if TYPE_CHECKING:
    from collections.abc import Iterable

    from qbindiff import Mapping
    from qbindiff.loader import Function as QBFunction, Program as QBProgram
    from qbindiff.types import Addr, Idx, SimMatrix


#: Structural feature keys registered on top of QBinDiff's own defaults.
#: The stock defaults (FuncName, Address, DatName, Constant) carry almost no
#: signal on a stripped binary at a different base address: names are gone,
#: addresses moved, and only data references and constants remain. These
#: describe the code itself — mnemonic histograms, CFG size and complexity,
#: call-graph fan-in/out, and which imports a function calls — and are cheap to
#: extract. `imp` is registered as backend.ImportCalls rather than QBinDiff's
#: ImpName, which is not in its FEATURES table and crashes on the call targets
#: this backend reports that are not functions.
DEFAULT_EXTRA_FEATURES = ("M", "Mt", "bnb", "cc", "cnb", "pnb", "imp")

#: QBinDiff defaults that run_diff leaves out. `addr` compares function
#: addresses with the same ratio as every numeric feature, |x - y| / (|x| + |y|),
#: and two addresses in one 64-bit image score ~0.9999 whatever they are: a
#: near-constant that only flattens the numeric features it is pooled with.
#: Measured on Lua builds with symbol ground truth, dropping it at sparsity 0.15
#: took wrong matches from 2/5/39 to 0/3/32 (patch / -O1 vs -O2 / 5.3 vs 5.4).
#: Still available as an explicit extra feature.
EXCLUDED_DEFAULT_FEATURES = ("addr",)

#: Feature keys offered in the UI beyond what run_diff registers by default.
#: spp and Gmd largely repeat what M and bnb/cc already measure, so they stay
#: opt-in rather than diluting the default weighting.
EXTRA_FEATURE_KEYS = ("spp", "Gmd", "jnb", "Gp")

#: Auto-generated name shapes (Binary Ninja's ``sub_401000``, IDA's
#: ``FUN_00401000``). Deliberately a superset of the filter QBinDiff's FuncName
#: feature applies: an address-shaped name is refused as an anchor even when
#: the digits differ from the function's own address, because two builds that
#: auto-name a function identically only prove their code sits at the same
#: offset — precisely the assumption that breaks when code is inserted.
_AUTO_NAME_RE = re.compile(r"^(sub|fun)_[0-9a-f]+$", re.IGNORECASE)


def is_generated_name(name: str) -> bool:
    """Whether a function name is Binary Ninja's placeholder rather than a symbol.

    Also what `core.symbols` uses to decide there is nothing worth porting, and
    that a target name may be overwritten without asking.
    """

    return bool(_AUTO_NAME_RE.match(name))


def _anchor_names(program: QBProgram) -> dict[str, Addr]:
    """Function names trustworthy enough to anchor a match: real (not
    auto-generated), unique within the program, and not an import, which
    QBinDiff's built-in prepass already anchors."""

    names: dict[str, Addr] = {}
    duplicated: set[str] = set()
    for addr, func in program.items():
        if func.is_import() or is_generated_name(func.name):
            continue
        if func.name in names:
            duplicated.add(func.name)
        names[func.name] = addr
    for name in duplicated:
        del names[name]
    return names


def match_named_functions(
    sim_matrix: SimMatrix,
    primary: QBProgram,
    secondary: QBProgram,
    primary_mapping: dict[Addr, Idx],
    secondary_mapping: dict[Addr, Idx],
    primary_features: dict | None = None,
    secondary_features: dict | None = None,
    *,
    primary_bv: BinaryView,
    secondary_bv: BinaryView,
    exact_pairs: set[tuple[int, int]] | None = None,
) -> None:
    """Anchor unique, byte-identical code and cautiously use matching names.

    The exact-code anchors rescue repeated small helpers whose generic graph
    features are indistinguishable. Same-address exact code is pinned first;
    code moved across addresses is pinned only when unique on both sides, so a
    ubiquitous return stub cannot force an arbitrary pair. Names are weaker:
    user annotations can refer to different code in two databases. A name is
    only pinned when the functions also have comparable size and line content.

    This runs after feature extraction. QBinDiff's prepass can divide by zero
    when every function is anchored, while its postpass has no such problem.
    """

    from functools import lru_cache

    from . import align

    def code_signatures(bv: BinaryView, program: QBProgram) -> dict[int, tuple[str, bytes]]:
        signatures: dict[int, tuple[str, bytes]] = {}
        for addr, _ in program.items():
            func = bv.get_function_at(addr)
            if func is None:
                continue
            blocks = sorted(func.basic_blocks, key=lambda block: block.start)
            if not blocks or sum(block.length for block in blocks) < 8:
                continue
            chunks = [bv.read(block.start, block.length) for block in blocks]
            if any(
                chunk is None or len(chunk) != block.length
                for chunk, block in zip(chunks, blocks, strict=True)
            ):
                continue
            code = b"".join(chunks)
            arch = getattr(func, "arch", bv.arch)
            key = (getattr(arch, "name", ""), hashlib.sha256(code).digest())
            signatures[addr] = key
        return signatures

    def unique_code(
        signatures: dict[int, tuple[str, bytes]], excluded: set[int]
    ) -> dict[tuple[str, bytes], int]:
        found: dict[tuple[str, bytes], int] = {}
        duplicates: set[tuple[str, bytes]] = set()
        for addr, key in signatures.items():
            if addr in excluded:
                continue
            if key in found:
                duplicates.add(key)
            else:
                found[key] = addr
        for key in duplicates:
            del found[key]
        return found

    def pin(pairs: list[tuple[int, int]]) -> None:
        if not pairs:
            return
        rows, cols = zip(*pairs, strict=True)
        sim_matrix[rows, :] = 0
        sim_matrix[:, cols] = 0
        sim_matrix[rows, cols] = 1

    left_signatures = code_signatures(primary_bv, primary)
    right_signatures = code_signatures(secondary_bv, secondary)
    same_addrs: set[int] = set()
    for addr, key in left_signatures.items():
        if right_signatures.get(addr) != key:
            continue
        left_name, right_name = primary[addr].name, secondary[addr].name
        if left_name != right_name and not (
            is_generated_name(left_name) or is_generated_name(right_name)
        ):
            continue
        same_addrs.add(addr)
    left_code = unique_code(left_signatures, same_addrs)
    right_code = unique_code(right_signatures, same_addrs)
    exact = [(primary_mapping[addr], secondary_mapping[addr]) for addr in same_addrs] + [
        (primary_mapping[left_addr], secondary_mapping[right_code[key]])
        for key, left_addr in left_code.items()
        if key in right_code
    ]
    pin(exact)
    if exact_pairs is not None:
        exact_pairs.update((addr, addr) for addr in same_addrs)
        exact_pairs.update(
            (left_addr, right_code[key])
            for key, left_addr in left_code.items()
            if key in right_code
        )
    used_left = {pair[0] for pair in exact}
    used_right = {pair[1] for pair in exact}

    @lru_cache(maxsize=512)
    def lines(bv_side: int, addr: int):
        bv = primary_bv if bv_side == 0 else secondary_bv
        func = bv.get_function_at(addr)
        if func is None:
            return []
        return align.function_instruction_lines(func, "Disassembly")

    @lru_cache(maxsize=512)
    def quick_lines(bv_side: int, addr: int) -> tuple[str, ...]:
        bv = primary_bv if bv_side == 0 else secondary_bv
        func = bv.get_function_at(addr)
        if func is None:
            return ()
        return tuple(
            align.normalize_line("".join(token.text for token in tokens))
            for block in sorted(func.basic_blocks, key=lambda block: block.start)
            for tokens, _length in block
        )

    named = []
    secondary_names = _anchor_names(secondary)
    for name, left_addr in _anchor_names(primary).items():
        right_addr = secondary_names.get(name)
        if right_addr is None:
            continue
        row, col = primary_mapping[left_addr], secondary_mapping[right_addr]
        if row in used_left or col in used_right:
            continue
        left = primary_bv.get_function_at(left_addr)
        right = secondary_bv.get_function_at(right_addr)
        if left is None or right is None:
            continue
        n_left = sum(block.instruction_count for block in left.basic_blocks)
        n_right = sum(block.instruction_count for block in right.basic_blocks)
        if min(n_left, n_right) == 0 or max(n_left, n_right) > 512:
            continue
        size_ratio = min(n_left, n_right) / max(n_left, n_right)
        if size_ratio < 0.65:
            continue
        feature_score = float(sim_matrix[row, col])
        if 0 <= feature_score < 0.2:
            continue
        if feature_score >= 0.75 and size_ratio >= 0.8:
            left_quick, right_quick = quick_lines(0, left_addr), quick_lines(1, right_addr)
            if (
                left_quick
                and right_quick
                and difflib.SequenceMatcher(None, left_quick, right_quick, autojunk=False).ratio()
                >= 0.8
            ):
                named.append((row, col))
                continue
        left_lines, right_lines = lines(0, left_addr), lines(1, right_addr)
        if not left_lines or not right_lines:
            continue
        if align.text_similarity(align.align_lines(left_lines, right_lines)) < 0.35:
            continue
        named.append((row, col))
    pin(named)
    log_info(
        f"Anchored {len(exact)} unique exact-code and {len(named)} verified-name pairs",
        "QBinDiff",
    )


def far_from_local_anchors(
    primary_addr: int, secondary_addr: int, exact_pairs: list[tuple[int, int]]
) -> bool:
    """Whether a low-evidence pair contradicts nearby exact-code landmarks.

    Reordering code is legitimate, so location alone never rejects a match.
    This check is only used after both code comparisons find almost nothing.
    Two close, order-preserving exact anchors are needed to establish locality.
    """

    index = bisect.bisect_left(exact_pairs, (primary_addr, -1))
    if index == 0 or index == len(exact_pairs):
        return False
    left, right = exact_pairs[index - 1], exact_pairs[index]
    gap = right[0] - left[0]
    if gap > 0x10000 or right[1] < left[1]:
        return False
    margin = max(0x1000, gap // 2)
    return not left[1] - margin <= secondary_addr <= right[1] + margin


def demote_unsubstantiated_matches(result: DiffResult, exact_pairs: set[tuple[int, int]]) -> None:
    """Leave implausible forced matches unmatched on both sides.

    QBinDiff's assignment can pair every function of the smaller binary even
    when its own similarity is zero. Only demote when line comparison also
    finds little in common and exact-code neighbors contradict the location.
    A genuine large rewrite near its old code remains paired for review.
    """

    from . import align

    landmarks = sorted(exact_pairs)
    kept: list[MatchRecord] = []
    removed: list[MatchRecord] = []
    for match in result.matches:
        if match.similarity >= 0.05 or (
            match.primary.name == match.secondary.name and not is_generated_name(match.primary.name)
        ):
            kept.append(match)
            continue
        if not far_from_local_anchors(match.primary.addr, match.secondary.addr, landmarks):
            kept.append(match)
            continue
        left = result.primary_bv.get_function_at(match.primary.addr)
        right = result.secondary_bv.get_function_at(match.secondary.addr)
        if left is None or right is None:
            kept.append(match)
            continue
        try:
            _status, rows = align.classify_pair(left, right)
        except Exception:
            kept.append(match)
            continue
        if not rows or align.text_similarity(rows) >= 0.25:
            kept.append(match)
            continue
        removed.append(match)
    if not removed:
        return
    result.matches = kept
    result.primary_unmatched.extend(match.primary for match in removed)
    result.secondary_unmatched.extend(match.secondary for match in removed)
    result.primary_unmatched.sort(key=lambda ref: ref.addr)
    result.secondary_unmatched.sort(key=lambda ref: ref.addr)
    result.reindex()
    log_warn(
        f"Left {len(removed)} low-evidence, out-of-region pair(s) unmatched",
        "QBinDiff",
    )


class _BinjaLogHandler(logging.Handler):
    """Forward qbindiff's root-logger output into the Binary Ninja log."""

    _LEVELS = ((logging.ERROR, log_error), (logging.WARNING, log_warn), (logging.INFO, log_info))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        for level, sink in self._LEVELS:
            if record.levelno >= level:
                sink(message, "QBinDiff")
                return
        log_debug(message, "QBinDiff")


class _log_bridge:
    """Route qbindiff's logging into the Binary Ninja log, and only there.

    qbindiff logs through the module-level ``logging`` functions — the root
    logger — so the handler has to go on root, and the root level has to come
    down to INFO for anything to reach it.

    That level change is also why every other root handler is muted for the
    duration. Root normally sits at WARNING, so qbindiff's INFO records are
    dropped before any handler sees them; lowering it lets them through to
    *all* of them, including whichever one in the process writes to the
    console. Binary Ninja mirrors that stream into its log, so each qbindiff
    line arrived twice — once tagged QBinDiff, once as ``INFO:root:`` from the
    scripting provider. Nothing is lost by muting: this handler forwards the
    same records, at the same levels, and the originals are restored on exit.
    """

    def __init__(self, level: int = logging.INFO):
        self._handler = _BinjaLogHandler()
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._level = level
        self._previous_level: int | None = None
        self._muted: list[tuple[logging.Handler, int]] = []

    def __enter__(self) -> None:
        root = logging.getLogger()
        self._previous_level = root.level
        self._muted = [(handler, handler.level) for handler in root.handlers]
        for handler, _level in self._muted:
            handler.setLevel(logging.CRITICAL + 1)
        root.setLevel(self._level)
        root.addHandler(self._handler)

    def __exit__(self, *_exc) -> None:
        root = logging.getLogger()
        root.removeHandler(self._handler)
        for handler, level in self._muted:
            handler.setLevel(level)
        self._muted = []
        if self._previous_level is not None:
            root.setLevel(self._previous_level)


@dataclass
class DiffOptions:
    """QBinDiff's matching parameters, in one place.

    The defaults are what every diff runs with: the UI constructs this and
    offers no way to change it, and the CLI exposes five of the nine as flags.
    Anything that should be settable belongs here rather than at the call site,
    so the two front ends cannot drift apart.
    """

    sparsity_ratio: float = 0.15
    """Share of the least likely candidate pairs discarded before matching.
    Lower is more accurate and slower. 0.15 against QBinDiff's own 0.6 cut the
    wrong matches on a real version upgrade (Lua 5.3 -> 5.4) from 45 to 32 of
    ~530, at about twice the matching time; going lower gained nothing. Raised
    for large programs, see scale_options_for_size."""
    tradeoff: float = 0.8
    epsilon: float = 0.9
    maxiter: int = 1000
    distance: str = "haussmann"
    normalize: bool = False
    sparse_row: bool = False
    """Sparsify the similarity matrix row by row instead of globally. At high
    sparsity a global cutoff can leave some functions with no candidate at all;
    row-wise keeps the best candidates for every function."""
    auto_sparsity: bool = True
    """Raise sparsity automatically for large binaries (see LARGE_DIFF_*)."""
    features: tuple[str, ...] = ()
    """Extra feature keys on top of the default set (QBinDiff's own defaults plus
    DEFAULT_EXTRA_FEATURES). Empty means defaults only."""


#: QBinDiff's graph matching computes quadratically sized matrices; upstream
#: advises against diffing programs beyond ~10k functions at the default
#: sparsity and recommends 0.99 for large ones.
LARGE_DIFF_FUNCTIONS = 10_000
LARGE_DIFF_SPARSITY = 0.99

#: Candidate pairs belief propagation may keep below LARGE_DIFF_FUNCTIONS: what
#: the former fixed sparsity of 0.6 already admitted at that threshold. Matching
#: memory and time follow the candidate count, so holding to it means the lower
#: default never makes a diff more expensive than the old worst case — and
#: never less accurate than before, since the ratio it implies only reaches 0.6
#: at the threshold itself.
CANDIDATE_BUDGET = round((1 - 0.6) * LARGE_DIFF_FUNCTIONS**2)


def scale_options_for_size(
    options: DiffOptions, primary_count: int, secondary_count: int
) -> DiffOptions:
    """Adapt the matching parameters to the size of the programs.

    Below LARGE_DIFF_FUNCTIONS, sparsity is raised only as far as it takes to
    keep the candidate pairs within CANDIDATE_BUDGET, which leaves the default
    alone up to roughly 6900 x 6900 functions.

    Above LARGE_DIFF_FUNCTIONS on either side, sparsity is raised to
    LARGE_DIFF_SPARSITY and row-wise sparsification is enabled, following
    upstream's guidance for large programs. This only tames the belief
    propagation stage: the dense similarity matrix (4 bytes per function
    pair) is allocated by QBinDiff before sparsification and no setting
    avoids it, so the warning states that cost rather than pretending the
    diff is now cheap.
    """

    largest = max(primary_count, secondary_count)
    if not options.auto_sparsity:
        return options
    if largest <= LARGE_DIFF_FUNCTIONS:
        pairs = primary_count * secondary_count
        needed = 1 - CANDIDATE_BUDGET / pairs if pairs else 0.0
        if needed <= options.sparsity_ratio:
            return options
        needed = round(needed, 3)
        log_info(
            f"{primary_count} x {secondary_count} functions: raising sparsity "
            f"{options.sparsity_ratio} -> {needed} to bound matching cost",
            "QBinDiff",
        )
        return replace(options, sparsity_ratio=needed)
    dense_gib = primary_count * secondary_count * 4 / 1024**3
    if options.sparsity_ratio >= LARGE_DIFF_SPARSITY:
        log_warn(
            f"Large diff ({primary_count} x {secondary_count} functions): the similarity "
            f"matrix alone needs ~{dense_gib:.1f} GiB of RAM",
            "QBinDiff",
        )
        return options
    log_warn(
        f"Large diff ({primary_count} x {secondary_count} functions): raising sparsity "
        f"{options.sparsity_ratio} -> {LARGE_DIFF_SPARSITY} and enabling row-wise "
        f"sparsification to contain matching memory. The similarity matrix alone still "
        f"needs ~{dense_gib:.1f} GiB of RAM",
        "QBinDiff",
    )
    return replace(options, sparsity_ratio=LARGE_DIFF_SPARSITY, sparse_row=True)


#: Progress fraction meaning "this will take a while and nothing can say how
#: long". The UI shows a busy indicator instead of a bar pinned at either end.
INDETERMINATE = -1.0

#: Where QBinDiff's feature phase stops being measurable. Its progress comes
#: from the feature *visitor* alone: once every function has been visited, the
#: same generator goes on to compute one full similarity matrix per registered
#: feature, and yields nothing again until that is finished. The visitor's last
#: step lands one function short of 1000 — within rounding of 1.0 for any
#: program of a few hundred functions, and for anything smaller the stage it
#: guards is instant anyway.
EXTRACTION_DONE = 0.995


def feature_phase(fraction: float) -> tuple[str, float]:
    """What to display for a step of QBinDiff's feature phase.

    Two stages hide behind one generator, and only the first can be measured.
    Leaving the second one labelled "Extracting features" at 100% is what makes
    a long diff look hung: the step it names finished minutes ago.
    """

    if fraction >= EXTRACTION_DONE:
        return "Building the similarity matrix", INDETERMINATE
    return "Extracting features", fraction


def format_duration(seconds: float) -> str:
    """A duration at the precision a human reading a log actually wants."""

    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {rest:02d}s"


@contextmanager
def _timed(timings: list[tuple[str, float]], label: str):
    """Record and log how long a phase took.

    Deliberately in a ``finally``: a cancelled or failed phase is exactly the
    one worth knowing the duration of, and "matching ran for 40 minutes before
    I gave up" is the number that tells you to raise the sparsity.
    """

    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        timings.append((label, elapsed))
        log_info(f"{label} took {format_duration(elapsed)}", "QBinDiff")


@dataclass(frozen=True)
class FunctionRef:
    """One side of a match, reduced to what the UI actually reads back."""

    addr: int
    name: str


@dataclass(frozen=True)
class MatchRecord:
    primary: FunctionRef
    secondary: FunctionRef
    similarity: float
    confidence: float


@dataclass
class DiffResult:
    """A completed diff, indexed for the UI.

    Deliberately holds plain records rather than QBinDiff's ``Mapping``. The
    ``Match`` objects there point back at the two ``Program`` graphs, which
    carry every feature vector extracted during the diff — tens of megabytes
    kept alive for the lifetime of the view, to expose an address and a name.
    Copying the few fields the UI needs also makes a result serializable, which
    is what ``core.persist`` saves and restores.
    """

    primary_bv: BinaryView
    secondary_bv: BinaryView
    similarity: float
    matches: list[MatchRecord] = field(default_factory=list)
    primary_unmatched: list[FunctionRef] = field(default_factory=list)
    secondary_unmatched: list[FunctionRef] = field(default_factory=list)
    #: Wall-clock seconds per phase, in the order they ran. Describes the run
    #: that produced this result, so a restored diff carries only its own load.
    timings: list[tuple[str, float]] = field(default_factory=list)
    #: Address lookups for the table and for navigation. Mapping's own
    #: match_primary/match_secondary are linear scans, far too slow for a
    #: table that queries per row.
    by_primary: dict[int, MatchRecord] = field(init=False, default_factory=dict)
    by_secondary: dict[int, MatchRecord] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        """Rebuild the address lookups. Call after replacing ``matches``."""

        self.by_primary = {m.primary.addr: m for m in self.matches}
        self.by_secondary = {m.secondary.addr: m for m in self.matches}

    @classmethod
    def build(cls, primary_bv, secondary_bv, mapping: Mapping) -> DiffResult:
        return cls(
            primary_bv=primary_bv,
            secondary_bv=secondary_bv,
            similarity=float(mapping.normalized_similarity),
            matches=[
                MatchRecord(
                    primary=FunctionRef(m.primary.addr, m.primary.name),
                    secondary=FunctionRef(m.secondary.addr, m.secondary.name),
                    similarity=float(m.similarity),
                    confidence=float(m.confidence),
                )
                for m in mapping
            ],
            primary_unmatched=cls._refs(mapping.primary_unmatched),
            secondary_unmatched=cls._refs(mapping.secondary_unmatched),
        )

    @staticmethod
    def _refs(functions: Iterable[QBFunction]) -> list[FunctionRef]:
        return [FunctionRef(f.addr, f.name) for f in sorted(functions, key=lambda f: f.addr)]

    @property
    def duration(self) -> float:
        return sum(seconds for _label, seconds in self.timings)

    @property
    def timing_report(self) -> str:
        """Every phase, one per line, longest-running first is *not* wanted —
        the order they ran in is what makes the total legible."""

        return "\n".join(f"{label}: {format_duration(seconds)}" for label, seconds in self.timings)

    @property
    def nb_match(self) -> int:
        return len(self.matches)

    @property
    def nb_unmatched_primary(self) -> int:
        return len(self.primary_unmatched)

    @property
    def nb_unmatched_secondary(self) -> int:
        return len(self.secondary_unmatched)


#: Binary Ninja database extension. Loading one restores the saved analysis,
#: including renamed functions, types and comments, which is usually what you
#: want to diff against rather than a fresh auto-analysis of the raw file.
DATABASE_SUFFIX = ".bndb"


def is_database(path: str) -> bool:
    return path.lower().endswith(DATABASE_SUFFIX)


def load_secondary(
    path: str,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> BinaryView | None:
    """Load the second side of the diff, from a raw binary or a ``.bndb``.

    Returns ``None`` if cancelled. Deliberately not using ``with load(...)`` —
    that closes the view on exit, and the diff view holds onto it. The caller
    owns ``bv.file.close()``.
    """

    report = progress or (lambda text: None)
    is_cancelled = cancelled or (lambda: False)
    name = Path(path).name

    if not Path(path).exists():
        raise RuntimeError(f"No such file: {path}")

    database = is_database(path)
    report(f"Opening database {name}" if database else f"Loading {name}")

    def on_progress(current: int, total: int) -> bool:
        # Only fires for databases. Returning False aborts the load, which is
        # the one chance to cancel before analysis starts.
        if total:
            report(f"Opening database {name} ({current * 100 // total}%)")
        return not is_cancelled()

    try:
        bv = binaryninja.load(path, update_analysis=False, progress_func=on_progress)
    except Exception:
        # Returning False from the progress callback aborts the load, and
        # binaryninja.load() surfaces that as a generic failure. Cancelling is
        # not an error, so do not let it reach the user as one.
        if is_cancelled():
            return None
        raise RuntimeError(
            f"Binary Ninja could not open {path}"
            + (
                " (the database may be from an incompatible version)"
                if database
                else " (unrecognized file format)"
            )
        ) from None

    if bv is None:
        if is_cancelled():
            return None
        raise RuntimeError(f"Binary Ninja could not open {path}")

    if is_cancelled():
        return bv

    from .scope import available_regions

    regions = available_regions(bv)
    if regions and not any(region.loaded for region in regions):
        # An empty container: analyzing it now costs functions later. Binary
        # Ninja's initial sweep runs once, over a view that has no code in it
        # yet, and the segments mapped afterwards get only what recursive
        # descent reaches from an entry point. Measured on a 26-module SEP
        # image: 26896 functions analyzing first, 31499 mapping first — the
        # missing 15% then silently absent from the diff. Whoever loads the
        # parts (scope, mirror_loaded) leaves the analysis to wait_for_analysis.
        return bv

    # A database usually arrives fully analyzed, but it may have been saved
    # mid-analysis, so finish the job either way.
    report(f"Analyzing {name}")
    bv.update_analysis_and_wait()
    return bv


#: How often to re-read a view's analysis progress while waiting on it.
ANALYSIS_POLL_SECONDS = 0.2


def wait_for_analysis(
    bv: BinaryView,
    progress: Callable[[str, float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Block until ``bv``'s auto-analysis has settled. ``False`` if cancelled.

    The secondary is analyzed as part of loading it, but the primary is the
    live view the user opened: the diff view can be on screen, and a diff
    started from it, while Binary Ninja is still disassembling. QBinDiff would
    then build its program graph from whatever existed at that instant — fewer
    functions, half-populated basic blocks — and produce a diff that looks
    plausible, is wrong, and differs from run to run.

    Polling first is what makes the wait cancellable and gives it a progress
    fraction; ``update_analysis_and_wait`` cannot report either. The final
    call is still needed, because idle means only that nothing is running
    right now, not that everything pending has been done.
    """

    report = progress or (lambda label, fraction: None)
    is_cancelled = cancelled or (lambda: False)
    name = Path(bv.file.filename).name
    label = f"Waiting for analysis of {name}"

    state = bv.analysis_progress
    while state.state != AnalysisState.IdleState:
        if is_cancelled():
            return False
        report(label, min(state.count / state.total, 1.0) if state.total else 0.0)
        time.sleep(ANALYSIS_POLL_SECONDS)
        state = bv.analysis_progress

    if is_cancelled():
        return False
    report(f"Analyzing {name}", 0.0)
    bv.update_analysis_and_wait()
    return True


def build_program(bv: BinaryView, region=None):
    """Wrap a BinaryView in a QBinDiff ``Program`` via the native backend.

    ``region`` restricts the program to one kext or SEP module; see
    ``core/scope.py``.
    """

    from qbindiff.loader import Program

    from .backend import ProgramBackendBinja
    from .scope import functions_in

    return Program.from_backend(ProgramBackendBinja(bv, functions_in(bv, region)))


def feature_extractors(extra: Iterable[str] = ()) -> list:
    """The feature extractor classes a diff registers, in registration order.

    QBinDiff's defaults minus EXCLUDED_DEFAULT_FEATURES, then
    DEFAULT_EXTRA_FEATURES, then ``extra``. Unknown keys are logged and skipped.
    """

    from qbindiff.features import DEFAULT_FEATURES, FEATURES

    from .backend import ImportCalls

    extractors = {f.key: f for f in FEATURES}
    extractors[ImportCalls.key] = ImportCalls
    selected = [f for f in DEFAULT_FEATURES if f.key not in EXCLUDED_DEFAULT_FEATURES]
    for key in (*DEFAULT_EXTRA_FEATURES, *extra):
        extractor = extractors.get(key)
        if extractor is None:
            log_warn(f"Unknown feature '{key}' ignored", "QBinDiff")
        elif extractor not in selected:
            selected.append(extractor)
    return selected


def run_diff(
    primary_bv: BinaryView,
    secondary_bv: BinaryView,
    options: DiffOptions | None = None,
    progress: Callable[[str, float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    region_name: str | None = None,
) -> DiffResult | None:
    """Run a full diff. Returns ``None`` if cancelled.

    ``progress`` receives a label and a 0..1 fraction. ``cancelled`` is polled
    between iterations; both generators below are ordinary Python generators,
    so abandoning them is safe.
    """

    import numpy

    from qbindiff import Distance, QBinDiff

    options = options or DiffOptions()
    is_cancelled = cancelled or (lambda: False)
    report = progress or (lambda label, frac: None)
    timings: list[tuple[str, float]] = []

    with _log_bridge():
        # Scoping happens before anything else: loading a kext changes the
        # function list, and waiting for analysis on a view that has not been
        # given its code yet would wait for nothing.
        primary_region = secondary_region = None
        if region_name is not None:
            from .scope import ensure_loaded, find_region

            for label, view in (("primary", primary_bv), ("secondary", secondary_bv)):
                region = find_region(view, region_name)
                if region is None:
                    from .scope import missing_region_hint

                    raise RuntimeError(
                        f"The {label} binary has no part named {region_name!r}."
                        f"{missing_region_hint(view)}"
                    )
                report(f"Loading {region_name}", INDETERMINATE)
                if not ensure_loaded(view, region):
                    raise RuntimeError(f"Could not load {region_name!r} from the {label} binary.")
                if label == "primary":
                    primary_region = find_region(view, region_name)
                else:
                    secondary_region = find_region(view, region_name)
            log_info(f"Diffing only {region_name}", "QBinDiff")

        else:
            # A container holds no code until something is mapped into it, so
            # an unscoped diff has to mirror the primary's parts across first.
            from .scope import mirror_loaded

            mirrored = mirror_loaded(
                primary_bv,
                secondary_bv,
                progress=lambda name: report(f"Loading {name}", INDETERMINATE),
            )
            if mirrored:
                log_info(f"Diffing {len(mirrored)} part(s): {', '.join(mirrored)}", "QBinDiff")

        # Before the function counts below mean anything: a view still being
        # analyzed can legitimately have none yet.
        with _timed(timings, "Waiting for analysis"):
            for view in (primary_bv, secondary_bv):
                if not wait_for_analysis(view, progress=report, cancelled=is_cancelled):
                    return None

        # A file Binary Ninja does not recognize still opens, as a raw view
        # with no functions. Diffing that yields an empty result that looks
        # like a plugin bug, so say what actually happened.
        for label, view in (("primary", primary_bv), ("secondary", secondary_bv)):
            if len(view.functions) == 0:
                raise RuntimeError(
                    f"The {label} binary ({view.file.filename}) contains no functions. "
                    f"Binary Ninja may not recognize its format, or analysis may not "
                    f"have run."
                )

        options = scale_options_for_size(
            options, len(primary_bv.functions), len(secondary_bv.functions)
        )

        report("Building program graphs", 0.0)
        with _timed(timings, "Building the primary graph"):
            primary = build_program(primary_bv, primary_region)
        if is_cancelled():
            return None
        with _timed(timings, "Building the secondary graph"):
            secondary = build_program(secondary_bv, secondary_region)
        if is_cancelled():
            return None

        log_info(
            f"Diffing {len(primary)} vs {len(secondary)} functions",
            "QBinDiff",
        )

        differ = QBinDiff(
            primary,
            secondary,
            distance=Distance[options.distance],
            normalize=options.normalize,
            sparsity_ratio=options.sparsity_ratio,
            tradeoff=options.tradeoff,
            epsilon=options.epsilon,
            maxiter=options.maxiter,
            sparse_row=options.sparse_row,
        )
        exact_pairs: set[tuple[int, int]] = set()
        differ.register_postpass(
            partial(
                match_named_functions,
                primary_bv=primary_bv,
                secondary_bv=secondary_bv,
                exact_pairs=exact_pairs,
            )
        )

        selected = feature_extractors(options.features)
        for extractor in selected:
            differ.register_feature_extractor(extractor, 1.0)

        # Phase 1 yields absolute values in [0, 1000], possibly more than 1000
        # times; phase 2 yields the iteration number and may converge early.
        # Both stop reporting well before they stop working — see the labels
        # below, which are set *before* each silent stretch begins.
        phase = ""
        with _timed(timings, "Extracting features"):
            for step in differ.process_iterator():
                if is_cancelled():
                    return None
                label, value = feature_phase(min(step / 1000.0, 1.0))
                if label != phase:
                    phase = label
                    if value is INDETERMINATE:
                        log_info(
                            f"Feature extraction done; building the similarity matrix for "
                            f"{len(selected)} features over {len(primary)} x "
                            f"{len(secondary)} functions. Nothing reports progress until "
                            f"that finishes, and it is usually the longest part of the run.",
                            "QBinDiff",
                        )
                report(label, value)

        # matching_iterator sparsifies the similarity matrix and computes the
        # squares matrix before its first yield; on a large pair that argsort
        # alone runs for minutes.
        report("Preparing the matcher", INDETERMINATE)
        iterations = 0
        # Belief propagation raises e to the marginals, which overflows to +inf
        # by design — qbindiff clips the result to 1e6 on the next line, since
        # any probability past 99.9999% is the same answer, and the clipped
        # value is identical either way. numpy still reports every one as a
        # RuntimeWarning, once per iteration, into a log being read for real
        # problems. errstate is thread-local, so this silences overflow for the
        # diff thread only and nothing else in Binary Ninja is affected.
        with _timed(timings, "Matching functions"), numpy.errstate(over="ignore"):
            for iteration in differ.matching_iterator():
                if is_cancelled():
                    return None
                iterations = iteration
                report("Matching functions", min(iteration / max(differ.maxiter, 1), 1.0))
        # Belief propagation usually converges long before maxiter, so the
        # iteration count is what makes its duration interpretable.
        log_info(
            f"Belief propagation converged after {iterations} of at most "
            f"{differ.maxiter} iterations",
            "QBinDiff",
        )

        report("Scoring matches", 1.0)
        with _timed(timings, "Scoring matches"):
            mapping = differ.mapping
        if mapping is None:
            return None
        result = DiffResult.build(primary_bv, secondary_bv, mapping)
        demote_unsubstantiated_matches(result, exact_pairs)
        result.timings = timings
        return result


class SecondaryTask(BackgroundTaskThread):
    """Shared plumbing for tasks that bring up a secondary binary.

    Both producing a diff and restoring a saved one open the second side and
    report the same way, and both must release that view again on every path
    that never hands it to the UI. Subclasses implement ``run``.
    """

    def __init__(
        self,
        title: str,
        primary_bv: BinaryView,
        secondary: BinaryView | str,
        on_done: Callable[[DiffResult], None],
        on_error: Callable[[str], None],
        on_progress: Callable[[str, float], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
    ):
        super().__init__(title, can_cancel=True)
        self.primary_bv = primary_bv
        self._secondary = secondary
        self._on_done = on_done
        self._on_error = on_error
        self._on_progress = on_progress
        self._on_cancelled = on_cancelled
        #: Set when this task loaded the secondary itself and therefore owns it.
        self.owns_secondary = isinstance(secondary, str)
        #: Timed here rather than in run_diff, which never sees the load.
        self.load_timings: list[tuple[str, float]] = []

    def _report(self, label: str, fraction: float) -> None:
        if fraction < 0:
            self.progress = f"Binary diff: {label}"
        else:
            self.progress = f"Binary diff: {label} ({fraction * 100:.0f}%)"
        if self._on_progress is not None:
            self._on_progress(label, fraction)

    def _report_text(self, label: str) -> None:
        """Progress for phases with no meaningful fraction, such as loading."""

        self.progress = f"Binary diff: {label}"
        if self._on_progress is not None:
            self._on_progress(label, 0.0)

    def _open_secondary(self) -> BinaryView | None:
        if isinstance(self._secondary, str):
            with _timed(self.load_timings, "Loading and analyzing the secondary binary"):
                return load_secondary(
                    self._secondary,
                    progress=self._report_text,
                    cancelled=lambda: self.cancelled,
                )
        return self._secondary

    def _cancel(self, secondary_bv: BinaryView | None) -> None:
        log_info("Diff cancelled", "QBinDiff")
        self._discard(secondary_bv)
        if self._on_cancelled is not None:
            self._on_cancelled()

    def _fail(self, secondary_bv: BinaryView | None, exc: Exception) -> None:
        log_error(traceback.format_exc(), "QBinDiff")
        self._discard(secondary_bv)
        self._on_error(str(exc))

    def _discard(self, secondary_bv: BinaryView | None) -> None:
        """Close a secondary we loaded but will never hand to the UI."""

        if secondary_bv is None or not self.owns_secondary:
            return
        try:
            secondary_bv.file.close()
        except Exception:
            log_warn("Failed to close the secondary binary", "QBinDiff")


class DiffTask(SecondaryTask):
    """Background task that optionally loads a secondary binary, then diffs."""

    def __init__(
        self,
        primary_bv: BinaryView,
        secondary: BinaryView | str,
        on_done: Callable[[DiffResult], None],
        on_error: Callable[[str], None],
        options: DiffOptions | None = None,
        on_progress: Callable[[str, float], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        region_name: str | None = None,
    ):
        super().__init__(
            "Binary diff: starting",
            primary_bv,
            secondary,
            on_done,
            on_error,
            on_progress=on_progress,
            on_cancelled=on_cancelled,
        )
        self._options = options or DiffOptions()
        #: Diff only this kext / SEP module, by name. See core/scope.py.
        self._region_name = region_name

    def run(self) -> None:
        secondary_bv: BinaryView | None = None
        try:
            secondary_bv = self._open_secondary()

            result = None
            if secondary_bv is not None and not self.cancelled:
                result = run_diff(
                    self.primary_bv,
                    secondary_bv,
                    options=self._options,
                    progress=self._report,
                    cancelled=lambda: self.cancelled,
                    region_name=self._region_name,
                )

            if result is None:
                self._cancel(secondary_bv)
                return

            result.timings[:0] = self.load_timings
            log_info(
                f"Diff complete in {format_duration(result.duration)}: "
                f"{result.nb_match} matches, similarity {result.similarity:.3f}",
                "QBinDiff",
            )
            self._on_done(result)
        except Exception as exc:
            self._fail(secondary_bv, exc)
