# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Intra-function alignment: basic blocks and per-line text diffs.

QBinDiff's belief-propagation engine only matches at function level, so
everything below the function is computed here, lazily, for the one function
pair the user has selected.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, NamedTuple
from collections.abc import Iterable, Sequence

import networkx

from binaryninja import BinaryView, Function as BNFunction
from binaryninja.enums import FunctionGraphType, InstructionTextTokenType

if TYPE_CHECKING:
    from binaryninja.lineardisassembly import LinearDisassemblyLine


class LineStatus(str, Enum):
    """Per-line diff classification, mirrored by the UI's highlight colors."""

    EQUAL = "equal"
    #: Same operation, but an operand's spelling differs: an immediate, an
    #: address, a stack offset, or which register was chosen. Kept distinct from
    #: CHANGED because rebasing a binary or adding one local perturbs these
    #: everywhere without changing what the code does.
    MINOR = "minor"
    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"
    #: Padding inserted on one side so both panes stay row-aligned.
    GAP = "gap"
    #: A line with no code on it — a user comment, a blank line. Shown, never
    #: compared: annotating a function is not changing it.
    COMMENT = "comment"

    @property
    def marker(self) -> str:
        """Single-character gutter marker.

        Colors alone are easy to miss on a dense screen, and they do not
        survive copy and paste.
        """

        return {
            LineStatus.EQUAL: " ",
            LineStatus.MINOR: "~",
            LineStatus.CHANGED: "!",
            LineStatus.ADDED: "+",
            LineStatus.REMOVED: "-",
            LineStatus.GAP: " ",
            LineStatus.COMMENT: " ",
        }[self]

    @property
    def is_difference(self) -> bool:
        return self not in (LineStatus.EQUAL, LineStatus.GAP, LineStatus.COMMENT)


class BlockStatus(str, Enum):
    IDENTICAL = "identical"
    CHANGED = "changed"
    UNMATCHED = "unmatched"


#: IL levels the UI can render, mapped to their graph type and the
#: LinearViewObject factory that produces whole-function text.
IL_LEVELS: dict[str, tuple[FunctionGraphType, str]] = {
    "Disassembly": (FunctionGraphType.NormalFunctionGraph, "single_function_disassembly"),
    "LLIL": (FunctionGraphType.LowLevelILFunctionGraph, "single_function_llil"),
    "MLIL": (FunctionGraphType.MediumLevelILFunctionGraph, "single_function_mlil"),
    "HLIL": (FunctionGraphType.HighLevelILFunctionGraph, "single_function_hlil"),
}

#: Language representations the UI offers, in display order. Each is a rendering
#: of HLIL rather than a level of its own; only those registered with the core
#: are shown (see `available_levels`), since they ship as separate plugins.
LANGUAGES = ("Pseudo C", "Pseudo Objective-C", "Pseudo Rust")


class RenderLevel(NamedTuple):
    """A way of rendering a function, in the two forms it takes.

    ``level`` is a key of `IL_LEVELS`, used to align blocks and grade lines.
    ``language`` is set only for a language representation — Pseudo C and
    friends — which is a *rendering* of HLIL rather than a level of its own:
    the graph and the linear view are built from the language, while the
    comparison behind them runs on HLIL, whose blocks they share.
    """

    level: str
    language: str | None = None

    @property
    def name(self) -> str:
        return self.language or self.level

    @property
    def graph_type(self):
        """What to hand `create_graph`, which takes either form."""

        return self.language or IL_LEVELS[self.level][0]

    def linear_object(self, func: BNFunction, settings=None):
        """The single-function `LinearViewObject` for this rendering."""

        from binaryninja import LinearViewObject

        if self.language:
            return LinearViewObject.single_function_language_representation(
                func, settings, self.language
            )
        _, factory_name = IL_LEVELS[self.level]
        return getattr(LinearViewObject, factory_name)(func, settings)


def render_level(name: str | RenderLevel) -> RenderLevel:
    """Resolve a display name — an IL level or a language — to a `RenderLevel`."""

    if isinstance(name, RenderLevel):
        return name
    if name in IL_LEVELS:
        return RenderLevel(name)
    if name in LANGUAGES:
        return RenderLevel("HLIL", name)
    raise ValueError(f"Unknown IL level {name!r}")


def available_levels() -> list[RenderLevel]:
    """Every rendering the UI can offer: the IL levels, then registered languages.

    A language whose plugin is not loaded would render nothing, so it is left
    out rather than offered as a choice that fails.
    """

    levels = [RenderLevel(level) for level in IL_LEVELS]
    try:
        from binaryninja.languagerepresentation import LanguageRepresentationFunctionType

        registered = {language.name for language in LanguageRepresentationFunctionType}
    except Exception:
        return levels
    return levels + [RenderLevel("HLIL", name) for name in LANGUAGES if name in registered]


#: Hex literals and addresses shift wholesale between builds, so comparing them
#: verbatim reports almost every line as changed.
_HEX = re.compile(r"0x[0-9a-fA-F]+")

#: Auto-generated names for a *location*, with the address they encode.
#: `sub_10001a9c8` is 0x10001a9c8 spelled as a name, so it is rewritten to the
#: address rather than to a placeholder: which spelling a view uses depends on
#: what it has resolved, and the same database rendered twice does not always
#: agree with itself. Canonicalising means `bl 0x10001a9c8` and
#: `bl sub_10001a9c8` are the same line — they are — while two *different*
#: auto-named targets stay different until the hex fold grades them `~`.
_ADDRESS_NAME = re.compile(r"\b(?:sub|data|jump_table|loc|label)_([0-9a-fA-F]+)\b")

#: A thunk, named after where it jumps rather than where it sits:
#: `j_sub_100007d88` lives at 0x100011ebc. The name therefore cannot be
#: resolved to an address the way `sub_100007d88` can — it would name the wrong
#: byte — so it is only ever folded to "some address" for the `~` tier. Two
#: loads of one file do not always agree on whether a thunk was identified, so
#: one side prints `bl 0x100011ebc` and the other `bl j_sub_100007d88`.
_THUNK_NAME = re.compile(r"\b(?:j_)+(?:sub|data|jump_table|loc|label)_[0-9a-fA-F]+\b")


#: Stack and argument slots keep a placeholder of their own: unlike a location,
#: `var_8` and `var_c` are different storage, and collapsing them into "some
#: address" would hide a genuine change of variable.
_LOCAL_NAME = re.compile(r"\b(var|arg)_[0-9a-fA-F]+\b")

#: An address written as a base plus an offset. Which form a view uses depends
#: on how it modelled the data there — one 16-byte variable renders
#: `data_5598e0+8`, two 8-byte ones render `data_5598e8` — and both name the
#: same byte. Resolved to that byte so the two spellings compare equal.
_ADDRESS_OFFSET = re.compile(r"0x([0-9a-fA-F]+)\+(0x)?([0-9a-fA-F]+)\b")


#: An address written as an index into a variable: `data_10001fce0[0x20]` is
#: 0x10001fd00. Folded to "some address" rather than resolved, because the
#: index is in *elements* and the element size is whatever type that view
#: inferred — which is precisely what the two sides disagree about. Guessing it
#: would risk calling two genuinely different addresses equal, and a false
#: "identical" hides real changes.
_ADDRESS_INDEX = re.compile(r"0x[0-9a-fA-F]+\[[^\]]*\]")


#: Binary Ninja's trailing hints — `{0x0}`, `{sub_10001aa08}`, `{__saved_x22}`.
#: Token-type stripping misses these when the renderer splits the braces and
#: their contents into separate tokens, which it does for symbols.
_TRAILING_HINTS = re.compile(r"(?:\s*\{[^{}]*\})+\s*$")

_WS = re.compile(r"\s+")

#: A comment line with no tokens to identify it. Only a *leading* `//` is
#: trusted: further along a line it may sit inside a string literal.
_COMMENT_LINE = re.compile(r"^\s*//")


def normalize_line(text: str) -> str:
    """Aggressive normalization, used to *align* rows and to grade them MINOR.

    Collapses every hex literal and every auto-generated name, so that two
    builds at different base addresses still line up — and so that a line
    differing only in where things live is graded `~` rather than rewritten.
    Too lossy to decide whether a line is *unchanged*: see :func:`compare_line`.
    """

    text = _THUNK_NAME.sub("0x?", compare_line(text))
    text = _ADDRESS_INDEX.sub("0x?", text)
    text = _LOCAL_NAME.sub(lambda m: f"{m.group(1)}_?", text)
    return _HEX.sub("0x?", text)


def compare_line(text: str) -> str:
    """Conservative normalization, used to decide a row is *equal*.

    Whitespace, plus auto-generated location names rewritten to the address
    they encode. Nothing else is folded, because everything else is something
    the reader can see on screen: `bl sub_100018fe8` against `bl sub_100019028`
    calls a function that *moved*, and reporting that pair as identical
    contradicts the two lines sitting side by side saying otherwise. Such a
    difference is `~`, which is what :func:`normalize_line` grades it.

    `bl 0x10001a9c8` against `bl sub_10001a9c8` is a different matter: same
    address, and one view merely resolved the symbol. Diffing a database
    against itself used to mark every such line, which is noise by definition.
    """

    text = _ADDRESS_NAME.sub(lambda m: f"0x{m.group(1)}", text)
    text = _ADDRESS_OFFSET.sub(
        lambda m: f"0x{int(m.group(1), 16) + int(m.group(3), 16 if m.group(2) else 10):x}", text
    )
    return _WS.sub(" ", text).strip()


#: Token types whose *text* is incidental to what an instruction does. Keyed by
#: token type rather than matched by regex so this works on every architecture
#: rather than just the x86 register names someone happened to think of.
_SHAPE_PLACEHOLDER = {
    InstructionTextTokenType.RegisterToken: "<reg>",
    InstructionTextTokenType.IntegerToken: "<num>",
    InstructionTextTokenType.PossibleAddressToken: "<num>",
    InstructionTextTokenType.FloatingPointToken: "<num>",
    InstructionTextTokenType.CodeRelativeAddressToken: "<num>",
    InstructionTextTokenType.AddressDisplayToken: "<num>",
    InstructionTextTokenType.CharacterConstantToken: "<num>",
    InstructionTextTokenType.StackVariableToken: "<var>",
    InstructionTextTokenType.LocalVariableToken: "<var>",
    # Trailing hints such as {var_ec} or {__saved_rbp}.
    InstructionTextTokenType.AnnotationToken: "",
}

# These instructions name architectural state in a register-looking operand.
# Treating that operand as ordinary register allocation makes writes to two
# different control/key registers look like a harmless register rename. The
# set describes instruction semantics, not a particular firmware or register.
_STATE_REGISTER_MNEMONICS = {
    # ARM system registers
    "msr",
    "mrs",
    # RISC-V CSRs
    "csrrw",
    "csrrs",
    "csrrc",
    "csrrwi",
    "csrrsi",
    "csrrci",
}


def _tokens_of(line):
    """Tokens for a line, unwrapping ``LinearDisassemblyLine`` if needed."""

    contents = getattr(line, "contents", None)
    target = contents if contents is not None else line
    return getattr(target, "tokens", None)


#: Tags are about the code rather than part of it, and include the analysis's
#: own warnings (`❓️` for an unresolved stack pointer), which one database has
#: and another of the same bytes does not.
_NOT_CODE = {
    InstructionTextTokenType.TagToken,
    # Opcode bytes sit after the address separator, but they are margin too:
    # shown or not by the reader's settings, and the text beside them says the
    # same thing.
    InstructionTextTokenType.OpcodeToken,
}


def instruction_tokens(line):
    """A line's tokens with annotations — braces and contents — comments and tags removed.

    Comments are the user's notes, not the code: a function someone has
    annotated is the same function, and reporting it as changed is exactly the
    wrong way round for a database compared with the binary it was built from.

    ``None`` when the line carries no tokens at all.
    """

    tokens = _tokens_of(line)
    if not tokens:
        return None
    # Everything before the separator is the line's margin — the address
    # column, opcode bytes, tags — present or not depending on the reader's
    # settings. `100000614  _fprintf(...)` and `_fprintf(...)` are one line.
    separator = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.type == InstructionTextTokenType.AddressSeparatorToken
        ),
        None,
    )
    if separator is not None:
        tokens = tokens[separator + 1 :]
    kept, depth = [], 0
    for token in tokens:
        if token.type == InstructionTextTokenType.CommentToken:
            # A comment runs to the end of the line, and only its prose is typed
            # as one: the renderer highlights numbers inside it, so
            # `// [VM opcode 0x1e, table 1]` carries an IntegerToken that would
            # otherwise be read as an operand.
            break
        if token.type in _NOT_CODE:
            continue
        if token.type == InstructionTextTokenType.AnnotationToken:
            depth += token.text.count("{") - token.text.count("}")
            continue
        if depth <= 0:
            kept.append(token)
    return kept


def instruction_text(line, key=str) -> str:
    """A line with Binary Ninja's annotations removed, contents included.

    `{0x0}`, `{sub_10001aa08}`, `{__saved_x22}` are commentary the renderer
    appends when it has worked out what a constant evaluates to or what a
    saved register held. Whether it appends them depends on the view rather
    than on the code — one side of a diff renders them and the other does not —
    so they are dropped before anything is compared.

    Only the *braces* are typed ``AnnotationToken``; what sits between them is
    an ordinary integer or symbol token::

        ('AnnotationToken', '  {'), ('CodeSymbolToken', 'sub_10001a9c8'),
        ('AnnotationToken', '}')

    Dropping annotation-typed tokens alone therefore deletes the braces and
    glues `sub_10001a9c8` onto the end of the instruction, where it is no
    longer preceded by a word boundary and no longer folds like the address it
    is. Hence the brace depth: everything between them goes too.
    """

    tokens = instruction_tokens(line)
    if tokens is None:
        # Plain strings and the test stubs: the braces are all there is to go on.
        text = key(line)
        if _COMMENT_LINE.match(text):
            return ""
        return _TRAILING_HINTS.sub("", text)
    return "".join(token.text for token in tokens)


#: A name the disassembler resolved for a target.
_SYMBOL_TOKENS = {
    InstructionTextTokenType.CodeSymbolToken,
    InstructionTextTokenType.DataSymbolToken,
    InstructionTextTokenType.ExternalSymbolToken,
    InstructionTextTokenType.ImportToken,
}

#: A target it did not resolve, printed as a bare number.
_UNRESOLVED_TOKENS = {
    InstructionTextTokenType.PossibleAddressToken,
    InstructionTextTokenType.CodeRelativeAddressToken,
    InstructionTextTokenType.AddressDisplayToken,
}


def resolved_symbol_only(left, right) -> bool:
    """Whether two lines differ only where one side resolved a symbol.

    `bl 0x100013d54` against `bl _DERDecodeSeqContentInit` is one call, and one
    side merely knows its name — symbol recovery differs between two views of
    the same bytes, so this turns up wherever one resolved a name and the other
    did not.

    Keyed off token types, not text: an address is not a competing name, it is
    the absence of one. `bl memcpy` against `bl malloc` has a real name on both
    sides and stays a change, and so does `mov eax, ebx` against
    `mov eax, 0x1`, where the differing pair is a register and a constant
    rather than a symbol and an address.
    """

    left_tokens, right_tokens = instruction_tokens(left), instruction_tokens(right)
    if not left_tokens or not right_tokens or len(left_tokens) != len(right_tokens):
        return False
    differing = [(a, b) for a, b in zip(left_tokens, right_tokens, strict=True) if a.text != b.text]
    if not differing:
        return False
    return all(
        {a.type, b.type} & _SYMBOL_TOKENS and {a.type, b.type} & _UNRESOLVED_TOKENS
        for a, b in differing
    )


def _spells_its_address(token) -> bool:
    """Whether a token prints nothing but the address it stands for.

    A bare number, or a placeholder such as `sub_453694` that encodes it; either
    way the view had no name for the target.
    """

    if token.type in _UNRESOLVED_TOKENS:
        return True
    match = _ADDRESS_NAME.fullmatch(token.text.strip())
    return (
        token.type in _SYMBOL_TOKENS
        and match is not None
        and int(match.group(1), 16) == token.value
    )


def _operand_tokens(line):
    """:func:`instruction_tokens`, with a symbol's `[index]` or `+offset` folded in.

    A view that defined a variable spells an address inside it as that variable
    plus an index — `s_data_data[1]` where the other has `data_591000` — and the
    symbol token's value is already the address meant, index included. Folding
    the suffix lets the two be compared as the one operand they are; the value
    comparison in :func:`resolved_same_target` is what makes that safe.
    """

    tokens = instruction_tokens(line)
    if not tokens:
        return tokens
    kept, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        kept.append(token)
        index += 1
        if token.type not in _SYMBOL_TOKENS:
            continue
        texts = [t.text.strip() for t in tokens[index : index + 3]]
        if texts[:1] == ["["] and texts[2:3] == ["]"]:
            index += 3
        elif texts[:1] == ["+"] and len(texts) > 1:
            index += 2
    return kept


def resolved_same_target(left, right) -> bool:
    """Whether every difference is one side naming the address the other printed.

    :func:`resolved_symbol_only` only knows that a name stands where an address
    does. The tokens say more: a symbol token carries the address it names, so
    `bl 0x431970` against `bl __stack_chk_fail` whose value is 0x431970 is the
    same call, and so is `bl sub_453694` against a `bl vm_interp_dispatch` the
    user renamed on one side only — the usual state of an annotated database
    against a fresh build. This is :func:`compare_line`'s `sub_...` rule for
    names that do not spell their address; a target that *moved* still comes
    out `~`, and two different real names are still a change.
    """

    left_tokens, right_tokens = _operand_tokens(left), _operand_tokens(right)
    if not left_tokens or not right_tokens or len(left_tokens) != len(right_tokens):
        return False
    differing = [(a, b) for a, b in zip(left_tokens, right_tokens, strict=True) if a.text != b.text]
    return bool(differing) and all(
        a.value
        and a.value == b.value
        and _spells_its_address(a) != _spells_its_address(b)
        and {a.type, b.type} & _SYMBOL_TOKENS
        for a, b in differing
    )


def shape_signature(line) -> str | None:
    """What the instruction *does*, with register and literal names erased.

    Lets register reallocation be graded like offset churn rather than as a
    rewrite: adding one local variable makes a compiler renumber registers
    through a whole loop, which would otherwise paint every line as changed.
    Returns ``None`` for lines that carry no token information, so plain strings
    simply skip this tier.
    """

    tokens = _tokens_of(line)
    if not tokens:
        return None

    mnemonic = next(
        (
            token.text.strip().lower()
            for token in tokens
            if token.type == InstructionTextTokenType.InstructionToken
        ),
        "",
    )
    preserve_registers = mnemonic in _STATE_REGISTER_MNEMONICS
    parts: list[str] = []
    for token in tokens:
        placeholder = _SHAPE_PLACEHOLDER.get(token.type)
        if preserve_registers and token.type == InstructionTextTokenType.RegisterToken:
            placeholder = None
        parts.append(token.text if placeholder is None else placeholder)
    return _WS.sub(" ", "".join(parts)).strip()


@dataclass
class AlignedRow:
    """One row of a side-by-side view. Either side may be ``None`` (a gap)."""

    left: object | None
    right: object | None
    status: LineStatus


@dataclass
class BlockPair:
    left_addr: int | None
    right_addr: int | None
    status: BlockStatus


@dataclass
class BlockAlignment:
    """Result of matching two functions' basic blocks at one IL level."""

    pairs: list[BlockPair] = field(default_factory=list)
    left_status: dict[int, BlockStatus] = field(default_factory=dict)
    right_status: dict[int, BlockStatus] = field(default_factory=dict)
    left_to_right: dict[int, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------


#: The property that forces each IL to exist. Reading it generates the IL if it
#: has not been already; the ``*_if_available`` variants deliberately do not.
_IL_PROPERTY = {"LLIL": "llil", "MLIL": "mlil", "HLIL": "hlil"}


def ensure_il(func: BNFunction, level: str):
    """Generate a function's IL before anything tries to render it.

    Asked for IL that does not exist yet, the linear view returns a single
    "Loading..." placeholder and fills the real text in asynchronously. The
    text panes render once into a QTextEdit, so that placeholder is what stays
    on screen. Switching to the Basic Blocks tab and back used to be the fix,
    because ``il_basic_blocks`` reads ``func.llil`` and generates the IL as a
    side effect — this is that same generation, done where it is needed.

    Disassembly needs it too, though it is not IL: the core resolves call
    targets and appends ``{var_...}`` only while the function's LLIL exists, and
    evicts that from its analysis cache (``analysis.limits.cacheSize``) for
    functions nobody has looked at. Unasked, three calls in four came out as
    `bl 0x431970` rather than `bl __stack_chk_fail`, on whichever side the
    cache happened to have dropped, and every one of them read as a difference.

    Returns the IL function, or ``None`` when there is none to generate.
    """

    attribute = "llil" if level == "Disassembly" else _IL_PROPERTY.get(level)
    if attribute is None:
        return None
    try:
        il = getattr(func, attribute)
    except Exception:
        # A function whose analysis was skipped has no IL and never will; the
        # linear view says so itself, which beats refusing to render.
        return None
    return None if level == "Disassembly" else il


def ensure_rendering(func: BNFunction, level: str | RenderLevel) -> None:
    """`ensure_il`, plus the language representation when one is asked for.

    A language is rendered from HLIL, but it is a separate object with its own
    lazy generation, and the linear view shows "Loading..." until it exists.
    """

    level = render_level(level)
    ensure_il(func, level.level)
    if level.language:
        try:
            func.language_representation(level.language)
        except Exception:
            pass


def function_lines(
    bv: BinaryView, func: BNFunction, level: str | RenderLevel
) -> list[LinearDisassemblyLine]:
    """Whole-function rendering at one IL level or language, in linear-view order.

    The cursor advances in chunks, so a single ``get_next_linear_disassembly_lines``
    call would only return the function header.
    """

    from binaryninja import DisassemblySettings, LinearViewCursor

    level = render_level(level)
    ensure_rendering(func, level)

    obj = level.linear_object(func, DisassemblySettings())
    cursor = LinearViewCursor(obj)
    cursor.seek_to_begin()

    lines: list[LinearDisassemblyLine] = []
    while True:
        chunk = bv.get_next_linear_disassembly_lines(cursor)
        if not chunk:
            break
        lines.extend(chunk)
    return lines


def il_basic_blocks(func: BNFunction, level: str):
    """Basic blocks at one IL level.

    All three IL basic-block classes inherit from ``BasicBlock``, so callers can
    treat the result uniformly (``.start``, ``.outgoing_edges``,
    ``.disassembly_text``).
    """

    if level == "Disassembly":
        ensure_il(func, level)
        return list(func.basic_blocks)
    il = ensure_il(func, level)
    if il is None:
        return []
    return list(il.basic_blocks)


def block_text(block) -> list[str]:
    """A block's code, without annotations or comment lines.

    What decides whether two blocks are the same, so it has to ignore what
    :func:`align_lines` ignores: a block whose only difference is a comment
    would otherwise be reported changed and have nothing in it tinted.
    """

    texts = (instruction_text(line) for line in block.disassembly_text)
    return [text for text in texts if text.strip()]


def _block_graph(blocks) -> networkx.DiGraph:
    graph = networkx.DiGraph()
    for block in blocks:
        graph.add_node(block.start)
        for edge in block.outgoing_edges:
            graph.add_edge(block.start, edge.target.start)
    return graph


# --------------------------------------------------------------------------
# Basic block alignment
# --------------------------------------------------------------------------


def _anchor_identical_blocks(left_blocks, right_blocks) -> dict[int, int]:
    """Pair blocks whose normalized instruction text is identical.

    This is the same idea as QBinDiff's ``compute_basic_block_match`` (a
    multiset hash over instructions), but computed on normalized text so it
    works at every IL level rather than only on machine instructions.
    """

    def signature(block) -> tuple[str, ...]:
        return tuple(sorted(normalize_line(line) for line in block_text(block)))

    left_by_sig: dict[tuple[str, ...], list[int]] = {}
    for block in left_blocks:
        left_by_sig.setdefault(signature(block), []).append(block.start)
    right_by_sig: dict[tuple[str, ...], list[int]] = {}
    for block in right_blocks:
        right_by_sig.setdefault(signature(block), []).append(block.start)

    anchors: dict[int, int] = {}
    for sig, left_addrs in left_by_sig.items():
        right_addrs = right_by_sig.get(sig)
        if not right_addrs:
            continue
        # Only trust unambiguous signatures; repeated identical blocks (common
        # for single-instruction epilogues) would otherwise pair arbitrarily.
        if len(left_addrs) == 1 and len(right_addrs) == 1:
            anchors[left_addrs[0]] = right_addrs[0]
    return anchors


def align_blocks(
    left_func: BNFunction, right_func: BNFunction, level: str = "Disassembly"
) -> BlockAlignment:
    """Match basic blocks between two functions at one IL level."""

    left_blocks = il_basic_blocks(left_func, level)
    right_blocks = il_basic_blocks(right_func, level)

    alignment = BlockAlignment()
    if not left_blocks and not right_blocks:
        return alignment

    left_by_addr = {b.start: b for b in left_blocks}
    right_by_addr = {b.start: b for b in right_blocks}

    matched = _anchor_identical_blocks(left_blocks, right_blocks)

    remaining_left = [a for a in left_by_addr if a not in matched]
    remaining_right = [a for a in right_by_addr if a not in set(matched.values())]

    if remaining_left and remaining_right:
        matched.update(_align_remaining(left_blocks, right_blocks, remaining_left, remaining_right))

    for left_addr, right_addr in matched.items():
        identical = _blocks_identical(left_by_addr[left_addr], right_by_addr[right_addr])
        status = BlockStatus.IDENTICAL if identical else BlockStatus.CHANGED
        alignment.pairs.append(BlockPair(left_addr, right_addr, status))
        alignment.left_status[left_addr] = status
        alignment.right_status[right_addr] = status
        alignment.left_to_right[left_addr] = right_addr

    for addr in left_by_addr:
        if addr not in alignment.left_status:
            alignment.left_status[addr] = BlockStatus.UNMATCHED
            alignment.pairs.append(BlockPair(addr, None, BlockStatus.UNMATCHED))
    for addr in right_by_addr:
        if addr not in alignment.right_status:
            alignment.right_status[addr] = BlockStatus.UNMATCHED
            alignment.pairs.append(BlockPair(None, addr, BlockStatus.UNMATCHED))

    alignment.pairs.sort(key=lambda p: (p.left_addr is None, p.left_addr or p.right_addr or 0))
    return alignment


#: Minimum normalized-text similarity before two leftover blocks are called a
#: pair. Below this, reporting them as unmatched-then-structural is safer.
_TEXT_MATCH_FLOOR = 0.5


def _align_remaining(
    left_blocks, right_blocks, remaining_left: list[int], remaining_right: list[int]
) -> dict[int, int]:
    """Pair the blocks that exact matching left over.

    Instruction text leads and CFG topology only fills the gaps. Topology alone
    is a weak signal inside one function, where many blocks share the same in and
    out degree: ``DiGraphDiffer`` will confidently pair a loop body with an
    epilogue, and then *every* line of both renders as a difference, burying the
    handful of real changes. Two builds of the same source agree far more on what
    the instructions say than on the exact shape of the graph.
    """

    matched = _greedy_text_match(left_blocks, right_blocks, remaining_left, remaining_right)

    left_over = [a for a in remaining_left if a not in matched]
    paired = set(matched.values())
    right_over = [a for a in remaining_right if a not in paired]
    if left_over and right_over:
        matched.update(_structural_match(left_blocks, right_blocks, left_over, right_over))
    return matched


def _structural_match(
    left_blocks, right_blocks, remaining_left: list[int], remaining_right: list[int]
) -> dict[int, int]:
    """Align blocks by CFG shape alone, for the ones text could not pair.

    This is the right tool for a block that was rewritten heavily enough that its
    text no longer resembles the original, and the wrong tool for anything else.
    """

    left_graph = _block_graph(left_blocks).subgraph(remaining_left).copy()
    right_graph = _block_graph(right_blocks).subgraph(remaining_right).copy()
    if not len(left_graph) or not len(right_graph):
        return {}

    try:
        from qbindiff.differ import DiGraphDiffer

        differ = DiGraphDiffer(left_graph, right_graph)
        mapping = differ.compute_matching()
        if mapping is not None:
            return {m.primary: m.secondary for m in mapping}
    except Exception:
        # DiGraphDiffer raises on empty graphs and can fail to converge on
        # pathological CFGs; leaving these unmatched is always safe.
        pass
    return {}


def _greedy_text_match(
    left_blocks, right_blocks, remaining_left: list[int], remaining_right: list[int]
) -> dict[int, int]:
    """Pair leftover blocks by normalized instruction-text similarity, best first."""

    left_by_addr = {b.start: b for b in left_blocks}
    right_by_addr = {b.start: b for b in right_blocks}

    # Normalize once per block, not once per candidate pair.
    def normalized(by_addr, addrs):
        return {a: [normalize_line(t) for t in block_text(by_addr[a])] for a in addrs}

    left_text = normalized(left_by_addr, remaining_left)
    right_text = normalized(right_by_addr, remaining_right)

    scored = []
    for left_addr, left_lines in left_text.items():
        for right_addr, right_lines in right_text.items():
            matcher = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
            # quick_ratio only bounds the real ratio from above, which is enough
            # to reject but not to rank; ranking is exactly what this does.
            if matcher.quick_ratio() <= _TEXT_MATCH_FLOOR:
                continue
            ratio = matcher.ratio()
            if ratio > _TEXT_MATCH_FLOOR:
                scored.append((ratio, left_addr, right_addr))

    scored.sort(reverse=True)
    used_left: set[int] = set()
    used_right: set[int] = set()
    matched: dict[int, int] = {}
    for _ratio, left_addr, right_addr in scored:
        if left_addr in used_left or right_addr in used_right:
            continue
        matched[left_addr] = right_addr
        used_left.add(left_addr)
        used_right.add(right_addr)
    return matched


def _blocks_identical(left_block, right_block) -> bool:
    """Whether two blocks read the same, by the rules the lines are graded on.

    Anything looser than :func:`align_lines` lets a block be called changed
    while none of its lines is, and the graph then reports a change it has
    nothing to tint for; anything stricter hides an immediate that changed.
    """

    rows = align_lines(list(left_block.disassembly_text), list(right_block.disassembly_text))
    return classify_rows(rows) is FunctionStatus.IDENTICAL


#: Block counts, in the order a summary reads them.
BLOCK_SUMMARY_KEYS = (
    "identical",
    "operands only",
    "changed",
    "only in primary",
    "only in secondary",
)


def summarize_blocks(alignment: BlockAlignment, left_blocks, right_blocks) -> dict[str, int]:
    """How many blocks of a pair are identical, changed, or on one side only.

    Both sides count: a block only the secondary has is as much a part of the
    diff as one only the primary has. A matched block that differs only in its
    operands is kept apart from one that really changed, by the same grading
    the lines get, so the count agrees with the colours inside the nodes.
    """

    left_by_addr = {block.start: block for block in left_blocks}
    right_by_addr = {block.start: block for block in right_blocks}
    counts = dict.fromkeys(BLOCK_SUMMARY_KEYS, 0)
    for pair in alignment.pairs:
        if pair.left_addr is None:
            counts["only in secondary"] += 1
        elif pair.right_addr is None:
            counts["only in primary"] += 1
        elif pair.status is BlockStatus.IDENTICAL:
            counts["identical"] += 1
        else:
            rows = align_lines(
                list(left_by_addr[pair.left_addr].disassembly_text),
                list(right_by_addr[pair.right_addr].disassembly_text),
            )
            minor = classify_rows(rows) is FunctionStatus.MINOR
            counts["operands only" if minor else "changed"] += 1
    return counts


def format_block_summary(counts: dict[str, int]) -> str:
    """``blocks: 3 identical · 1 changed``, leaving out what there is none of."""

    parts = [f"{counts[key]} {key}" for key in BLOCK_SUMMARY_KEYS if counts.get(key)]
    return "blocks: " + " \u00b7 ".join(parts) if parts else ""


# --------------------------------------------------------------------------
# Line alignment
# --------------------------------------------------------------------------


def align_lines(
    left_lines: Sequence[object],
    right_lines: Sequence[object],
    key=str,
) -> list[AlignedRow]:
    """Row-align two line sequences, padding with gaps so both sides match up.

    ``key`` extracts comparable text from each line; the default handles both
    ``DisassemblyTextLine`` and ``LinearDisassemblyLine`` because both define
    ``__str__``.
    """

    # Lines with no code on them take no part in the alignment, or a comment on
    # one side reads as an added instruction. They are woven back in below.
    left_code = [i for i, line in enumerate(left_lines) if instruction_text(line, key).strip()]
    right_code = [i for i, line in enumerate(right_lines) if instruction_text(line, key).strip()]
    left_keys = [normalize_line(instruction_text(left_lines[i], key)) for i in left_code]
    right_keys = [normalize_line(instruction_text(right_lines[i], key)) for i in right_code]

    def classify(left, right) -> LineStatus:
        """Grade a matched pair, from identical through to structurally different."""

        # Compared without annotations: those say what the renderer knows about
        # the code, not what the code is, and one view knows more than the other.
        left_text, right_text = instruction_text(left, key), instruction_text(right, key)
        if left_text == right_text:
            return LineStatus.EQUAL
        if compare_line(left_text) == compare_line(right_text):
            return LineStatus.EQUAL
        if resolved_same_target(left, right):
            return LineStatus.EQUAL
        left_norm, right_norm = normalize_line(left_text), normalize_line(right_text)
        if left_norm == right_norm:
            return LineStatus.MINOR
        if resolved_symbol_only(left, right):
            # One side resolved a symbol the other left as a bare address.
            return LineStatus.MINOR
        left_shape = shape_signature(left)
        if left_shape is not None and left_shape == shape_signature(right):
            # Same operation and operand kinds; only registers or literals moved.
            return LineStatus.MINOR
        return LineStatus.CHANGED

    pairs: list[tuple[int | None, int | None]] = []
    matcher = difflib.SequenceMatcher(None, left_keys, right_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "replace"):
            # Equal under the aggressive key still allows literals to differ,
            # which is exactly the case that used to render as unchanged.
            for offset in range(max(i2 - i1, j2 - j1)):
                left = left_code[i1 + offset] if i1 + offset < i2 else None
                right = right_code[j1 + offset] if j1 + offset < j2 else None
                pairs.append((left, right))
        elif tag == "delete":
            pairs.extend((left_code[i], None) for i in range(i1, i2))
        elif tag == "insert":
            pairs.extend((None, right_code[j]) for j in range(j1, j2))

    rows: list[AlignedRow] = []
    next_left = next_right = 0

    def commentary(left_stop: int, right_stop: int) -> None:
        # Paired up side by side, so a blank line both renderings share stays
        # one row rather than two.
        nonlocal next_left, next_right
        left_notes = left_lines[next_left:left_stop]
        right_notes = right_lines[next_right:right_stop]
        for offset in range(max(len(left_notes), len(right_notes))):
            left = left_notes[offset] if offset < len(left_notes) else None
            right = right_notes[offset] if offset < len(right_notes) else None
            rows.append(AlignedRow(left, right, LineStatus.COMMENT))
        next_left, next_right = max(next_left, left_stop), max(next_right, right_stop)

    for left_index, right_index in pairs:
        commentary(
            next_left if left_index is None else left_index,
            next_right if right_index is None else right_index,
        )
        left = None if left_index is None else left_lines[left_index]
        right = None if right_index is None else right_lines[right_index]
        if left is None:
            rows.append(AlignedRow(None, right, LineStatus.ADDED))
        elif right is None:
            rows.append(AlignedRow(left, None, LineStatus.REMOVED))
        else:
            rows.append(AlignedRow(left, right, classify(left, right)))
        next_left = next_left if left_index is None else left_index + 1
        next_right = next_right if right_index is None else right_index + 1
    commentary(len(left_lines), len(right_lines))
    return rows


def side_statuses(rows: Sequence[AlignedRow]) -> tuple[list[LineStatus], list[LineStatus]]:
    """Split aligned rows back into one status per real line, per side.

    Gap rows contribute nothing to the side they are padding, so each returned
    list lines up one-to-one with that side's original lines. That is what lets
    a caller zip the statuses onto a flow graph node's own ``lines``.
    """

    left: list[LineStatus] = []
    right: list[LineStatus] = []
    for row in rows:
        if row.left is not None:
            left.append(row.status)
        if row.right is not None:
            right.append(row.status)
    return left, right


def align_line_statuses(left_lines, right_lines) -> tuple[list[LineStatus], list[LineStatus]]:
    """Per-line statuses for two line sequences, one entry per input line.

    Callers pass the exact lines they intend to color. Deriving statuses from one
    rendering and applying them to another does not work: a flow graph node
    prepends a symbol label (``main:``) that ``BasicBlock.disassembly_text``
    omits, and only for some blocks, so the two are off by a varying amount.
    """

    return side_statuses(align_lines(list(left_lines), list(right_lines)))


def align_function_text(
    left_bv: BinaryView,
    left_func: BNFunction,
    right_bv: BinaryView,
    right_func: BNFunction,
    level: str | RenderLevel,
) -> list[AlignedRow]:
    """Full side-by-side alignment of two functions at one IL level or language."""

    left_lines = function_lines(left_bv, left_func, level)
    right_lines = function_lines(right_bv, right_func, level)
    return align_lines(left_lines, right_lines)


def line_address(line) -> int | None:
    """The address a rendered line stands for, from either kind of line object."""

    contents = getattr(line, "contents", None)
    address = getattr(contents if contents is not None else line, "address", None)
    return address if isinstance(address, int) else None


def address_pairs(rows: Iterable[AlignedRow]) -> list[tuple[int, int]]:
    """``(left, right)`` addresses of every row that has a line on both sides.

    What "the same place on the other side" means, for following a cursor.
    """

    pairs = []
    for row in rows:
        if row.left is None or row.right is None:
            continue
        left, right = line_address(row.left), line_address(row.right)
        if left is not None and right is not None:
            pairs.append((left, right))
    return pairs


def counterpart(pairs: Sequence[tuple[int, int]], address: int, from_left: bool) -> int | None:
    """Where ``address`` on one side sits on the other, or ``None`` with no pairs.

    An exact match wins — the first, since one address renders as several
    lines at the IL levels and the first is where it starts. Otherwise the
    nearest address, because a line only one side has (added, removed) is not
    in any pair, and landing next to where it would be beats not moving.
    """

    here, there = (0, 1) if from_left else (1, 0)
    best, best_distance = None, None
    for pair in pairs:
        distance = abs(pair[here] - address)
        if distance == 0:
            return pair[there]
        if best_distance is None or distance < best_distance:
            best, best_distance = pair[there], distance
    return best


def anchor_row(rows: Sequence[AlignedRow], address: int) -> int:
    """The row to scroll to so that ``address`` stays where the reader left it.

    Switching the view keeps the place by address, since two renderings share
    nothing else — and not every rendering has every address: one HLIL
    statement stands for several instructions. So an exact match wins, and
    otherwise the row whose address is nearest.
    """

    best, best_distance = 0, None
    for index, row in enumerate(rows):
        for line in (row.left, row.right):
            candidate = line_address(line) if line is not None else None
            if candidate is None:
                continue
            distance = abs(candidate - address)
            if distance == 0:
                return index
            if best_distance is None or distance < best_distance:
                best, best_distance = index, distance
    return best


class FunctionStatus(str, Enum):
    """How a matched pair compares, as a whole.

    QBinDiff's similarity is a distance between feature vectors, not a verdict
    on the code: two functions score 1.0 while every address in them differs.
    Calling that "identical" contradicts the panes, which mark those lines `~`.
    These come from the same per-line classification the panes render, so the
    table and the text agree by construction.
    """

    IDENTICAL = "identical"
    #: Only operand spellings differ — addresses, immediates, stack offsets,
    #: register allocation. The `~` tier, at function scale.
    MINOR = "offsets only"
    CHANGED = "changed"
    #: Too large to classify without making the table stutter; no difference
    #: verdict has been reached, so the user-facing text must say that.
    UNKNOWN = "unclassified"


#: Above this many instructions on either side, classifying a pair is skipped.
#: The table asks for this while painting, so the cost has to stay bounded; the
#: text panes will still classify the function line by line when it is selected.
MAX_CLASSIFY_INSTRUCTIONS = 4000


def _instruction_count(func: BNFunction) -> int:
    return sum(block.instruction_count for block in func.basic_blocks)


def classify_rows(rows: Iterable[AlignedRow]) -> FunctionStatus:
    """Summarize aligned rows into one status for the pair they came from."""

    statuses = {row.status for row in rows} - {LineStatus.GAP, LineStatus.COMMENT}
    if not statuses - {LineStatus.EQUAL}:
        return FunctionStatus.IDENTICAL
    if not statuses - {LineStatus.EQUAL, LineStatus.MINOR}:
        return FunctionStatus.MINOR
    return FunctionStatus.CHANGED


def text_similarity(rows: Iterable[AlignedRow]) -> float:
    """How much of a pair is the same code, from the rows already aligned.

    A real answer to "how different is this function", which QBinDiff's own
    score is not: that one is a MinHash over whole basic blocks, so a
    single-block function that gained one instruction shares no shingle with
    its previous build and reads 0.000 while being the same code.

    Counted per line, over the longer side: `EQUAL` and `MINOR` are the same
    code — the `~` tier is a rebase or a register reallocation, not an edit —
    while a changed, added or removed line is not. `GAP` rows are padding the
    alignment inserted opposite an added or removed line, and are already
    counted through the line they sit against.
    """

    total = same = 0
    for row in rows:
        if row.status in (LineStatus.GAP, LineStatus.COMMENT):
            continue
        total += 1
        if row.status in (LineStatus.EQUAL, LineStatus.MINOR):
            same += 1
    return same / total if total else 1.0


def function_instruction_lines(func: BNFunction, level: str = "Disassembly") -> list:
    """Every instruction of a function, in address order, as rendered lines.

    Taken from the basic blocks, which is what the graph pane has always used,
    and deliberately *not* from the linear view. The linear rendering carries
    the function's prototype, is produced asynchronously, and — the reason this
    exists — hands back lines without tokens until something else has drawn the
    function. No tokens means `shape_signature` cannot answer, which silently
    demotes "same operation, different registers" from `~` to a full rewrite:
    every unvisited row in the match table read "changed" because of it.

    Block text has none of those properties. It is the instructions, tokens
    included, every time.
    """

    blocks = il_basic_blocks(func, level)
    lines: list = []
    for block in sorted(blocks, key=lambda b: b.start):
        for line in block.disassembly_text:
            # Lines that are nothing but an annotation — `{Case 0x114}` above a
            # switch target, and the like — are emitted by one view and not the
            # other. Keeping them makes the side that has them look like it
            # gained instructions, which is how a database diffed against the
            # bytes it was built from came out "changed".
            if instruction_text(line).strip():
                lines.append(line)
    return lines


def classify_pair(
    left_func: BNFunction,
    right_func: BNFunction,
    level: str = "Disassembly",
) -> tuple[FunctionStatus | None, list[AlignedRow]]:
    """Classify a matched pair, returning the verdict and the rows behind it.

    ``None`` when either side rendered nothing it should have, so the caller can
    ask again rather than record a verdict drawn from an empty function.
    """

    if (
        _instruction_count(left_func) > MAX_CLASSIFY_INSTRUCTIONS
        or _instruction_count(right_func) > MAX_CLASSIFY_INSTRUCTIONS
    ):
        return FunctionStatus.UNKNOWN, []

    left_lines = function_instruction_lines(left_func, level)
    right_lines = function_instruction_lines(right_func, level)
    if (left_func.basic_blocks and not left_lines) or (right_func.basic_blocks and not right_lines):
        return None, []

    rows = align_lines(left_lines, right_lines)
    return classify_rows(rows), rows


def summarize(rows: Iterable[AlignedRow]) -> dict[str, int]:
    counts = {status.value: 0 for status in LineStatus}
    for row in rows:
        counts[row.status.value] += 1
    return counts


def edit_pattern(rows: Sequence[AlignedRow]) -> str | None:
    """Fingerprint the non-minor edits in one aligned function pair.

    This is evidence of a *repeated edit*, not a verdict that the edit is
    unimportant. Keep literal values and resolved targets distinct: folding
    them as ``normalize_line`` does could group two security-relevant constant
    changes. Function names and binary-specific logging conventions play no
    part in the fingerprint. Row count and edit positions prevent a shared
    one-line operation in unrelated functions from being grouped too eagerly.
    """

    edits = [
        (
            index,
            row.status.value,
            compare_line(instruction_text(row.left)) if row.left is not None else None,
            compare_line(instruction_text(row.right)) if row.right is not None else None,
        )
        for index, row in enumerate(rows)
        if row.status in (LineStatus.CHANGED, LineStatus.ADDED, LineStatus.REMOVED)
    ]
    if not edits:
        return None
    payload = json.dumps((len(rows), edits), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def pairing_needs_review(matcher_similarity: float, line_similarity: float | None) -> bool:
    """Flag a pairing when independent code comparisons both find little support.

    Belief-propagation confidence is conditional on the available candidates,
    so even a completely unrelated forced pair can have confidence near 1.
    This is a review cue, not an automatic rejection: a genuine rewrite can
    share almost no code with its old version.
    """

    return line_similarity is not None and matcher_similarity < 0.05 and line_similarity < 0.25
