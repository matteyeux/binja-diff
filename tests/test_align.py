"""Edge-case coverage for the alignment logic.

Run with the project virtualenv:

    .venv-qbindiff/bin/python binja_diff/tests/test_align.py
"""

from __future__ import annotations

from typing import ClassVar

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
Token = _stubs.Token

from binja_diff.core import align  # noqa: E402
from binja_diff.core.align import LineStatus  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def insn(text: str):
    return ([Token(TT.InstructionToken, text)], 4)


def func_with_blocks(name: str, blocks_spec: dict[int, list[str]], edges=()) -> Function:
    bv = BinaryView(name)
    blocks = {
        addr: BasicBlock(addr, [insn(t) for t in texts]) for addr, texts in blocks_spec.items()
    }
    for src, dst in edges:
        blocks[src].outgoing_edges.append(Edge(BranchType.UnconditionalBranch, blocks[dst]))
    func = Function(bv, min(blocks_spec), name, list(blocks.values()))
    bv.functions = [func]
    return func


def test_empty_inputs():
    print("empty and degenerate inputs")
    check("both empty", align.align_lines([], []) == [])

    only_left = align.align_lines(["a", "b"], [])
    check("all removed", [r.status for r in only_left] == [LineStatus.REMOVED] * 2)
    check("no right side", all(r.right is None for r in only_left))

    only_right = align.align_lines([], ["a", "b"])
    check("all added", [r.status for r in only_right] == [LineStatus.ADDED] * 2)

    empty_a = func_with_blocks("a", {0x10: []})
    empty_b = func_with_blocks("b", {0x10: []})
    alignment = align.align_blocks(empty_a, empty_b, "Disassembly")
    check("empty blocks still align", len(alignment.left_status) == 1)

    no_blocks = Function(BinaryView("x"), 0x10, "stub", [])
    result = align.align_blocks(no_blocks, no_blocks, "Disassembly")
    check("function with no blocks", result.pairs == [])


def test_row_alignment_invariants():
    print("row alignment invariants")
    left = [f"line {i}" for i in range(20)]
    right = [f"line {i}" for i in range(0, 20, 2)] + ["tail"]

    rows = align.align_lines(left, right)
    check("all left preserved", [r.left for r in rows if r.left is not None] == left)
    check("all right preserved", [r.right for r in rows if r.right is not None] == right)
    check("no empty rows", all(r.left is not None or r.right is not None for r in rows))
    check("row count >= max input", len(rows) >= max(len(left), len(right)))

    counts = align.summarize(rows)
    check("summary counts every row", sum(counts.values()) == len(rows))


def test_normalization():
    print("normalization")
    cases = [
        ("mov eax, 0xdeadbeef", "mov eax, 0x?"),
        # An auto-generated location name IS an address, so it is rewritten to
        # one and then folded like any other.
        ("call sub_401000", "call 0x?"),
        ("lea rax, [rbp-0x8]", "lea rax, [rbp-0x?]"),
        ("mov   eax,   ebx", "mov eax, ebx"),
        ("  leading and trailing  ", "leading and trailing"),
    ]
    for raw, expected in cases:
        actual = align.normalize_line(raw)
        check(f"normalize {raw!r}", actual == expected, f"got {actual!r}")

    check(
        "different auto-names collapse",
        align.normalize_line("jmp jump_table_1000") == align.normalize_line("jmp jump_table_2000"),
    )
    check(
        "real names preserved",
        align.normalize_line("call memcpy") != align.normalize_line("call malloc"),
    )

    # compare_line must keep literals, or changed constants become invisible.
    check(
        "compare_line keeps immediates distinct",
        align.compare_line("mov eax, 0x1") != align.compare_line("mov eax, 0x2"),
    )
    # compare_line decides only that a row is *unchanged*, so it folds nothing
    # but whitespace: a callee that moved is visible on screen and is graded
    # `~` by normalize_line, not declared equal.
    check(
        "compare_line keeps moved callees distinct",
        align.compare_line("call sub_401000") != align.compare_line("call sub_502000"),
    )
    check(
        "normalize_line is what folds them",
        align.normalize_line("call sub_401000") == align.normalize_line("call sub_502000"),
    )
    check(
        "compare_line collapses whitespace",
        align.compare_line("mov   eax,  ebx") == "mov eax, ebx",
    )

    # One view resolves a reference to a symbol, the other prints the number.
    check(
        "a name and a raw address are the same thing",
        align.normalize_line("adr x8, 0x10001a9c8")
        == align.normalize_line("adr x8, sub_10001aa08"),
    )
    # Stack slots are not locations: var_8 and var_c are different storage.
    check(
        "stack slots keep their own placeholder",
        align.normalize_line("ldr x8, [sp, var_8]") != align.normalize_line("ldr x8, [sp, 0x8]"),
    )


def test_change_visibility():
    """The regression this module exists for: changed literals must be visible."""

    print("changed lines are classified, not hidden")

    rows = align.align_lines(["mov eax, 0x1"], ["mov eax, 0x2"])
    check("single row", len(rows) == 1, f"got {len(rows)}")
    check(
        "changed immediate is not EQUAL",
        rows[0].status is not align.LineStatus.EQUAL,
        f"got {rows[0].status}",
    )
    check("changed immediate is a difference", rows[0].status.is_difference)
    check("classified MINOR", rows[0].status is align.LineStatus.MINOR, f"got {rows[0].status}")

    # A different instruction is a stronger signal than a different literal.
    rows = align.align_lines(["mov eax, ebx"], ["xor eax, eax"])
    check(
        "different mnemonic is CHANGED",
        rows[0].status is align.LineStatus.CHANGED,
        f"{rows[0].status}",
    )

    # One side resolved the target to a symbol, the other printed the address.
    rows = align.align_lines(["adr x8, 0x10001a9c8"], ["adr x8, sub_10001aa08"])
    check(
        "a name against an address is MINOR",
        rows[0].status is align.LineStatus.MINOR,
        f"{rows[0].status}",
    )

    # Binary Ninja's trailing hints belong to the renderer, not the instruction.
    rows = align.align_lines(
        ["stp xzr, x8, [x0, #0x8]"],
        ["stp xzr, x8, [x0, #0x8]  {0x0}  {sub_10001aa08}"],
    )
    check(
        "trailing hints are ignored", rows[0].status is align.LineStatus.EQUAL, f"{rows[0].status}"
    )

    # Symbol recovery differs between two views of the same bytes: one renders
    # `bl _DERDecodeSeqContentInit`, the other `bl 0x100013d54`. An address is
    # not a competing name, it is the absence of one. The token types below are
    # the ones Binary Ninja really emits for those two forms.
    def call(kind: str, text: str):
        class Line:
            tokens: ClassVar = [
                Token(TT.InstructionToken, "bl"),
                Token(TT.TextToken, "      "),
                Token(getattr(TT, kind), text),
            ]

            def __str__(self):
                return "".join(token.text for token in self.tokens)

        return Line()

    unresolved = call("PossibleAddressToken", "0x100013d54")
    resolved = call("CodeSymbolToken", "_DERDecodeSeqContentInit")
    rows = align.align_lines([unresolved], [resolved])
    check(
        "an unresolved address against a name is MINOR",
        rows[0].status is align.LineStatus.MINOR,
        f"{rows[0].status}",
    )
    # Both sides know what they call, and disagree: a real change.
    rows = align.align_lines([call("CodeSymbolToken", "memcpy")], [resolved])
    check(
        "two real names still differ",
        rows[0].status is align.LineStatus.CHANGED,
        f"{rows[0].status}",
    )
    # A register replaced by a constant is not symbol resolution either.
    check(
        "a register against a constant is not symbol resolution",
        not align.resolved_symbol_only(call("RegisterToken", "ebx"), call("IntegerToken", "0x1")),
    )

    # A callee that moved is noise, but visible noise: the two lines differ on
    # screen, so the row is marked `~` rather than silently called equal.
    rows = align.align_lines(["call sub_401000"], ["call sub_502000"])
    check("a moved callee is MINOR", rows[0].status is align.LineStatus.MINOR, f"{rows[0].status}")
    # But a genuinely different callee is a real change.
    rows = align.align_lines(["call memcpy"], ["call malloc"])
    check(
        "a different callee is CHANGED",
        rows[0].status is align.LineStatus.CHANGED,
        f"{rows[0].status}",
    )

    # Identical input must produce no differences at all.
    same = ["push rbp", "mov rbp, rsp", "mov eax, 0x2a", "pop rbp", "ret"]
    rows = align.align_lines(same, same)
    check("identical input has no differences", not any(r.status.is_difference for r in rows))

    # A realistic mixed function: one constant changed, one line inserted.
    left = ["push rbp", "mov rbp, rsp", "cmp edi, 0x2a", "jne 0x40", "pop rbp", "ret"]
    right = ["push rbp", "mov rbp, rsp", "cmp edi, 0x2b", "jne 0x40", "nop", "pop rbp", "ret"]
    rows = align.align_lines(left, right)
    statuses = [r.status for r in rows]
    check(
        "constant change detected",
        any(s is align.LineStatus.MINOR for s in statuses),
        f"got {[s.value for s in statuses]}",
    )
    check(
        "insertion detected",
        any(s is align.LineStatus.ADDED for s in statuses),
        f"got {[s.value for s in statuses]}",
    )
    differing = [s for s in statuses if s.is_difference]
    check("exactly two differences", len(differing) == 2, f"got {[s.value for s in differing]}")


def test_shape_signature():
    """Register reallocation must not read as a rewrite.

    Adding one local makes a compiler renumber registers through a whole loop.
    Grading each of those lines CHANGED buries the one real change among a dozen
    false ones, which is what the graph view was doing.
    """

    print("shape signature grades register churn as MINOR")

    from binja_diff.tests.stub_binaryninja import InstructionTextTokenType as T
    from binja_diff.tests.stub_binaryninja import Token

    class TokenLine:
        """Stands in for DisassemblyTextLine: tokens plus a text rendering."""

        def __init__(self, tokens):
            self.tokens = tokens

        def __str__(self) -> str:
            return "".join(token.text for token in self.tokens)

    def instr(mnemonic, *operands):
        tokens = [Token(T.InstructionToken, mnemonic), Token(T.TextToken, " ")]
        for index, (kind, text) in enumerate(operands):
            if index:
                tokens.append(Token(T.OperandSeparatorToken, ", "))
            tokens.append(Token(kind, text))
        return TokenLine(tokens)

    reg = lambda name: (T.RegisterToken, name)  # noqa: E731
    num = lambda text: (T.IntegerToken, text)  # noqa: E731

    # Same operation, different registers: the compiler's choice, not a change.
    rows = align.align_lines(
        [instr("add", reg("eax"), reg("ecx"))],
        [instr("add", reg("edx"), reg("r8d"))],
    )
    check("register swap is MINOR", rows[0].status is align.LineStatus.MINOR, f"{rows[0].status}")

    # A system/CSR register names architectural state, not compiler allocation.
    rows = align.align_lines(
        [instr("msr", reg("apdbkeylo_el2"), reg("x1"))],
        [instr("msr", reg("apibkeylo_el1"), reg("x1"))],
    )
    check("different system registers are CHANGED", rows[0].status is align.LineStatus.CHANGED)
    rows = align.align_lines(
        [instr("mrs", reg("x0"), reg("sctlr_el1"))],
        [instr("mrs", reg("x0"), reg("sctlr_el2"))],
    )
    check("different control registers are CHANGED", rows[0].status is align.LineStatus.CHANGED)
    check("weak matching and weak code evidence need review", align.pairing_needs_review(0.0, 0.2))
    check("substantial shared code avoids review", not align.pairing_needs_review(0.0, 0.6))

    # Same operation, different immediate.
    rows = align.align_lines(
        [instr("cmp", reg("eax"), num("0x64"))],
        [instr("cmp", reg("edx"), num("0x64"))],
    )
    check("register+immediate is MINOR", rows[0].status is align.LineStatus.MINOR)

    # Different mnemonic is a real change even with the same operand shape.
    rows = align.align_lines(
        [instr("sub", reg("edx"), reg("eax"))],
        [instr("add", reg("edx"), reg("eax"))],
    )
    check(
        "different mnemonic stays CHANGED",
        rows[0].status is align.LineStatus.CHANGED,
        f"{rows[0].status}",
    )

    # Different operand *kinds* is a real change: register vs immediate source.
    rows = align.align_lines(
        [instr("mov", reg("eax"), reg("ebx"))],
        [instr("mov", reg("eax"), num("0x1"))],
    )
    check(
        "operand kind change stays CHANGED",
        rows[0].status is align.LineStatus.CHANGED,
        f"{rows[0].status}",
    )

    # Identical lines are still EQUAL, not MINOR.
    same = instr("add", reg("eax"), reg("ecx"))
    rows = align.align_lines([same], [instr("add", reg("eax"), reg("ecx"))])
    check("identical stays EQUAL", rows[0].status is align.LineStatus.EQUAL, f"{rows[0].status}")

    # Plain strings carry no tokens and must skip the tier entirely.
    check("no tokens yields no signature", align.shape_signature("mov eax, ebx") is None)
    rows = align.align_lines(["mov eax, ebx"], ["mov edx, ecx"])
    check(
        "untokenized register swap is still CHANGED",
        rows[0].status is align.LineStatus.CHANGED,
        f"{rows[0].status}",
    )


def test_side_statuses():
    """Graph nodes are tinted by zipping these onto a node's own lines.

    If either list drifts out of step with its side's line count, the wrong
    instructions get highlighted, so the one-to-one property is the whole point.
    """

    print("per-side statuses line up with each side's lines")

    left = ["push rbp", "mov eax, 0x1", "pop rbp", "ret"]
    right = ["push rbp", "mov eax, 0x2", "nop", "pop rbp", "ret"]
    rows = align.align_lines(left, right)
    left_status, right_status = align.side_statuses(rows)

    check("left length matches", len(left_status) == len(left), f"got {len(left_status)}")
    check("right length matches", len(right_status) == len(right), f"got {len(right_status)}")
    check("left has no gaps", align.LineStatus.GAP not in left_status)
    check("right has no gaps", align.LineStatus.GAP not in right_status)

    # The changed constant is on line 1 of both sides; the insertion only right.
    check("left constant flagged", left_status[1].is_difference, f"got {left_status[1]}")
    check("right constant flagged", right_status[1].is_difference, f"got {right_status[1]}")
    check("inserted nop flagged", right_status[2] is align.LineStatus.ADDED, f"{right_status[2]}")
    check("left prologue untouched", not left_status[0].is_difference)
    check("left epilogue untouched", not any(s.is_difference for s in left_status[2:]))

    # Identical blocks must yield nothing to highlight at all.
    same = ["test eax, eax", "je 0x10"]
    left_status, right_status = align.side_statuses(align.align_lines(same, same))
    check("identical yields no highlights", not any(s.is_difference for s in left_status))
    check("identical lengths preserved", len(left_status) == len(right_status) == len(same))

    # Empty on one side: every line belongs to the other.
    left_status, right_status = align.side_statuses(align.align_lines(same, []))
    check("all left removed", all(s is align.LineStatus.REMOVED for s in left_status))
    check("no right statuses", right_status == [])


def test_markers():
    print("gutter markers")
    seen = {status: status.marker for status in align.LineStatus}
    check("equal has no marker", seen[align.LineStatus.EQUAL] == " ")
    check("added marker", seen[align.LineStatus.ADDED] == "+")
    check("removed marker", seen[align.LineStatus.REMOVED] == "-")
    check("changed marker", seen[align.LineStatus.CHANGED] == "!")
    check("minor marker", seen[align.LineStatus.MINOR] == "~")
    check(
        "markers are single width",
        all(len(m) == 1 for m in seen.values()),
        f"got {seen}",
    )
    check(
        "difference flag matches markers",
        {s for s in align.LineStatus if s.is_difference}
        == {
            align.LineStatus.MINOR,
            align.LineStatus.CHANGED,
            align.LineStatus.ADDED,
            align.LineStatus.REMOVED,
        },
    )


def test_block_alignment_cases():
    print("block alignment cases")

    # Identical functions: everything identical, nothing unmatched.
    spec = {0x10: ["push rbp", "mov rbp, rsp"], 0x20: ["pop rbp", "ret"]}
    a = func_with_blocks("a", spec, edges=[(0x10, 0x20)])
    b = func_with_blocks("b", spec, edges=[(0x10, 0x20)])
    result = align.align_blocks(a, b, "Disassembly")
    check("identical functions fully matched", len(result.left_to_right) == 2)
    check("all identical", all(s.value == "identical" for s in result.left_status.values()))

    # Extra block on one side must be reported as unmatched.
    bigger = func_with_blocks(
        "bigger",
        {0x10: ["push rbp", "mov rbp, rsp"], 0x20: ["pop rbp", "ret"], 0x30: ["nop", "nop", "nop"]},
        edges=[(0x10, 0x20)],
    )
    result = align.align_blocks(a, bigger, "Disassembly")
    unmatched = [addr for addr, s in result.right_status.items() if s.value == "unmatched"]
    check("extra block unmatched", unmatched == [0x30], f"got {unmatched}")

    # Ambiguous duplicate blocks must not be anchored arbitrarily.
    dup = func_with_blocks("dup", {0x10: ["ret"], 0x20: ["ret"]})
    dup2 = func_with_blocks("dup2", {0x10: ["ret"], 0x20: ["ret"]})
    result = align.align_blocks(dup, dup2, "Disassembly")
    check(
        "duplicate blocks still 1:1",
        len(set(result.left_to_right.values())) == len(result.left_to_right),
    )
    check("duplicate blocks all classified", len(result.left_status) == 2)

    # Text must outrank topology when pairing leftover blocks. A CFG this small
    # is nearly symmetric, so a purely structural matcher happily pairs the loop
    # body with the epilogue; every line of both then reads as a difference,
    # which is how a two-line change turned into a fully repainted function.
    loop_body = ["add eax, ecx", "cmp eax, 0x64", "sub edx, 0x1", "jne 0x20"]
    epilogue = ["add eax, esi", "ret"]
    left = func_with_blocks(
        "left",
        {0x10: ["push rbp"], 0x20: loop_body, 0x30: epilogue},
        edges=[(0x10, 0x20), (0x20, 0x20), (0x20, 0x30)],
    )
    # Same shape, but both leftover blocks are perturbed so neither anchors
    # exactly and the structural path would otherwise decide.
    right = func_with_blocks(
        "right",
        {
            0x10: ["push rbp"],
            0x20: ["add eax, ecx", "cmp eax, 0x64", "sub edx, 0x1", "nop", "jne 0x20"],
            0x30: ["add eax, edi", "ret"],
        },
        edges=[(0x10, 0x20), (0x20, 0x20), (0x20, 0x30)],
    )
    result = align.align_blocks(left, right, "Disassembly")
    check(
        "loop body pairs with loop body",
        result.left_to_right.get(0x20) == 0x20,
        f"got {result.left_to_right}",
    )
    check(
        "epilogue pairs with epilogue",
        result.left_to_right.get(0x30) == 0x30,
        f"got {result.left_to_right}",
    )

    # A changed block should be matched but flagged, not dropped.
    changed = func_with_blocks(
        "changed",
        {0x10: ["push rbp", "mov rbp, rsp"], 0x20: ["pop rbp", "xor eax, eax", "ret"]},
        edges=[(0x10, 0x20)],
    )
    result = align.align_blocks(a, changed, "Disassembly")
    check("changed block matched", 0x20 in result.left_to_right)
    check(
        "changed block flagged",
        result.left_status[0x20].value == "changed",
        f"got {result.left_status[0x20].value}",
    )


def test_disjoint_functions():
    print("completely different functions")
    a = func_with_blocks("a", {0x10: ["push rbp", "call foo", "ret"]})
    b = func_with_blocks("b", {0x100: ["fmul xmm0, xmm1", "pxor xmm2, xmm2"]})
    result = align.align_blocks(a, b, "Disassembly")
    check("both sides classified", len(result.left_status) == 1 and len(result.right_status) == 1)
    check(
        "nothing marked identical", all(s.value != "identical" for s in result.left_status.values())
    )


def test_function_classification():
    """What the match table says a pair is, from the same rows the panes show.

    The point is that "identical" stops meaning "QBinDiff scored it 1.0" — two
    functions can score that while every address in them differs.
    """

    print("function-level classification")
    body = ["push rbp", "mov rbp, rsp", "pop rbp", "ret"]

    check(
        "same text is identical",
        align.classify_rows(align.align_lines(body, body)) is align.FunctionStatus.IDENTICAL,
    )

    # Rebased: same code, every address moved. This is the case that used to
    # read as "identical" in the table while the pane marked the lines '~'.
    rows = align.align_lines(
        ["mov eax, [0x401000]", "call sub_401100", "ret"],
        ["mov eax, [0x501000]", "call sub_501100", "ret"],
    )
    check(
        "moved addresses are offsets only",
        align.classify_rows(rows) is align.FunctionStatus.MINOR,
        f"got {align.classify_rows(rows)}",
    )

    # A changed constant is a real change, not an offset.
    rows = align.align_lines(["cmp edi, 0x2a", "ret"], ["cmp edi, 0x63", "ret"])
    check(
        "a changed immediate still counts as a difference",
        align.classify_rows(rows) is not align.FunctionStatus.IDENTICAL,
        f"got {align.classify_rows(rows)}",
    )

    rows = align.align_lines(["mov eax, ebx", "ret"], ["xor eax, eax", "ret"])
    check(
        "a different instruction is changed",
        align.classify_rows(rows) is align.FunctionStatus.CHANGED,
        f"got {align.classify_rows(rows)}",
    )

    rows = align.align_lines(["push rbp", "ret"], ["push rbp", "nop", "ret"])
    check(
        "an added instruction is changed",
        align.classify_rows(rows) is align.FunctionStatus.CHANGED,
        f"got {align.classify_rows(rows)}",
    )

    check(
        "two empty functions are identical",
        align.classify_rows([]) is align.FunctionStatus.IDENTICAL,
    )

    # The label is what the table prints, so it has to read as a status.
    check("labels", align.FunctionStatus.MINOR.value == "offsets only")


def test_il_is_generated_before_rendering():
    """IL has to exist before the linear view is asked to render it.

    Otherwise it answers with a "Loading..." placeholder and fills the real
    text in asynchronously — which a QTextEdit rendered once never sees. The
    symptom was having to visit the Basic Blocks tab and come back, because
    that path reads func.llil and generates it as a side effect.
    """

    print("IL is generated before it is rendered")

    class FakeIL:
        basic_blocks = ()

    class FakeFunction:
        def __init__(self, available=True):
            self.touched: list[str] = []
            self._available = available
            self.basic_blocks = ()

        def _get(self, name):
            self.touched.append(name)
            if not self._available:
                raise RuntimeError("analysis skipped")
            return FakeIL()

        llil = property(lambda self: self._get("llil"))
        mlil = property(lambda self: self._get("mlil"))
        hlil = property(lambda self: self._get("hlil"))

    for level, attribute in (("LLIL", "llil"), ("MLIL", "mlil"), ("HLIL", "hlil")):
        func = FakeFunction()
        align.ensure_il(func, level)
        check(f"{level} generates {attribute}", func.touched == [attribute], f"got {func.touched}")

    func = FakeFunction()
    check("disassembly needs nothing generated", align.ensure_il(func, "Disassembly") is None)
    check("and touches no IL", func.touched == [], f"got {func.touched}")

    # A function whose analysis was skipped has no IL and never will.
    func = FakeFunction(available=False)
    check("unavailable IL is not an error", align.ensure_il(func, "HLIL") is None)
    check("blocks fall back to empty", align.il_basic_blocks(func, "HLIL") == [])


def test_annotations_are_not_code():
    """`{0x0}` and `{sub_10001aa08}` are the renderer talking, not the code.

    Binary Ninja appends them when it has worked out what a constant evaluates
    to or where a reference points, and whether it has depends on the view: a
    database renders them, a fresh view of the same bytes does not.

    Only the *braces* carry ``AnnotationToken``; the value between them is an
    ordinary integer or symbol token. The token stream below is copied verbatim
    from a real ARM64 `stp`. Dropping annotation-typed tokens alone leaves
    `stp ... [x0, #0x8]0x0sub_10001a9c8` — the symbol glued to the instruction,
    where it no longer starts on a word boundary and no longer folds like the
    address it is. That is why a pair of identical functions read "changed".
    """

    print("annotations are commentary, not instructions")

    def stp(symbol: str):
        spec = [
            ("AddressSeparatorToken", ""),
            ("InstructionToken", "stp"),
            ("TextToken", "     "),
            ("RegisterToken", "xzr"),
            ("OperandSeparatorToken", ", "),
            ("RegisterToken", "x8"),
            ("OperandSeparatorToken", ", "),
            ("BraceToken", "["),
            ("BeginMemoryOperandToken", ""),
            ("RegisterToken", "x0"),
            ("TextToken", ", "),
            ("OperationToken", "#"),
            ("IntegerToken", "0x8"),
            ("EndMemoryOperandToken", ""),
            ("BraceToken", "]"),
            ("AnnotationToken", "  {"),
            ("IntegerToken", "0x0"),
            ("AnnotationToken", "}"),
            ("AnnotationToken", "  {"),
            ("CodeSymbolToken", symbol),
            ("AnnotationToken", "}"),
        ]

        class Line:
            tokens: ClassVar = [Token(getattr(TT, kind), text) for kind, text in spec]

            def __str__(self):
                return "".join(token.text for token in self.tokens)

        return Line()

    left, right = stp("sub_10001a9c8"), stp("sub_10001aa08")
    check(
        "the annotation and its contents both go",
        align.instruction_text(left) == "stp     xzr, x8, [x0, #0x8]",
        repr(align.instruction_text(left)),
    )
    check(
        "so the pair is equal",
        align.align_lines([left], [right])[0].status is align.LineStatus.EQUAL,
        f"{align.align_lines([left], [right])[0].status}",
    )

    # Without tokens there are only the braces to go on, and they are enough.
    rows = align.align_lines(
        ["stp xzr, x8, [x0, #0x8]"],
        ["stp xzr, x8, [x0, #0x8]  {0x0}  {sub_10001aa08}"],
    )
    check("text-only lines strip them too", rows[0].status is align.LineStatus.EQUAL)


def test_annotation_only_lines_are_not_instructions():
    """`{Case 0x114}` is a label, and only one view emits it.

    Binary Ninja prints switch-case labels as whole lines above their target.
    A database renders them where a fresh view of the same bytes does not, so
    keeping them makes one side look like it gained instructions — which is how
    a `.bndb` diffed against its own bytes came out "changed".
    """

    print("a line that is only an annotation is not an instruction")

    plain = func_with_blocks("a", {0x10: ["push rbp", "ret"]})
    labelled = func_with_blocks("b", {0x10: ["{Case 0x114}", "push rbp", "ret"]})

    check(
        "the label carries no instruction",
        align.instruction_text("{Case 0x114}").strip() == "",
        repr(align.instruction_text("{Case 0x114}")),
    )
    lines = align.function_instruction_lines(labelled)
    check(
        "so it is left out",
        [str(x) for x in lines] == ["push rbp", "ret"],
        f"{[str(x) for x in lines]}",
    )

    status, _rows = align.classify_pair(plain, labelled)
    check("and the pair is identical", status is align.FunctionStatus.IDENTICAL, f"{status}")


def test_comments_are_not_code():
    """A function someone commented is the same function.

    Binary Ninja renders a function comment as whole lines of ``CommentToken``
    above the code, and an address comment on the end of its instruction. Both
    are the user's notes: a database annotated this way and diffed against the
    binary it came from read "changed", its comment lines marked removed.
    """

    print("comments are not code")

    class Line:
        def __init__(self, *spec):
            self.tokens = [Token(getattr(TT, kind), text) for kind, text in spec]

        def __str__(self):
            return "".join(token.text for token in self.tokens)

    def mov(dst: str, src: str, comment: str = ""):
        spec = [
            ("InstructionToken", "mov"),
            ("TextToken", "     "),
            ("RegisterToken", dst),
            ("OperandSeparatorToken", ", "),
            ("RegisterToken", src),
        ]
        if comment:
            spec += [("TextToken", "  "), ("CommentToken", f"// {comment}")]
        return Line(*spec)

    note = Line(("CommentToken", "// trivial jump(arg1) tail-call stub"))
    br = Line(("InstructionToken", "br"), ("TextToken", "      "), ("RegisterToken", "x2"))

    check("a comment line carries no code", align.instruction_text(note) == "")
    check(
        "an inline comment goes, the instruction stays",
        align.instruction_text(mov("x2", "x0", "the callback")).strip() == "mov     x2, x0",
        repr(align.instruction_text(mov("x2", "x0", "the callback"))),
    )

    commented = [note, note, mov("x2", "x0", "the callback"), mov("x0", "x1"), br]
    plain = [mov("x2", "x0"), mov("x0", "x1"), br]
    rows = align.align_lines(commented, plain)
    check(
        "comment lines are kept, and marked as such",
        [row.status for row in rows][:2] == [LineStatus.COMMENT, LineStatus.COMMENT],
        f"{[row.status.value for row in rows]}",
    )
    check(
        "the code still pairs up",
        [row.status for row in rows][2:] == [LineStatus.EQUAL] * 3,
        f"{[row.status.value for row in rows]}",
    )
    check(
        "so the pair is identical",
        align.classify_rows(rows) is align.FunctionStatus.IDENTICAL,
        f"{align.classify_rows(rows)}",
    )
    similarity = align.text_similarity(rows)
    check("and wholly similar", similarity == 1.0, f"{similarity}")

    left_status, right_status = align.align_line_statuses(commented, plain)
    check("one status per line", len(left_status) == 5 and len(right_status) == 3)
    check("none of them a difference", not any(s.is_difference for s in left_status + right_status))

    # A comment between instructions stays where it was written.
    rows = align.align_lines([mov("x2", "x0"), note, br], [mov("x2", "x0"), br])
    check(
        "a comment in the middle stays in place",
        [row.status for row in rows] == [LineStatus.EQUAL, LineStatus.COMMENT, LineStatus.EQUAL],
        f"{[row.status.value for row in rows]}",
    )
    # A real change next to a comment is still a change.
    rows = align.align_lines([note, mov("x2", "x0")], [mov("x3", "x0"), Line()])
    check(
        "code differences are still reported",
        align.classify_rows(rows) is not align.FunctionStatus.IDENTICAL,
        f"{[row.status.value for row in rows]}",
    )

    # Without tokens only a leading `//` is trusted.
    check("text-only comment lines go too", align.instruction_text("  // note") == "")
    check(
        "but not a `//` inside the code",
        align.instruction_text('puts("http://x")') == 'puts("http://x")',
    )

    left = func_with_blocks("a", {0x10: ["// a note", "mov x2, x0", "br x2"]})
    right = func_with_blocks("b", {0x10: ["mov x2, x0", "br x2"]})
    alignment = align.align_blocks(left, right)
    check(
        "the block is identical, not changed",
        alignment.left_status.get(0x10) is align.BlockStatus.IDENTICAL,
        f"{alignment.left_status}",
    )
    status, _rows = align.classify_pair(left, right)
    check("and so is the function", status is align.FunctionStatus.IDENTICAL, f"{status}")


def test_classification_runs_on_instructions_only():
    """The status comes from the basic blocks, so nothing else can leak in.

    The linear rendering carries the function's name and prototype, arrives
    asynchronously, and hands back token-less lines until something has drawn
    the function — which is what made every unvisited row read "changed".
    Block text is the instructions, tokens included, every time.
    """

    print("classification runs on the instructions alone")

    body = {0x10: ["push rbp", "mov rbp, rsp"], 0x20: ["pop rbp", "ret"]}
    left = func_with_blocks("sub_10001a13c", body)
    right = func_with_blocks("aes_init", body)

    status, rows = align.classify_pair(left, right)
    check("a different name changes nothing", status is align.FunctionStatus.IDENTICAL, f"{status}")
    check("every instruction was compared", len(rows) == 4, f"got {len(rows)}")

    origin = func_with_blocks("sub_1", {0x10: ["push rbp"], 0x20: ["b 0x1000"]})
    moved = func_with_blocks("sub_2", {0x10: ["push rbp"], 0x20: ["b 0x2000"]})
    status, _rows = align.classify_pair(origin, moved)
    check(
        "a moved branch target is offsets only", status is align.FunctionStatus.MINOR, f"{status}"
    )

    changed = func_with_blocks(
        "sub_3", {0x10: ["push rbp", "xor eax, eax"], 0x20: ["pop rbp", "ret"]}
    )
    status, _rows = align.classify_pair(left, changed)
    check("a different instruction is changed", status is align.FunctionStatus.CHANGED, f"{status}")

    huge = func_with_blocks("big", {0x10: ["nop"] * (align.MAX_CLASSIFY_INSTRUCTIONS + 1)})
    status, _rows = align.classify_pair(huge, huge)
    check("an enormous pair is left unknown", status is align.FunctionStatus.UNKNOWN, f"{status}")
    check("unknown does not claim a difference", status.value == "unclassified", status.value)


def test_text_similarity_counts_unchanged_lines():
    """QBinDiff's score is a MinHash over whole basic blocks, so a one-block
    function that gained an instruction reads 0.000 while being the same code.
    This is the number the table shows instead."""

    print("similarity is the share of lines that are the same code")
    rows = align.align_lines(["mov x0, x1", "ret"], ["mov x0, x1", "ret"])
    check("identical is 1.0", align.text_similarity(rows) == 1.0, f"{rows}")

    rebased = align.align_lines(["bl 0x1000", "ret"], ["bl 0x2000", "ret"])
    check(
        "an operand respelled still counts as the same",
        align.text_similarity(rebased) == 1.0,
        f"{[r.status for r in rebased]}",
    )

    grown = align.align_lines(["mov x0, x1", "ret"], ["mov x0, x1", "bl 0x99", "ret"])
    same = align.text_similarity(grown)
    check("an added line costs a share", 0.0 < same < 1.0, f"{same}")

    rewritten = align.align_lines(["mov x0, x1"], ["add x2, x3, x4"])
    check("a rewrite is 0.0", align.text_similarity(rewritten) == 0.0, f"{rewritten}")

    check("nothing to compare is 1.0", align.text_similarity([]) == 1.0)


def test_render_levels():
    """Every view the UI offers resolves to an IL level to compare on.

    A language is a rendering of HLIL, so its blocks are paired on HLIL and its
    text is drawn from the language; generating HLIL alone leaves the language's
    linear view on its "Loading..." placeholder.
    """

    print("render levels")

    for name in align.IL_LEVELS:
        level = align.render_level(name)
        check(f"{name} is its own level", (level.level, level.language) == (name, None))
        check(f"{name} is named after itself", level.name == name)

    for name in align.LANGUAGES:
        level = align.render_level(name)
        check(f"{name} compares on HLIL", (level.level, level.language) == ("HLIL", name))
        check(f"{name} is drawn as itself", level.graph_type == name and level.name == name)

    check("a level passes through", align.render_level(align.RenderLevel("MLIL")).level == "MLIL")
    try:
        align.render_level("Pseudo COBOL")
        check("an unknown view is refused", False)
    except ValueError:
        check("an unknown view is refused", True)

    # The stub registers no languages, which is the case of a core without the
    # plugins: the IL levels must still all be offered.
    names = [level.name for level in align.available_levels()]
    check("IL levels come first", names[: len(align.IL_LEVELS)] == list(align.IL_LEVELS))

    class FakeFunction:
        def __init__(self):
            self.touched: list[str] = []

        @property
        def hlil(self):
            self.touched.append("hlil")

        def language_representation(self, language):
            self.touched.append(language)

    func = FakeFunction()
    align.ensure_rendering(func, "Pseudo Rust")
    check("a language generates HLIL, then itself", func.touched == ["hlil", "Pseudo Rust"])
    func = FakeFunction()
    align.ensure_rendering(func, "HLIL")
    check("an IL level generates no language", func.touched == ["hlil"], f"{func.touched}")


def test_anchor_row():
    """Switching views keeps the reader's place by address.

    Two renderings share no lines, only addresses, and not every address: an
    HLIL statement covers several instructions. The nearest one has to do.
    """

    print("anchor row")

    class Line:
        def __init__(self, address):
            self.address = address

    class Linear:
        """A LinearDisassemblyLine: the address is on its contents."""

        def __init__(self, address):
            self.contents = Line(address)

    rows = [
        align.AlignedRow(Linear(0x1000), Linear(0x2000), align.LineStatus.EQUAL),
        align.AlignedRow(Line(0x1010), None, align.LineStatus.REMOVED),
        align.AlignedRow(None, Line(0x1020), align.LineStatus.ADDED),
        align.AlignedRow(Line(0x1040), Line(0x1040), align.LineStatus.EQUAL),
    ]
    check("an exact address wins", align.anchor_row(rows, 0x1020) == 2)
    check("either side counts", align.anchor_row(rows, 0x2000) == 0)
    check("otherwise the nearest", align.anchor_row(rows, 0x1038) == 3)
    check("no rows, top", align.anchor_row([], 0x1000) == 0)
    check("reads either line kind", align.line_address(Linear(0x42)) == 0x42)
    check("a line without one is None", align.line_address(object()) is None)


def test_repeated_edit_pattern():
    """Repetitions are visible without folding distinct literal changes."""

    print("repeated edit pattern")
    base = ["push x29", "mov x0, x1", "ret"]
    inserted = ["push x29", "mov x0, x1", "mov w5, #0x2", "bl 0x1000", "ret"]
    first = align.edit_pattern(align.align_lines(base, inserted))
    check("an insertion has a pattern", first is not None)
    check("the same edit repeats", first == align.edit_pattern(align.align_lines(base, inserted)))
    different_target = inserted.copy()
    different_target[3] = "bl 0x2000"
    check(
        "distinct call targets remain distinct",
        first != align.edit_pattern(align.align_lines(base, different_target)),
    )
    different_position = ["mov w5, #0x2", "bl 0x1000", *base]
    check(
        "the location in the function matters",
        first != align.edit_pattern(align.align_lines(base, different_position)),
    )
    check(
        "operand-only churn has no edit pattern",
        align.edit_pattern(align.align_lines(["bl 0x1000"], ["bl 0x2000"])) is None,
    )


def main() -> int:
    for test in (
        test_empty_inputs,
        test_row_alignment_invariants,
        test_normalization,
        test_change_visibility,
        test_text_similarity_counts_unchanged_lines,
        test_function_classification,
        test_annotations_are_not_code,
        test_annotation_only_lines_are_not_instructions,
        test_comments_are_not_code,
        test_classification_runs_on_instructions_only,
        test_il_is_generated_before_rendering,
        test_render_levels,
        test_anchor_row,
        test_repeated_edit_pattern,
        test_shape_signature,
        test_side_statuses,
        test_markers,
        test_block_alignment_cases,
        test_disjoint_functions,
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
