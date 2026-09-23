"""Exercise the QBinDiff backend and alignment logic against real qbindiff.

Run with the project virtualenv:

    .venv-qbindiff/bin/python binja_diff/tests/test_backend.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()

_stubs = _bootstrap.stubs()
BasicBlock = _stubs.BasicBlock
BinaryView = _stubs.BinaryView
BranchType = _stubs.BranchType
Edge = _stubs.Edge
Function = _stubs.Function
TT = _stubs.InstructionTextTokenType
SymbolType = _stubs.SymbolType
Token = _stubs.Token

from binja_diff.core.backend import ProgramBackendBinja, mnemonic_id  # noqa: E402
from binja_diff.core import align  # noqa: E402

from qbindiff.loader import Program  # noqa: E402
from qbindiff.loader.types import FunctionType, ProgramCapability  # noqa: E402


def insn(mnemonic: str, *operands, length: int = 4):
    tokens = [Token(TT.InstructionToken, mnemonic), Token(TT.TextToken, " ")]
    for i, (kind, text, value) in enumerate(operands):
        if i:
            tokens.append(Token(TT.OperandSeparatorToken, ", "))
        tokens.append(Token(kind, text, value))
    return (tokens, length)


def build_view(name: str, *, extra_instruction: bool = False) -> BinaryView:
    bv = BinaryView(name)

    entry_block = BasicBlock(
        0x1000,
        [
            insn("push", (TT.RegisterToken, "rbp", 0)),
            insn("mov", (TT.RegisterToken, "rbp", 0), (TT.RegisterToken, "rsp", 0)),
            insn("cmp", (TT.RegisterToken, "edi", 0), (TT.IntegerToken, "0x2a", 42)),
            insn("jne", (TT.PossibleAddressToken, "0x1030", 0x1030)),
        ],
    )
    then_block = BasicBlock(
        0x1020,
        [insn("mov", (TT.RegisterToken, "eax", 0), (TT.IntegerToken, "0x1", 1))],
    )
    else_instrs = [insn("xor", (TT.RegisterToken, "eax", 0), (TT.RegisterToken, "eax", 0))]
    if extra_instruction:
        else_instrs.append(insn("inc", (TT.RegisterToken, "eax", 0)))
    else_block = BasicBlock(0x1030, else_instrs)
    exit_block = BasicBlock(0x1040, [insn("pop", (TT.RegisterToken, "rbp", 0)), insn("ret")])

    entry_block.outgoing_edges = [
        Edge(BranchType.FalseBranch, then_block),
        Edge(BranchType.TrueBranch, else_block),
    ]
    then_block.outgoing_edges = [Edge(BranchType.UnconditionalBranch, exit_block)]
    else_block.outgoing_edges = [Edge(BranchType.UnconditionalBranch, exit_block)]

    main = Function(bv, 0x1000, "main", [entry_block, then_block, else_block, exit_block])

    helper_block = BasicBlock(
        0x2000,
        [insn("mov", (TT.RegisterToken, "eax", 0), (TT.IntegerToken, "0x7b", 123)), insn("ret")],
    )
    helper = Function(bv, 0x2000, "helper", [helper_block])
    # 0x9000 is what Binary Ninja reports for a synthetic builtin or a GOT slot:
    # a call target that is no function of the program.
    main.callee_addresses = [0x2000, 0x3000, 0x9000]

    imported = Function(bv, 0x3000, "printf", [], sym_type=SymbolType.ImportedFunctionSymbol)

    bv.functions = [main, helper, imported]
    return bv


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def test_backend_shape():
    print("backend shape")
    bv = build_view("primary")
    program = Program.from_backend(ProgramBackendBinja(bv))

    check("three functions loaded", len(program) == 3, f"got {len(program)}")
    # Program.__iter__ yields Function objects, not keys, so go through items().
    addrs = {addr for addr, _func in program.items()}
    check("keyed by virtual address", addrs == {0x1000, 0x2000, 0x3000}, f"got {addrs}")

    main = program[0x1000]
    check("function name", main.name == "main")
    check("function type normal", main.type == FunctionType.normal)
    check("four basic blocks", len(main) == 4, f"got {len(main)}")
    check("cfg edge count", len(main.flowgraph.edges) == 4, f"got {len(main.flowgraph.edges)}")
    check("children", main.children == {0x2000, 0x3000, 0x9000}, f"got {main.children}")

    helper = program[0x2000]
    check("parents", helper.parents == {0x1000}, f"got {helper.parents}")

    imported = program[0x3000]
    check("import detected", imported.is_import())
    check("import has no blocks", len(imported) == 0)

    check("callgraph edge", (0x1000, 0x2000) in program.callgraph.edges)
    check("capabilities", program.capabilities == ProgramCapability.INSTR_GROUP)
    check("fun_names", program.get_function("helper").addr == 0x2000)


def test_instruction_level():
    print("instruction and operand level")
    bv = build_view("primary")
    program = Program.from_backend(ProgramBackendBinja(bv))
    main = program[0x1000]

    with main:
        entry = main[0x1000]
        instructions = list(entry)
        check("instruction count", len(instructions) == 4, f"got {len(instructions)}")

        mnemonics = [i.mnemonic for i in instructions]
        check("mnemonics", mnemonics == ["push", "mov", "cmp", "jne"], f"got {mnemonics}")

        addrs = [i.addr for i in instructions]
        check("addresses advance by length", addrs == [0x1000, 0x1004, 0x1008, 0x100C], f"{addrs}")

        cmp_insn = instructions[2]
        operands = list(cmp_insn.operands)
        check("cmp has two operands", len(operands) == 2, f"got {len(operands)}")
        check("register operand type", operands[0].type.name == "register")
        check("immediate operand type", operands[1].type.name == "immediate")
        check("immediate is_immediate", operands[1].is_immediate())
        check("immediate value", operands[1].value == 42, f"got {operands[1].value}")
        check("register not immediate", not operands[0].is_immediate())

        check("id in range", 0 <= cmp_insn.id < 3000)
        check("id deterministic", cmp_insn.id == mnemonic_id("cmp"))
        check("bytes length", len(cmp_insn.bytes) == 4)
        check("block bytes", len(entry.bytes) == 16, f"got {len(entry.bytes)}")


def test_full_diff():
    print("end-to-end diff")
    from qbindiff import Distance, QBinDiff
    from qbindiff.features import DEFAULT_FEATURES

    primary = Program.from_backend(ProgramBackendBinja(build_view("primary")))
    secondary = Program.from_backend(
        ProgramBackendBinja(build_view("secondary", extra_instruction=True))
    )

    differ = QBinDiff(primary, secondary, distance=Distance.haussmann, sparsity_ratio=0.0)
    for feature in DEFAULT_FEATURES:
        differ.register_feature_extractor(feature, 1.0)

    steps = list(differ.process_iterator())
    check("phase 1 yielded progress", len(steps) > 0)
    check("phase 1 monotonic", steps == sorted(steps), f"{steps[:8]}")

    iterations = list(differ.matching_iterator())
    check("phase 2 yielded progress", len(iterations) > 0)

    mapping = differ.mapping
    check("mapping produced", mapping is not None)
    check("matches found", mapping.nb_match > 0, f"got {mapping.nb_match}")
    matched = {(m.primary.addr, m.secondary.addr) for m in mapping}
    check("main matched to main", (0x1000, 0x1000) in matched, f"got {sorted(matched)}")
    check("helper matched", (0x2000, 0x2000) in matched, f"got {sorted(matched)}")
    check("similarity in range", 0.0 <= mapping.normalized_similarity <= 1.0)


def test_import_calls_feature():
    """ImportCalls counts calls to imports and skips targets that are no function.

    QBinDiff's own ImpName raises KeyError on the second kind, which Binary Ninja
    reports for synthetic builtins and GOT slots.
    """

    print("import calls feature")
    from qbindiff.features.extractor import FeatureCollector
    from qbindiff.features.topology import ImpName

    from binja_diff.core.backend import ImportCalls

    program = Program.from_backend(ProgramBackendBinja(build_view("primary")))
    main = program[0x1000]

    try:
        ImpName(1.0).visit_function(program, main, FeatureCollector())
        check("QBinDiff's ImpName trips on it (else ImportCalls is redundant)", False)
    except KeyError:
        check("QBinDiff's ImpName trips on it (else ImportCalls is redundant)", True)

    collector = FeatureCollector()
    ImportCalls(1.0).visit_function(program, main, collector)
    check("same key as ImpName", ImportCalls.key == ImpName.key == "imp")
    check(
        "the import is counted",
        dict(collector.get("imp") or {}) == {"printf": 1},
        f"got {collector.get('imp')}",
    )


def test_instr_group_features():
    print("INSTR_GROUP capability features")
    from qbindiff import QBinDiff
    from qbindiff.features import GroupsCategory, JumpNb

    primary = Program.from_backend(ProgramBackendBinja(build_view("primary")))
    secondary = Program.from_backend(ProgramBackendBinja(build_view("secondary")))
    differ = QBinDiff(primary, secondary)
    try:
        differ.register_feature_extractor(JumpNb, 1.0)
        differ.register_feature_extractor(GroupsCategory, 1.0)
        differ.process()
        check("INSTR_GROUP features accepted and ran", True)
    except Exception as exc:
        check("INSTR_GROUP features accepted and ran", False, repr(exc))


def build_named_view(name: str, names_by_addr: dict[int, str]) -> BinaryView:
    """A view whose functions all share one body, so only names can tell them
    apart. Imports get no blocks, mirroring the real backend's view of them."""

    bv = BinaryView(name)
    for addr, func_name in names_by_addr.items():
        if func_name == "printf":
            bv.functions.append(
                Function(bv, addr, func_name, [], sym_type=SymbolType.ImportedFunctionSymbol)
            )
            continue
        block = BasicBlock(
            addr,
            [insn("mov", (TT.RegisterToken, "eax", 0), (TT.IntegerToken, "0x1", 1)), insn("ret")],
        )
        bv.functions.append(Function(bv, addr, func_name, [block]))
    return bv


def test_name_anchor_pass():
    print("name-anchor prepass")
    import numpy as np

    from binja_diff.core import engine

    layout = {
        0x1000: "alpha",
        0x2000: "dup",
        0x3000: "dup",
        0x4000: "sub_4000",
        0x5000: "printf",
    }
    primary = Program.from_backend(ProgramBackendBinja(build_named_view("primary", layout)))
    secondary = Program.from_backend(ProgramBackendBinja(build_named_view("secondary", layout)))

    p_map = {addr: i for i, (addr, _) in enumerate(primary.items())}
    s_map = {addr: i for i, (addr, _) in enumerate(secondary.items())}
    sim = np.full((len(p_map), len(s_map)), -1, dtype=np.float32)

    engine.match_named_functions(sim, primary, secondary, p_map, s_map)

    alpha_row = sim[p_map[0x1000]]
    check("alpha anchored", alpha_row[s_map[0x1000]] == 1, f"got {alpha_row}")
    check("alpha row zeroed elsewhere", sum(alpha_row) == 1, f"got {alpha_row}")
    check("alpha column zeroed", sum(sim[:, s_map[0x1000]]) == 1, f"got {sim[:, s_map[0x1000]]}")
    for label, addr in (("duplicate name", 0x2000), ("auto name", 0x4000), ("import", 0x5000)):
        check(f"{label} not anchored", 1 not in sim[p_map[addr]], f"got {sim[p_map[addr]]}")
        check(f"{label} row left for features", -1 in sim[p_map[addr]], f"got {sim[p_map[addr]]}")


def test_name_anchor_end_to_end():
    print("name anchors override address order in run_diff")
    from binja_diff.core import engine

    # Identical bodies, names swapped between addresses: the features cannot
    # tell the two apart, only the anchor pass knows which is which.
    primary_bv = build_named_view("primary", {0x1000: "encrypt", 0x2000: "decrypt"})
    secondary_bv = build_named_view("secondary", {0x1000: "decrypt", 0x2000: "encrypt"})

    result = engine.run_diff(primary_bv, secondary_bv)
    check("diff completed", result is not None)
    if result is None:
        return
    matched = {(m.primary.addr, m.secondary.addr) for m in result.matches}
    check("encrypt follows its name", (0x1000, 0x2000) in matched, f"got {sorted(matched)}")
    check("decrypt follows its name", (0x2000, 0x1000) in matched, f"got {sorted(matched)}")

    # Timings come from the real phases, so only a real run can prove they are
    # all reached -- an early return would silently drop the later ones.
    phases = [label for label, _seconds in result.timings]
    check(
        "every phase timed",
        phases
        == [
            "Waiting for analysis",
            "Building the primary graph",
            "Building the secondary graph",
            "Extracting features",
            "Matching functions",
            "Scoring matches",
        ],
        f"got {phases}",
    )
    check("total is positive", result.duration > 0, f"got {result.duration}")


def test_line_alignment():
    print("line alignment")
    left = ["mov eax, 1", "call sub_401000", "ret"]
    right = ["mov eax, 1", "nop", "call sub_502000", "ret"]

    rows = align.align_lines(left, right)
    statuses = [r.status.value for r in rows]
    check("every left line placed", sum(1 for r in rows if r.left is not None) == 3)
    check("every right line placed", sum(1 for r in rows if r.right is not None) == 4)
    check("first line equal", statuses[0] == "equal", f"got {statuses}")
    check("last line equal", statuses[-1] == "equal", f"got {statuses}")

    # sub_401000 vs sub_502000 normalize to the same token, so the calls match.
    check(
        "auto-names normalized away",
        align.normalize_line("call sub_401000") == align.normalize_line("call sub_502000"),
    )
    check("hex normalized", align.normalize_line("mov eax, 0x1f") == "mov eax, 0x?")

    identical = align.align_lines(left, left)
    check("identical input is all equal", all(r.status.value == "equal" for r in identical))

    counts = align.summarize(rows)
    check("summary totals", sum(counts.values()) == len(rows))


def test_block_alignment():
    print("block alignment")
    left_bv = build_view("primary")
    right_bv = build_view("secondary", extra_instruction=True)
    left_main = left_bv.functions[0]
    right_main = right_bv.functions[0]

    alignment = align.align_blocks(left_main, right_main, "Disassembly")
    check("all left blocks classified", len(alignment.left_status) == 4)
    check("all right blocks classified", len(alignment.right_status) == 4)

    identical = sum(1 for s in alignment.left_status.values() if s.value == "identical")
    check("three blocks identical", identical == 3, f"got {identical}")
    check(
        "modified block flagged changed",
        alignment.left_status.get(0x1030) is not None
        and alignment.left_status[0x1030].value == "changed",
        f"got {alignment.left_status}",
    )
    check(
        "mapping is 1:1", len(set(alignment.left_to_right.values())) == len(alignment.left_to_right)
    )

    same = align.align_blocks(left_main, left_bv.functions[0], "Disassembly")
    check(
        "self-alignment all identical",
        all(s.value == "identical" for s in same.left_status.values()),
    )


def main() -> int:
    for test in (
        test_backend_shape,
        test_instruction_level,
        test_full_diff,
        test_import_calls_feature,
        test_instr_group_features,
        test_name_anchor_pass,
        test_name_anchor_end_to_end,
        test_line_alignment,
        test_block_alignment,
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
