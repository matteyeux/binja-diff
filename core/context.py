# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""What else a changed function touches: its callees and the strings it uses.

The backend reads both for QBinDiff and throws them away once the features are
extracted. Read again here, for one pair at a time, they answer the question a
changed verdict raises next — did this function start calling something new,
stop calling something, or pick up a different string — without opening the
two panes. Callees are compared *through the diff*: a callee counts as shared
when the result pairs it with a callee of the other side, so a helper that
moved still counts as the same helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from binaryninja import BinaryView, Function

from .engine import DiffResult


@dataclass(frozen=True)
class ContextDelta:
    """How two functions' surroundings differ."""

    #: Callees the two sides share, as (primary name, secondary name).
    shared_callees: list[tuple[str, str]] = field(default_factory=list)
    primary_only_callees: list[str] = field(default_factory=list)
    secondary_only_callees: list[str] = field(default_factory=list)
    shared_strings: int = 0
    primary_only_strings: list[str] = field(default_factory=list)
    secondary_only_strings: list[str] = field(default_factory=list)

    @property
    def differs(self) -> bool:
        return bool(
            self.primary_only_callees
            or self.secondary_only_callees
            or self.primary_only_strings
            or self.secondary_only_strings
        )

    def summary(self, limit: int = 4) -> str:
        """A few lines for a tooltip or a report; empty when nothing differs."""

        lines = []
        calls = []
        if self.primary_only_callees:
            calls.append(f"only primary calls {_some(self.primary_only_callees, limit)}")
        if self.secondary_only_callees:
            calls.append(f"only secondary calls {_some(self.secondary_only_callees, limit)}")
        if calls:
            lines.append(f"callees: {len(self.shared_callees)} shared; " + "; ".join(calls))
        strings = []
        if self.primary_only_strings:
            strings.append(f"only primary uses {_some(self.primary_only_strings, limit)}")
        if self.secondary_only_strings:
            strings.append(f"only secondary uses {_some(self.secondary_only_strings, limit)}")
        if strings:
            lines.append(f"strings: {self.shared_strings} shared; " + "; ".join(strings))
        return "\n".join(lines)


def _some(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} and {len(items) - limit} more"


def _name(bv: BinaryView, addr: int, fallback: str | None = None) -> str:
    func = bv.get_function_at(addr)
    if func is not None:
        return func.name
    return fallback if fallback is not None else f"{addr:#x}"


def string_refs(bv: BinaryView, func: Function) -> set[str]:
    """Every string a function's instructions reference, by value.

    Walked per instruction the way the backend does; the cost is one data-ref
    lookup per instruction, which is why this runs for the pair asked about and
    not for the whole table.
    """

    found: set[str] = set()
    for block in func.basic_blocks:
        addr = block.start
        for _tokens, length in block:
            for target in bv.get_data_refs_from(addr):
                string = bv.get_string_at(target)
                if string is not None:
                    found.add(str(string.value))
            addr += length
    return found


def context_delta(result: DiffResult, left: Function, right: Function) -> ContextDelta:
    """Compare what two matched functions call and which strings they use."""

    primary_bv, secondary_bv = result.primary_bv, result.secondary_bv
    left_callees = set(left.callee_addresses)
    right_callees = set(right.callee_addresses)

    shared: list[tuple[str, str]] = []
    primary_only: list[str] = []
    covered: set[int] = set()
    for addr in sorted(left_callees):
        match = result.by_primary.get(addr)
        if match is not None and match.secondary.addr in right_callees:
            covered.add(match.secondary.addr)
            shared.append(
                (
                    _name(primary_bv, addr, match.primary.name),
                    _name(secondary_bv, match.secondary.addr, match.secondary.name),
                )
            )
        elif match is None and addr in right_callees and addr not in result.by_secondary:
            # A target the diff never saw — a synthetic builtin, a GOT slot —
            # at the same address on both sides is the same target.
            covered.add(addr)
            shared.append((_name(primary_bv, addr), _name(secondary_bv, addr)))
        else:
            primary_only.append(_name(primary_bv, addr))
    secondary_only = [_name(secondary_bv, addr) for addr in sorted(right_callees - covered)]

    left_strings = string_refs(primary_bv, left)
    right_strings = string_refs(secondary_bv, right)
    return ContextDelta(
        shared_callees=shared,
        primary_only_callees=primary_only,
        secondary_only_callees=secondary_only,
        shared_strings=len(left_strings & right_strings),
        primary_only_strings=sorted(left_strings - right_strings),
        secondary_only_strings=sorted(right_strings - left_strings),
    )
