"""Minimal in-memory stand-in for the Binary Ninja API.

Binary Ninja's core cannot always be imported outside the GUI (it needs a
matching libstdc++ and a license), so these stubs let the backend and
alignment logic be exercised against the real qbindiff on any machine. They
model only the surface ``binja_diff.core`` actually touches.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntEnum


class BranchType(IntEnum):
    UnconditionalBranch = 0
    FalseBranch = 1
    TrueBranch = 2
    CallDestination = 3
    FunctionReturn = 4
    SystemCall = 5
    IndirectBranch = 6
    ExceptionBranch = 7


class SymbolType(IntEnum):
    FunctionSymbol = 0
    ImportAddressSymbol = 1
    ImportedFunctionSymbol = 2
    DataSymbol = 3
    ImportedDataSymbol = 4
    ExternalSymbol = 5
    LibraryFunctionSymbol = 6


class InstructionTextTokenType(IntEnum):
    TextToken = 0
    InstructionToken = 1
    OperandSeparatorToken = 2
    RegisterToken = 3
    IntegerToken = 4
    PossibleAddressToken = 5
    BeginMemoryOperandToken = 6
    EndMemoryOperandToken = 7
    FloatingPointToken = 8
    AnnotationToken = 9
    CodeRelativeAddressToken = 10
    OpcodeToken = 16
    CharacterConstantToken = 18
    TagToken = 24
    CommentToken = 29
    OperationToken = 36
    BraceToken = 39
    CodeSymbolToken = 64
    DataSymbolToken = 65
    LocalVariableToken = 66
    ImportToken = 67
    AddressDisplayToken = 68
    ExternalSymbolToken = 70
    StackVariableToken = 71
    AddressSeparatorToken = 72


class AnalysisState(IntEnum):
    InitialState = 0
    HoldState = 1
    IdleState = 2
    DiscoveryState = 3
    DisassembleState = 4
    AnalyzeState = 5
    ExtendedAnalyzeState = 6


@dataclass
class AnalysisProgress:
    state: AnalysisState = AnalysisState.IdleState
    count: int = 0
    total: int = 0


class LinearDisassemblyLineType(IntEnum):
    BlankLineType = 0
    BasicLineType = 1
    CodeDisassemblyLineType = 2
    FunctionHeaderLineType = 5
    FunctionHeaderStartLineType = 6
    FunctionHeaderEndLineType = 7
    LocalVariableLineType = 9
    FunctionEndLineType = 11
    AnalysisWarningLineType = 19


class FunctionGraphType(IntEnum):
    NormalFunctionGraph = 0
    LowLevelILFunctionGraph = 1
    MediumLevelILFunctionGraph = 4
    HighLevelILFunctionGraph = 8


@dataclass
class Token:
    type: InstructionTextTokenType
    text: str
    value: int = 0

    def __str__(self) -> str:
        return self.text


@dataclass
class Edge:
    type: BranchType
    target: BasicBlock


@dataclass
class Branch:
    type: BranchType
    target: int = 0


@dataclass
class InstructionInfo:
    length: int = 1
    branches: list = field(default_factory=list)


@dataclass
class TextLine:
    text: str

    def __str__(self) -> str:
        return self.text


class BasicBlock:
    def __init__(self, start: int, instructions: list, function=None):
        self.start = start
        #: list of (tokens, length)
        self.instructions = instructions
        self.function = function
        self.outgoing_edges: list[Edge] = []

    @property
    def length(self) -> int:
        return sum(length for _tokens, length in self.instructions)

    @property
    def end(self) -> int:
        return self.start + self.length

    @property
    def instruction_count(self) -> int:
        return len(self.instructions)

    def __iter__(self):
        return iter(self.instructions)

    @property
    def disassembly_text(self):
        lines = []
        addr = self.start
        for tokens, length in self.instructions:
            lines.append(TextLine("".join(t.text for t in tokens)))
            addr += length
        return lines


class Symbol:
    def __init__(self, sym_type: SymbolType):
        self.type = sym_type


class Function:
    def __init__(self, view, start: int, name: str, blocks, sym_type=SymbolType.FunctionSymbol):
        self.view = view
        self.start = start
        self.name = name
        self.basic_blocks = blocks
        self.symbol = Symbol(sym_type)
        self.callee_addresses: list[int] = []
        self._comments: dict[int, str] = {}
        for block in blocks:
            block.function = self

    @property
    def callers(self):
        return [f for f in self.view.functions if self.start in f.callee_addresses]

    def get_comment_at(self, addr: int) -> str:
        return self._comments.get(addr, "")


class Architecture:
    max_instr_length = 16

    def get_instruction_info(self, data, addr):
        return InstructionInfo(length=1, branches=[])


@dataclass
class Section:
    name: str
    start: int
    end: int


class FileMetadata:
    def __init__(self, filename: str):
        self.filename = filename
        self.original_filename = filename
        self.has_database = False

    def close(self):
        pass


class BinaryView:
    def __init__(self, name: str, data: bytes = b""):
        self.file = FileMetadata(name)
        self.arch = Architecture()
        self.functions: list[Function] = []
        self.types: dict = {}
        self.view_type = "Stub"
        #: name -> Section, as BinaryView.sections is keyed.
        self.sections: dict = {}
        self.analysis_progress = AnalysisProgress()
        self.analysis_waits = 0
        self._data = data
        self._metadata: dict = {}

    def update_analysis_and_wait(self) -> None:
        self.analysis_waits += 1

    def get_function_at(self, addr: int):
        return next((f for f in self.functions if f.start == addr), None)

    @contextmanager
    def undoable_transaction(self):
        """Commits on success, reverts on exception, like the real one.

        Modelled rather than stubbed away: reverting on failure is the property
        that keeps a half-applied symbol port out of somebody's database.
        """

        snapshot = [(f, f.name) for f in self.functions]
        try:
            yield
        except Exception:
            for func, name in snapshot:
                func.name = name
            raise

    @property
    def length(self) -> int:
        # Matches the real API, which has no __len__ here. Modelling it as one
        # is what let `len(bv)` through the tests and fail in Binary Ninja.
        return len(self._data)

    def read(self, addr: int, length: int) -> bytes:
        return bytes((addr + i) & 0xFF for i in range(length))

    # The real store is typed (see binaryninja.metadata); the plugin only ever
    # puts a string in it, so a dict models it faithfully enough.
    def store_metadata(self, key: str, value, isAuto: bool = False) -> None:
        self._metadata[key] = value

    def query_metadata(self, key: str):
        if key not in self._metadata:
            raise KeyError(key)
        return self._metadata[key]

    def remove_metadata(self, key: str) -> None:
        self._metadata.pop(key, None)

    def get_data_refs_from(self, addr: int):
        return []

    def get_data_var_at(self, addr: int):
        return None

    def get_string_at(self, addr: int):
        return None


def install() -> None:
    """Register the stub modules in ``sys.modules``."""

    bn = types.ModuleType("binaryninja")
    bn.__path__ = []  # make it a package so `binaryninja.enums` resolves
    bn.BinaryView = BinaryView
    bn.Function = Function
    bn.BasicBlock = BasicBlock
    bn.BackgroundTaskThread = object
    bn.log_info = bn.log_warn = bn.log_error = bn.log_debug = lambda *a, **k: None
    bn.load = lambda *a, **k: None
    bn.execute_on_main_thread = lambda fn: fn()
    bn.DisassemblySettings = lambda *a, **k: None
    # The plugin entry point registers its UI only under a real GUI; keep the
    # headless tests on the non-UI path.
    bn.core_ui_enabled = lambda: False

    enums = types.ModuleType("binaryninja.enums")
    enums.BranchType = BranchType
    enums.SymbolType = SymbolType
    enums.InstructionTextTokenType = InstructionTextTokenType
    enums.FunctionGraphType = FunctionGraphType
    enums.AnalysisState = AnalysisState
    enums.LinearDisassemblyLineType = LinearDisassemblyLineType
    bn.enums = enums

    function_mod = types.ModuleType("binaryninja.function")
    function_mod.DisassemblyTextLine = TextLine
    bn.function = function_mod

    linear_mod = types.ModuleType("binaryninja.lineardisassembly")
    linear_mod.LinearDisassemblyLine = TextLine
    bn.lineardisassembly = linear_mod

    sys.modules["binaryninja"] = bn
    sys.modules["binaryninja.enums"] = enums
    sys.modules["binaryninja.function"] = function_mod
    sys.modules["binaryninja.lineardisassembly"] = linear_mod
