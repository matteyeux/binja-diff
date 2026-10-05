# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Saving diff results, and restoring them without re-running QBinDiff.

A diff of two sizeable binaries costs minutes; what it produces is a list of
function pairs. Everything the view draws on top of that — block and line
alignment, per-line status, the rendered text — is recomputed from the two
BinaryViews on demand, so persisting the matching alone brings a whole session
back after a restart. The second binary still has to be re-opened and analyzed,
which is why restoring is a background task rather than a function call.

One format, two sinks: the same JSON goes either into the primary's analysis
database as a metadata string, or into a standalone file. The file is the
portable one — it survives a database rebuild and can be shared or diffed by
other tools — while the database copy is the one that is simply *there* the
next time the binary is opened.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

from binaryninja import BinaryView, log_info, log_warn

from .engine import (
    DiffOptions,
    DiffResult,
    FunctionRef,
    MatchRecord,
    SecondaryTask,
    format_duration,
    wait_for_analysis,
)
from .scope import ensure_named_loaded

#: Identifies the payload wherever it is found. A metadata key can be read by
#: any plugin, and a .json file can be handed to anything.
FORMAT = "binja-diff"

#: Bumped only for changes older readers cannot cope with. ``from_dict``
#: tolerates missing optional keys, so purely additive changes keep the version.
VERSION = 1

#: Metadata key on the primary BinaryView. Namespaced because the metadata
#: store is shared with every other plugin.
METADATA_KEY = "binja-diff.result"

#: Doubled suffix so the file is obviously a diff and obviously JSON.
FILE_SUFFIX = ".bndiff.json"


def _basename(path: str) -> str:
    return os.path.basename(path) or path


@dataclass(frozen=True)
class ViewInfo:
    """Enough of a BinaryView to notice it is not the one the diff ran on."""

    filename: str
    view_type: str
    length: int
    functions: int

    @classmethod
    def of(cls, bv: BinaryView) -> ViewInfo:
        return cls(
            filename=bv.file.filename,
            view_type=bv.view_type,
            # BinaryView exposes `length`; it has no __len__, unlike most of the
            # other sized objects in the API.
            length=bv.length,
            functions=len(bv.functions),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ViewInfo:
        return cls(
            filename=str(data.get("filename", "")),
            view_type=str(data.get("view_type", "")),
            length=int(data.get("length", 0)),
            functions=int(data.get("functions", 0)),
        )

    def differences(self, bv: BinaryView) -> list[str]:
        """Human-readable reasons to distrust a restore against ``bv``.

        A saved diff carries addresses and nothing else, so a binary that has
        been rebuilt underneath it pairs the wrong functions and reports no
        error at all. None of these signals is fatal on its own — a moved file
        is fine, and a re-analysis that found a few more functions usually is
        too — so they are surfaced to the user, not enforced here.
        """

        current = ViewInfo.of(bv)
        notes = []
        if _basename(self.filename) != _basename(current.filename):
            notes.append(f"file name: {_basename(self.filename)} -> {_basename(current.filename)}")
        if self.length != current.length:
            notes.append(f"size: {self.length} -> {current.length} bytes")
        if self.functions != current.functions:
            notes.append(f"functions: {self.functions} -> {current.functions}")
        return notes


@dataclass
class SavedDiff:
    """A diff result in serializable form."""

    primary: ViewInfo
    secondary: ViewInfo
    similarity: float
    matches: list[MatchRecord] = field(default_factory=list)
    primary_unmatched: list[FunctionRef] = field(default_factory=list)
    secondary_unmatched: list[FunctionRef] = field(default_factory=list)
    created: str = ""
    #: The DiffOptions the run used, for provenance. Never fed back into a
    #: restore — nothing is recomputed — but it answers "what produced this?".
    options: dict = field(default_factory=dict)
    #: Container parts the diff covered. Unlike ``options`` this *is* fed back
    #: into a restore: the parts have to be mapped in again before the saved
    #: addresses point at any function. Empty for an ordinary binary.
    scope: list[str] = field(default_factory=list)
    version: int = VERSION

    # -- conversion --------------------------------------------------------

    @classmethod
    def from_result(cls, result: DiffResult, options: DiffOptions | None = None) -> SavedDiff:
        return cls(
            primary=ViewInfo.of(result.primary_bv),
            secondary=ViewInfo.of(result.secondary_bv),
            similarity=result.similarity,
            matches=list(result.matches),
            primary_unmatched=list(result.primary_unmatched),
            secondary_unmatched=list(result.secondary_unmatched),
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            options=asdict(options) if options is not None else {},
            scope=list(result.scope),
        )

    def to_result(self, primary_bv: BinaryView, secondary_bv: BinaryView) -> DiffResult:
        return DiffResult(
            primary_bv=primary_bv,
            secondary_bv=secondary_bv,
            similarity=self.similarity,
            matches=list(self.matches),
            primary_unmatched=list(self.primary_unmatched),
            secondary_unmatched=list(self.secondary_unmatched),
            scope=list(self.scope),
        )

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "version": self.version,
            "created": self.created,
            "options": self.options,
            "scope": list(self.scope),
            "similarity": round(self.similarity, 6),
            "primary": self.primary.to_dict(),
            "secondary": self.secondary.to_dict(),
            # Positional rows rather than one object per match: a 50k-function
            # diff shrinks several-fold, and this payload also has to fit in a
            # database metadata string.
            "matches": [
                [
                    m.primary.addr,
                    m.primary.name,
                    m.secondary.addr,
                    m.secondary.name,
                    round(m.similarity, 6),
                    round(m.confidence, 6),
                ]
                for m in self.matches
            ],
            "primary_unmatched": [[f.addr, f.name] for f in self.primary_unmatched],
            "secondary_unmatched": [[f.addr, f.name] for f in self.secondary_unmatched],
        }

    @classmethod
    def from_dict(cls, data: dict) -> SavedDiff:
        """Rebuild from parsed JSON. Raises ``ValueError`` on anything else."""

        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("Not a binja-diff result file")
        version = data.get("version", 0)
        if not isinstance(version, int) or version > VERSION:
            raise ValueError(
                f"Saved diff is version {version}, but this plugin only reads up to {VERSION}"
            )
        try:
            return cls(
                primary=ViewInfo.from_dict(data.get("primary", {})),
                secondary=ViewInfo.from_dict(data.get("secondary", {})),
                similarity=float(data.get("similarity", 0.0)),
                matches=[
                    MatchRecord(
                        primary=FunctionRef(int(row[0]), str(row[1])),
                        secondary=FunctionRef(int(row[2]), str(row[3])),
                        similarity=float(row[4]),
                        confidence=float(row[5]),
                    )
                    for row in data.get("matches", [])
                ],
                primary_unmatched=_refs(data.get("primary_unmatched", [])),
                secondary_unmatched=_refs(data.get("secondary_unmatched", [])),
                created=str(data.get("created", "")),
                options=data.get("options") or {},
                scope=[str(name) for name in data.get("scope") or []],
                version=version,
            )
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise ValueError(f"Saved diff is malformed: {exc}") from None

    def to_json(self) -> str:
        # Compact: a large diff is megabytes, and both sinks store it verbatim.
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> SavedDiff:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Saved diff is not valid JSON: {exc}") from None
        return cls.from_dict(data)

    @property
    def summary(self) -> str:
        """One line for a status bar or a confirmation dialog."""

        when = f" from {self.created}" if self.created else ""
        return (
            f"{len(self.matches)} matches against {_basename(self.secondary.filename)}"
            f", similarity {self.similarity:.3f}{when}"
        )


def _refs(rows) -> list[FunctionRef]:
    return [FunctionRef(int(row[0]), str(row[1])) for row in rows]


# -- file sink -------------------------------------------------------------


def default_filename(result: DiffResult) -> str:
    """Suggested name for an export: ``primary-vs-secondary.bndiff.json``."""

    primary = _basename(result.primary_bv.file.filename) or "primary"
    secondary = _basename(result.secondary_bv.file.filename) or "secondary"
    return f"{primary}-vs-{secondary}{FILE_SUFFIX}"


def write_file(saved: SavedDiff, path: str) -> None:
    try:
        Path(path).write_text(saved.to_json(), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not write {path}: {exc}") from None


def read_file(path: str) -> SavedDiff:
    """Load a saved diff from disk. Raises ``ValueError`` or ``RuntimeError``."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from None
    return SavedDiff.from_json(text)


# -- database sink ---------------------------------------------------------


def store_in_database(bv: BinaryView, saved: SavedDiff) -> None:
    """Attach the diff to the primary's analysis database.

    Metadata only reaches disk when the database itself is saved, and a view
    that was never saved as a ``.bndb`` has no database to write to at all —
    hence ``is_persistent`` for callers that need to say so.
    """

    bv.store_metadata(METADATA_KEY, saved.to_json())


def is_persistent(bv: BinaryView) -> bool:
    """Whether storing metadata on ``bv`` can outlive the session.

    Only ever asked after a successful save, to word the follow-up advice, so a
    view that cannot answer is reported as non-persistent rather than turning
    that save into an error.
    """

    try:
        return bool(bv.file.has_database)
    except Exception:
        return False


def has_saved_diff(bv: BinaryView) -> bool:
    """Whether ``bv`` carries a saved diff, without parsing megabytes of it."""

    try:
        return bool(bv.query_metadata(METADATA_KEY))
    except KeyError:
        return False


def load_from_database(bv: BinaryView) -> SavedDiff | None:
    """The diff stored on ``bv``, or ``None`` if there is none or it is junk."""

    try:
        raw = bv.query_metadata(METADATA_KEY)
    except KeyError:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        log_warn(f"Ignoring unreadable saved diff in {METADATA_KEY}", "QBinDiff")
        return None
    try:
        return SavedDiff.from_json(raw)
    except ValueError as exc:
        log_warn(f"Ignoring saved diff in the database: {exc}", "QBinDiff")
        return None


def remove_from_database(bv: BinaryView) -> None:
    """Drop the stored diff. Removing a key that is not there is not an error."""

    bv.remove_metadata(METADATA_KEY)


# -- restoring -------------------------------------------------------------


class RestoreTask(SecondaryTask):
    """Re-open the secondary binary and rebuild a saved diff against it.

    Same callback contract as ``DiffTask``, so the view can treat a restore and
    a fresh diff identically; only the loading half of the work is left.
    """

    def __init__(
        self,
        primary_bv: BinaryView,
        saved: SavedDiff,
        secondary: BinaryView | str,
        on_done: Callable[[DiffResult], None],
        on_error: Callable[[str], None],
        on_progress: Callable[[str, float], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
    ):
        super().__init__(
            "Binary diff: restoring saved diff",
            primary_bv,
            secondary,
            on_done,
            on_error,
            on_progress=on_progress,
            on_cancelled=on_cancelled,
        )
        self._saved = saved

    def run(self) -> None:
        secondary_bv: BinaryView | None = None
        try:
            secondary_bv = self._open_secondary()
            if secondary_bv is None or self.cancelled:
                self._cancel(secondary_bv)
                return

            if not self._restore_scope(secondary_bv):
                self._cancel(secondary_bv)
                return

            # The primary was checked before the task started; the secondary
            # could only be inspected once it was open. Neither is fatal, so
            # this is a warning rather than a refusal to restore.
            drift = self._saved.secondary.differences(secondary_bv)
            if drift:
                log_warn(
                    "Restored diff: the secondary binary has changed since it was saved "
                    f"({'; '.join(drift)}). Matches may point at the wrong functions.",
                    "QBinDiff",
                )

            result = self._saved.to_result(self.primary_bv, secondary_bv)
            # A restore's only cost is the load; the phases of the original run
            # belong to that run and are not replayed here.
            result.timings = list(self.load_timings)
            log_info(
                f"Restored diff in {format_duration(result.duration)}: "
                f"{result.nb_match} matches, similarity {result.similarity:.3f}",
                "QBinDiff",
            )
            self._on_done(result)
        except Exception as exc:
            self._fail(secondary_bv, exc)

    def _restore_scope(self, secondary_bv: BinaryView) -> bool:
        """Map the saved parts back into both views. ``False`` if cancelled.

        A saved diff of one kext or SEP module names that part; the view the
        restore just opened holds none of it, and the primary may have had it
        unloaded since. Without this every address in the result resolved to
        nothing and the whole table read as missing. Both sides are mapped
        before either is analyzed, for the reason ``ensure_all_loaded`` gives.
        """

        names = self._saved.scope
        if not names:
            return True
        self._report_text(f"Loading {', '.join(names)}")
        for label, view in (("primary", self.primary_bv), ("secondary", secondary_bv)):
            missing = ensure_named_loaded(view, names)
            if missing:
                raise RuntimeError(
                    f"The {label} binary has no part named {', '.join(missing)}; the saved "
                    f"diff covered it, so it cannot be restored against this file."
                )
        for view in (self.primary_bv, secondary_bv):
            if not wait_for_analysis(view, progress=self._report, cancelled=lambda: self.cancelled):
                return False
        return True
