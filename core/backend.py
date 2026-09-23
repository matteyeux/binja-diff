# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""QBinDiff loader backend backed directly by a Binary Ninja ``BinaryView``.

This lets QBinDiff diff two open BinaryViews with no intermediate export file
and without pulling in Quokka, BinExport or IDA. The five classes below
implement the interfaces declared in ``qbindiff.loader.backend.abstract`` and
are consumed through ``Program.from_backend()``.
"""

from __future__ import annotations

#: QBinDiff's abstract backends require a property literally named ``bytes``,
#: which shadows the builtin for the rest of that class body — including the
#: return annotation sitting next to it.
import builtins
import weakref
from collections import defaultdict
import zlib
from functools import cached_property
from typing import TYPE_CHECKING
from collections.abc import Iterator

import networkx

from binaryninja import BinaryView, Function as BNFunction, BasicBlock as BNBasicBlock
from binaryninja.enums import (
    BranchType,
    InstructionTextTokenType,
    SymbolType,
)

from qbindiff.features.topology import ImpName
from qbindiff.loader import Data, Structure
from qbindiff.loader.backend import (
    AbstractBasicBlockBackend,
    AbstractFunctionBackend,
    AbstractInstructionBackend,
    AbstractOperandBackend,
    AbstractProgramBackend,
)
from qbindiff.loader.types import (
    DataType,
    FunctionType,
    InstructionGroup,
    OperandType,
    ProgramCapability,
    ReferenceType,
    StructureType,
)

if TYPE_CHECKING:
    from qbindiff.loader.types import ReferenceTarget
    from qbindiff.types import Addr


#: Token types that carry an operand value we can classify.
_TOKEN_TO_OPERAND_TYPE = {
    InstructionTextTokenType.RegisterToken: OperandType.register,
    InstructionTextTokenType.IntegerToken: OperandType.immediate,
    InstructionTextTokenType.FloatingPointToken: OperandType.float_point,
    InstructionTextTokenType.PossibleAddressToken: OperandType.memory,
    InstructionTextTokenType.CodeRelativeAddressToken: OperandType.memory,
    InstructionTextTokenType.CodeSymbolToken: OperandType.memory,
    InstructionTextTokenType.DataSymbolToken: OperandType.memory,
    InstructionTextTokenType.ExternalSymbolToken: OperandType.memory,
    InstructionTextTokenType.ImportToken: OperandType.memory,
    InstructionTextTokenType.LocalVariableToken: OperandType.memory,
    InstructionTextTokenType.StackVariableToken: OperandType.memory,
}

_BRANCH_TO_GROUP = {
    BranchType.UnconditionalBranch: InstructionGroup.GRP_JUMP,
    BranchType.TrueBranch: InstructionGroup.GRP_JUMP,
    BranchType.FalseBranch: InstructionGroup.GRP_JUMP,
    BranchType.IndirectBranch: InstructionGroup.GRP_JUMP,
    BranchType.CallDestination: InstructionGroup.GRP_CALL,
    BranchType.FunctionReturn: InstructionGroup.GRP_RET,
    BranchType.SystemCall: InstructionGroup.GRP_INT,
}

#: Branch types whose destination is encoded as a PC-relative displacement.
_RELATIVE_BRANCHES = (
    BranchType.UnconditionalBranch,
    BranchType.TrueBranch,
    BranchType.FalseBranch,
    BranchType.CallDestination,
)


def mnemonic_id(mnemonic: str) -> int:
    """Stable instruction ID in ``[0, MAX_ID)`` derived from the mnemonic.

    QBinDiff backends normally forward Capstone instruction IDs. Binary Ninja
    has no equivalent, but the only requirement is that the two programs being
    diffed agree, so a deterministic hash of the mnemonic works. Callers that
    compare IDs across backends would be wrong to do so, which the abstract
    interface already warns about.
    """

    return zlib.crc32(mnemonic.encode("utf-8")) % AbstractInstructionBackend.MAX_ID


def _function_type(func: BNFunction) -> FunctionType:
    symbol = func.symbol
    sym_type = symbol.type if symbol is not None else None

    if sym_type in (SymbolType.ImportedFunctionSymbol, SymbolType.ImportAddressSymbol):
        return FunctionType.imported
    if sym_type == SymbolType.ExternalSymbol:
        return FunctionType.extern
    if sym_type == SymbolType.LibraryFunctionSymbol:
        return FunctionType.library

    # Binary Ninja marks trampolines by resolving them to their target rather
    # than with a symbol type, so detect the shape instead.
    blocks = func.basic_blocks
    if len(blocks) == 0:
        return FunctionType.extern
    if len(blocks) == 1 and len(func.callee_addresses) == 1 and blocks[0].length <= 16:
        return FunctionType.thunk
    return FunctionType.normal


def _data_type_for(length: int, is_ascii: bool) -> DataType:
    if is_ascii:
        return DataType.ASCII
    return {
        1: DataType.BYTE,
        2: DataType.WORD,
        4: DataType.DOUBLE_WORD,
        8: DataType.QUAD_WORD,
        16: DataType.OCTO_WORD,
    }.get(length, DataType.UNKNOWN)


class OperandBackendBinja(AbstractOperandBackend):
    """A single operand, recovered from a disassembly text token."""

    def __init__(self, token):
        self._token = token

    def __str__(self) -> str:
        return self._token.text

    @property
    def value(self) -> int | None:
        if self.is_immediate():
            return self._token.value
        return None

    @property
    def type(self) -> OperandType:
        return _TOKEN_TO_OPERAND_TYPE.get(self._token.type, OperandType.unknown)

    def is_immediate(self) -> bool:
        return self._token.type in (
            InstructionTextTokenType.IntegerToken,
            InstructionTextTokenType.CharacterConstantToken,
        )


class InstructionBackendBinja(AbstractInstructionBackend):
    """A single machine instruction."""

    def __init__(self, program, func: BNFunction, addr: int, tokens, length: int):
        self._program = program
        self._func = func
        self._addr = addr
        self._tokens = tokens
        self._length = length

    @property
    def program(self) -> ProgramBackendBinja:
        if (program := self._program()) is None:
            raise RuntimeError("Expired weak reference on ProgramBackendBinja")
        return program

    @property
    def addr(self) -> Addr:
        return self._addr

    @property
    def mnemonic(self) -> str:
        for token in self._tokens:
            if token.type == InstructionTextTokenType.InstructionToken:
                return token.text.strip()
        return self._tokens[0].text.strip() if self._tokens else ""

    @property
    def operands(self) -> Iterator[OperandBackendBinja]:
        return (
            OperandBackendBinja(token)
            for token in self._tokens
            if token.type in _TOKEN_TO_OPERAND_TYPE
        )

    @cached_property
    def references(self) -> dict[ReferenceType, list[ReferenceTarget]]:
        bv = self.program.bv
        targets: list[ReferenceTarget] = []
        for ref_addr in bv.get_data_refs_from(self._addr):
            var = bv.get_data_var_at(ref_addr)
            string = bv.get_string_at(ref_addr)
            if string is not None:
                targets.append(Data(DataType.ASCII, ref_addr, string.value))
            elif var is not None:
                length = var.type.width if var.type is not None else 0
                targets.append(Data(_data_type_for(length, False), ref_addr, None))
        if not targets:
            return {}
        return {ReferenceType.DATA: targets}

    @property
    def groups(self) -> list[InstructionGroup]:
        info = self.program.instruction_info(self._addr)
        if info is None:
            return []
        groups: list[InstructionGroup] = []
        for branch in info.branches:
            group = _BRANCH_TO_GROUP.get(branch.type)
            if group is not None and group not in groups:
                groups.append(group)
            if (
                branch.type in _RELATIVE_BRANCHES
                and InstructionGroup.GRP_BRANCH_RELATIVE not in groups
            ):
                groups.append(InstructionGroup.GRP_BRANCH_RELATIVE)
        return groups

    @property
    def id(self) -> int:
        return mnemonic_id(self.mnemonic)

    @property
    def comment(self) -> str:
        return self._func.get_comment_at(self._addr) or ""

    @property
    def bytes(self) -> builtins.bytes:
        return self.program.bv.read(self._addr, self._length) or b""


class BasicBlockBackendBinja(AbstractBasicBlockBackend):
    """A basic block of machine instructions."""

    def __init__(self, program, func: BNFunction, block: BNBasicBlock):
        self._program = program
        self._func = func
        self._block = block

    def __len__(self) -> int:
        return self._block.instruction_count

    @property
    def addr(self) -> Addr:
        return self._block.start

    @property
    def instructions(self) -> Iterator[InstructionBackendBinja]:
        addr = self._block.start
        for tokens, length in self._block:
            yield InstructionBackendBinja(self._program, self._func, addr, tokens, length)
            addr += length

    @property
    def bytes(self) -> builtins.bytes:
        program = self._program()
        if program is None:
            return b""
        return program.bv.read(self._block.start, self._block.length) or b""


class FunctionBackendBinja(AbstractFunctionBackend):
    """A function, its CFG and its position in the call graph."""

    def __init__(self, program, func: BNFunction):
        self._program = program
        self._func = func

    @property
    def bn_function(self) -> BNFunction:
        return self._func

    @property
    def basic_blocks(self) -> Iterator[BasicBlockBackendBinja]:
        if self.is_import():
            return iter([])
        return (
            BasicBlockBackendBinja(self._program, self._func, block)
            for block in self._func.basic_blocks
        )

    @property
    def addr(self) -> Addr:
        return self._func.start

    @cached_property
    def graph(self) -> networkx.DiGraph:
        graph = networkx.DiGraph()
        for block in self._func.basic_blocks:
            graph.add_node(block.start)
            for edge in block.outgoing_edges:
                graph.add_edge(block.start, edge.target.start)
        return graph

    @cached_property
    def parents(self) -> set[Addr]:
        return {caller.start for caller in self._func.callers}

    @cached_property
    def children(self) -> set[Addr]:
        """Every call target, including ones that are no function of the program.

        Binary Ninja reports its synthetic ``__builtin_memset`` and friends and
        the GOT slot an import stub jumps through as callees, and a scoped diff
        has callees outside the part being diffed. Keep them: QBinDiff's call
        graph drops those edges by itself, and ChildNb counting them measurably
        helps matching (they are stable across builds). What cannot cope is a
        feature that looks each child up in the program; see ImportCalls.
        """

        return set(self._func.callee_addresses)

    @cached_property
    def type(self) -> FunctionType:
        return _function_type(self._func)

    @property
    def name(self) -> str:
        return self._func.name

    def is_import(self) -> bool:
        return self.type in (FunctionType.imported, FunctionType.extern)


class ProgramBackendBinja(AbstractProgramBackend):
    """QBinDiff program backend over a live ``BinaryView``."""

    def __init__(self, bv: BinaryView, functions: list[BNFunction] | None = None):
        super().__init__()
        self.bv = bv
        #: The functions to expose, defaulting to the whole view. Scoping a
        #: diff to one kext or SEP module happens here: matching is quadratic,
        #: so cutting the input is the only thing that makes a kernelcache
        #: tractable at all (see core/scope.py).
        self._functions = list(bv.functions) if functions is None else list(functions)
        self._callgraph = networkx.DiGraph()
        self._fun_names: dict[str, Addr] = {}
        self._instruction_info: dict[int, object] = {}

    def instruction_info(self, addr: int):
        """Cached ``InstructionInfo`` lookup.

        Several instructions are visited more than once across feature
        extraction and block alignment, and decoding is not free.
        """

        if addr in self._instruction_info:
            return self._instruction_info[addr]
        data = self.bv.read(addr, self.bv.arch.max_instr_length if self.bv.arch else 16)
        info = self.bv.arch.get_instruction_info(data, addr) if data and self.bv.arch else None
        self._instruction_info[addr] = info
        return info

    @property
    def functions(self) -> Iterator[FunctionBackendBinja]:
        functions: dict[Addr, FunctionBackendBinja] = {}
        self_ref = weakref.ref(self)
        for func in self._functions:
            backend = FunctionBackendBinja(self_ref, func)
            addr = backend.addr
            if addr in functions:
                continue
            functions[addr] = backend
            self._fun_names.setdefault(backend.name, addr)

        for addr, backend in functions.items():
            self._callgraph.add_node(addr)
            for child in backend.children:
                self._callgraph.add_edge(addr, child)
            for parent in backend.parents:
                self._callgraph.add_edge(parent, addr)

        return iter(functions.values())

    @property
    def name(self) -> str:
        return self.bv.file.filename

    @cached_property
    def structures(self) -> list[Structure]:
        structures: list[Structure] = []
        for qualified_name, type_obj in self.bv.types.items():
            members = getattr(type_obj, "members", None)
            if members is None:
                continue
            struct = Structure(
                StructureType.UNION
                if type_obj.type_class.name == "UnionTypeClass"
                else StructureType.STRUCT,
                str(qualified_name),
                type_obj.width,
            )
            for member in members:
                struct.add_member(
                    member.offset,
                    _data_type_for(member.type.width, False),
                    member.name,
                    member.type.width,
                    None,
                )
            structures.append(struct)
        return structures

    @cached_property
    def structures_by_name(self) -> dict[str, Structure]:
        return {struct.name: struct for struct in self.structures}

    def get_structure(self, name: str) -> Structure:
        return self.structures_by_name[name]

    @property
    def callgraph(self) -> networkx.DiGraph:
        return self._callgraph

    @property
    def fun_names(self) -> dict[str, Addr]:
        return self._fun_names

    @property
    def exec_path(self) -> str:
        return self.bv.file.original_filename or self.bv.file.filename

    @property
    def export_path(self) -> str:
        return ""

    @property
    def capabilities(self) -> ProgramCapability:
        return ProgramCapability.INSTR_GROUP


class ImportCalls(ImpName):
    """QBinDiff's ImpName, for callees this backend reports that are not functions.

    ImpName looks every child up in the program and raises KeyError on a call
    target that is not one of its functions, which this backend reports on
    purpose (see FunctionBackendBinja.children). Same key, same values, so it
    is a drop-in replacement.
    """

    def visit_function(self, program, function, collector) -> None:
        counts: dict[str, float] = defaultdict(int)
        for addr in function.children:
            if addr in program and program[addr].is_import():
                counts[program[addr].name] += 1
        collector.add_dict_feature(self.key, counts)
