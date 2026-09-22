"""Smoke-test the backend against the real Binary Ninja API and real binaries.

Unlike the other test modules, this one needs a working Binary Ninja
installation and a license that permits headless use. It is skipped
automatically when ``binaryninja`` cannot be imported.

    .venv-qbindiff-312/bin/python binja_diff/tests/test_live.py [primary] [secondary]

Defaults to diffing two system binaries against each other.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

#: Added by the Binary Ninja installer for GUI use; not always on sys.path
#: for an arbitrary interpreter.
_BN_PYTHON = Path.home() / "Documents" / "binaryninja" / "python"
if _BN_PYTHON.is_dir() and str(_BN_PYTHON) not in sys.path:
    sys.path.append(str(_BN_PYTHON))

try:
    import binaryninja
except Exception as exc:  # pragma: no cover - depends on the host
    print(f"SKIP: Binary Ninja is not importable here ({exc.__class__.__name__}: {exc})")
    raise SystemExit(0) from None

# Registered by path rather than by importing the parent directory: the
# checkout's own name is not a valid module name. Loaded the same way, since
# importing it through the package would be circular.
_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.register_package()


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


def pick_binaries() -> tuple[str, str]:
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]
    candidates = [
        p
        for p in ("/bin/true", "/bin/false", "/bin/echo", "/bin/cat", "/bin/ls")
        if Path(p).is_file()
    ]
    if len(candidates) < 2:
        print("SKIP: could not find two system binaries to diff")
        raise SystemExit(0)
    return candidates[0], candidates[1]


def test_backend_against_real_view(bv):
    print(f"backend over {bv.file.filename}")
    from qbindiff.loader import Program

    from binja_diff.core.backend import ProgramBackendBinja

    program = Program.from_backend(ProgramBackendBinja(bv))

    check("function count matches Binary Ninja", len(program) == len(bv.functions))
    check("program is non-empty", len(program) > 0, f"got {len(program)}")

    addrs = {addr for addr, _f in program.items()}
    bn_addrs = {f.start for f in bv.functions}
    check("addresses match Binary Ninja", addrs == bn_addrs, f"diff {addrs ^ bn_addrs}")

    total_blocks = 0
    total_instrs = 0
    checked = 0
    for addr, func in program.items():
        bn_func = bv.get_function_at(addr)
        if bn_func is None:
            continue
        with func:
            # Like Program, Function.__iter__ yields values rather than keys,
            # so .values() from the Mapping mixin raises.
            blocks = list(func)
            total_blocks += len(blocks)
            if not func.is_import():
                check_once = len(blocks) == len(bn_func.basic_blocks)
                if not check_once and checked < 3:
                    check(
                        f"block count for {func.name}",
                        False,
                        f"{len(blocks)} vs {len(bn_func.basic_blocks)}",
                    )
                    checked += 1
            for block in blocks:
                instrs = list(block)
                total_instrs += len(instrs)
                for instr in instrs:
                    if instr.mnemonic == "":
                        check(f"empty mnemonic at {instr.addr:#x}", False)
                        return

    check("basic blocks recovered", total_blocks > 0, f"got {total_blocks}")
    check("instructions recovered", total_instrs > 0, f"got {total_instrs}")
    check("callgraph populated", len(program.callgraph) == len(program))
    print(f"       {len(program)} functions, {total_blocks} blocks, {total_instrs} instructions")
    return program


def test_real_diff(primary_bv, secondary_bv):
    print("end-to-end diff on real binaries")
    from binja_diff.core.engine import run_diff

    steps: list[tuple[str, float]] = []
    result = run_diff(
        primary_bv,
        secondary_bv,
        progress=lambda label, fraction: steps.append((label, fraction)),
    )

    check("diff produced a result", result is not None)
    if result is None:
        return
    check("progress was reported", len(steps) > 0, f"got {len(steps)}")
    phases = {label for label, _fraction in steps}
    check("both phases reported", len(phases) >= 2, f"got {phases}")
    check("similarity in range", 0.0 <= result.similarity <= 1.0, f"got {result.similarity}")
    check("primary index built", len(result.by_primary) == result.nb_match)
    check("secondary index built", len(result.by_secondary) == result.nb_match)
    print(
        f"       {result.nb_match} matches, "
        f"{result.nb_unmatched_primary} primary-only, "
        f"{result.nb_unmatched_secondary} secondary-only, "
        f"similarity {result.similarity:.3f}"
    )
    return result


def test_alignment_on_real_functions(result):
    print("alignment on real matched functions")
    from binja_diff.core import align

    if result is None or result.nb_match == 0:
        print("  (no matches to align)")
        return

    aligned_any = False
    for match in list(result.matches)[:5]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue

        blocks = align.align_blocks(primary, secondary, "Disassembly")
        check(
            f"blocks classified for {primary.name}",
            len(blocks.left_status) == len(primary.basic_blocks),
            f"{len(blocks.left_status)} vs {len(primary.basic_blocks)}",
        )

        # Languages are rendered from HLIL but are a different linear view.
        for level in [level.name for level in align.available_levels()]:
            rows = align.align_function_text(
                result.primary_bv, primary, result.secondary_bv, secondary, level
            )
            check(f"{level} produced rows for {primary.name}", len(rows) > 0, "empty")
            check(
                f"{level} rows are well formed",
                all(r.left is not None or r.right is not None for r in rows),
            )
        aligned_any = True
        break

    check("aligned at least one pair", aligned_any)


def test_il_renders_on_the_first_try(result):
    """The IL panes must be readable without being visited twice.

    Rendering IL that has not been generated yet yields a "Loading..."
    placeholder, filled in asynchronously — which a pane drawn once never
    sees. Only real Binary Ninja generates IL lazily, so only this test can
    catch it coming back.
    """

    print("IL renders on the first pass")
    from binja_diff.core import align

    if result is None or result.nb_match == 0:
        print("  (no matches)")
        return

    pair = None
    for match in list(result.matches)[:20]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is not None and secondary is not None and primary.basic_blocks:
            pair = (primary, secondary)
            break
    if pair is None:
        print("  (no resolvable pair)")
        return

    primary, secondary = pair
    languages = [level.name for level in align.available_levels() if level.language]
    for level in ("LLIL", "MLIL", "HLIL", *languages):
        # Deliberately without touching the graph path first: that is what used
        # to generate the IL as a side effect and mask this.
        lines = align.function_lines(result.primary_bv, primary, level)
        text = [str(line) for line in lines]
        check(f"{level} produced lines", bool(text), "empty")
        check(
            f"{level} is not still loading",
            not any("Loading" in line for line in text),
            f"got {text[:3]}",
        )


def test_status_agrees_with_the_panes(result):
    """The table's status must describe the rows the panes actually show.

    It used to be read off QBinDiff's similarity, which says nothing about the
    text: a pair scored 1.0 came up "identical" while the pane marked every
    line '~'. Only real disassembly can catch that disagreeing again.
    """

    print("match table status matches the rendered diff")
    from binja_diff.core import align

    if result is None or result.nb_match == 0:
        print("  (no matches)")
        return

    checked = 0
    for match in list(result.matches)[:40]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue

        status, rows = align.classify_pair(primary, secondary)
        markers = {row.status for row in rows if row.status.is_difference}
        if status is align.FunctionStatus.IDENTICAL:
            check(f"{primary.name}: identical means no markers", not markers, f"got {markers}")
        elif status is align.FunctionStatus.MINOR:
            check(
                f"{primary.name}: offsets only means only '~'",
                markers == {align.LineStatus.MINOR},
                f"got {markers}",
            )
        else:
            check(f"{primary.name}: changed means real differences", bool(markers))
        checked += 1
        if checked >= 5:
            break

    check("classified at least one pair", checked > 0)


def test_changes_are_visible(result):
    """Non-identical matched functions must produce visible differences.

    This is the regression guard for changes being silently classified as
    equal: it holds the whole pipeline to the promise that a function the
    matcher scored below 1.0 actually shows something in the text panes.
    """

    print("changes surface in the text views")
    from binja_diff.core import align

    if result is None:
        print("  (no result)")
        return

    imperfect = [m for m in result.matches if m.similarity < 0.99]
    if not imperfect:
        print("  (no imperfect matches in this pair; skipping)")
        return

    imperfect.sort(key=lambda m: m.similarity)
    inspected = 0
    silent = []
    for match in imperfect[:8]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue

        rows = align.align_function_text(
            result.primary_bv, primary, result.secondary_bv, secondary, "Disassembly"
        )
        differing = [r for r in rows if r.status.is_difference]
        inspected += 1
        if not differing:
            silent.append((primary.name, match.similarity))
        else:
            kinds = sorted({r.status.value for r in differing})
            print(
                f"       {primary.name} (sim {match.similarity:.3f}): "
                f"{len(differing)}/{len(rows)} rows differ {kinds}"
            )

    check("inspected some imperfect matches", inspected > 0)
    check(
        "no imperfect match renders as fully identical",
        not silent,
        f"silent: {silent}",
    )


def test_graph_line_highlighting(result):
    """Per-instruction highlights in the flow graph must actually stick.

    The graph pane zips per-line statuses onto a node's own ``lines``, so two
    things have to hold and neither is obvious: a node's line count must match
    its basic block's rendered text, and a highlight written through the
    ``lines`` setter must survive the round trip into the core. If either fails
    the pane silently falls back to no highlighting at all.
    """

    print("flow graph per-instruction highlighting")
    from binaryninja import HighlightColor
    from binaryninja.enums import HighlightColorStyle

    from binja_diff.core import align

    if result is None:
        print("  (no result)")
        return

    # Least similar first: in a near-identical binary pair only a handful of
    # functions have a changed block at all, and mapping order will not find them.
    candidates = sorted(result.matches, key=lambda m: m.similarity)

    pair = None
    for match in candidates[:40]:
        primary = result.primary_bv.get_function_at(match.primary.addr)
        secondary = result.secondary_bv.get_function_at(match.secondary.addr)
        if primary is None or secondary is None:
            continue
        alignment = align.align_blocks(primary, secondary, "Disassembly")
        changed = [
            p
            for p in alignment.pairs
            if p.status is align.BlockStatus.CHANGED and p.left_addr is not None
        ]
        if changed:
            pair = (primary, secondary, alignment, changed[0])
            break

    if pair is None:
        print("  (no changed block pair in this binary pair; skipping)")
        return

    primary, secondary, alignment, block_pair = pair

    left_graph = primary.create_graph()
    left_graph.layout_and_wait()
    right_graph = secondary.create_graph()
    right_graph.layout_and_wait()

    def node_for(graph, addr):
        return next(
            (n for n in graph.nodes if n.basic_block is not None and n.basic_block.start == addr),
            None,
        )

    left_node = node_for(left_graph, block_pair.left_addr)
    right_node = node_for(right_graph, block_pair.right_addr)
    check("found both graph nodes", left_node is not None and right_node is not None)
    if left_node is None or right_node is None:
        return

    # A node prepends a symbol label its basic block's text does not have, so the
    # statuses have to come from the node lines themselves.
    lines = left_node.lines
    right_lines = right_node.lines
    left_status, right_status = align.align_line_statuses(lines, right_lines)

    check(
        "statuses match the left node's line count",
        len(left_status) == len(lines),
        f"{len(left_status)} vs {len(lines)}",
    )
    check(
        "statuses match the right node's line count",
        len(right_status) == len(right_lines),
        f"{len(right_status)} vs {len(right_lines)}",
    )
    check(
        "a changed block has something to highlight",
        any(s.is_difference for s in left_status) or any(s.is_difference for s in right_status),
        f"left {[s.value for s in left_status]}",
    )
    check(
        "unchanged instructions stay unmarked",
        any(not s.is_difference for s in left_status),
        "every line flagged, which would be the old whole-block behavior",
    )

    marked = [i for i, s in enumerate(left_status) if s.is_difference]
    for index in marked:
        lines[index].highlight = HighlightColor(red=210, green=170, blue=40)
    left_node.lines = lines
    node = left_node

    read_back = node.lines
    check("line count survives the write", len(read_back) == len(lines))
    persisted = [
        i
        for i, line in enumerate(read_back)
        if line.highlight is not None
        and line.highlight.style == HighlightColorStyle.CustomHighlightColor
    ]
    check(
        "highlights persisted on exactly the changed lines",
        persisted == marked,
        f"marked {marked}, persisted {persisted}",
    )
    print(f"       {primary.name}: {len(marked)}/{len(lines)} lines highlighted in one block")


def test_saved_diff_round_trip(result, primary_path: str):
    """A saved diff must survive the real metadata store and a real .bndb.

    The stubbed tests prove the JSON round trips; only here can it be shown
    that Binary Ninja accepts a payload that size as metadata and hands it back
    intact after the database has been written and reopened.
    """

    print("saving and restoring a diff")
    import tempfile

    from binja_diff.core import persist

    if result is None:
        print("  (no result)")
        return

    saved = persist.SavedDiff.from_result(result)
    persist.store_in_database(result.primary_bv, saved)
    reloaded = persist.load_from_database(result.primary_bv)
    check("stored diff reads back", reloaded is not None)
    if reloaded is None:
        return
    check(
        "matches survive the metadata store",
        [(m.primary.addr, m.secondary.addr) for m in reloaded.matches]
        == [(m.primary.addr, m.secondary.addr) for m in result.matches],
    )
    check("similarity survives", abs(reloaded.similarity - result.similarity) < 1e-6)
    check(
        "no drift against the binary it came from",
        reloaded.primary.differences(result.primary_bv) == [],
    )

    restored = reloaded.to_result(result.primary_bv, result.secondary_bv)
    check("restored result is usable", restored.nb_match == result.nb_match)
    check(
        "restored functions resolve in the view",
        all(
            result.primary_bv.get_function_at(m.primary.addr) is not None
            for m in list(restored.matches)[:20]
        ),
    )

    # A separate view of the same file: creating a database rebinds the view to
    # it, and this one's directory is about to be deleted.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "with_diff.bndb")
        fresh = binaryninja.load(primary_path)
        try:
            persist.store_in_database(fresh, saved)
            created = fresh.create_database(db_path)
        finally:
            fresh.file.close()
        check("database with a saved diff created", created)
        if not created:
            return

        reopened = binaryninja.load(db_path)
        try:
            persisted = persist.load_from_database(reopened)
            check("diff survives closing and reopening the database", persisted is not None)
            if persisted is not None:
                check("match count unchanged", len(persisted.matches) == result.nb_match)
        finally:
            reopened.file.close()

    persist.remove_from_database(result.primary_bv)


def test_kernelcache_scoping():
    """Diffing one kext out of a kernelcache, if one is to hand.

    A kernelcache is the case scoping exists for: matching is quadratic, so the
    whole container never finishes while one kext takes under a minute. Only
    Binary Ninja's own loader can enumerate and map a kext, so this cannot be
    checked against the stub.
    """

    print("kernelcache scoping")
    from binja_diff.core import scope

    caches = sorted(Path.home().glob("dev/kcache/kernelcache*"))
    if len(caches) < 2:
        print("  (no kernelcache pair to hand; skipping)")
        return

    bv = binaryninja.load(str(caches[0]), update_analysis=False)
    try:
        check("recognised as a kernelcache", bv.view_type == scope.KERNELCACHE_VIEW, bv.view_type)
        regions = scope.available_regions(bv)
        check("kexts enumerated", len(regions) > 10, f"got {len(regions)}")
        check("none loaded yet", not any(r.loaded for r in regions))
        check("and no functions yet", len(bv.functions) == 0, f"got {len(bv.functions)}")

        wanted = next((r for r in regions if "AppleSEPManager" in r.name), regions[0])
        check("found by name", scope.find_region(bv, wanted.name) is not None)
        check("loads on demand", scope.ensure_loaded(bv, wanted))
        check("which is what creates functions", len(bv.functions) > 0)

        scoped = scope.functions_in(bv, scope.find_region(bv, wanted.name))
        check(
            "and they all belong to that kext",
            0 < len(scoped) <= len(bv.functions),
            f"{len(scoped)} of {len(bv.functions)}",
        )
        print(f"       {wanted.name}: {len(scoped)} functions of {len(regions)} kexts")
    finally:
        bv.file.close()


def test_similarity_provider(primary_path: str, secondary_path: str):
    """Drive a real Binary Similarity session through the QBinDiff provider.

    Only the Ultimate edition has the similarity API, so this skips elsewhere.
    Nothing below can be checked against the stub: the session owns the entity
    ids, calls the provider on its own threads, and dispatches the callbacks
    through the core — which is how `_render` shadowing the base class's own
    callback went unnoticed until a session actually rendered something.
    """

    print("QBinDiff as a similarity provider")
    try:
        from binaryninja import similarity
    except Exception as exc:
        print(f"  (no similarity API here: {exc.__class__.__name__}; skipping)")
        return

    from binja_diff.core.similarity import PROVIDER_NAME

    names = [t.name for t in similarity.SimilarityProviderType]
    check("the provider is registered", PROVIDER_NAME in names, f"{names}")
    if PROVIDER_NAME not in names:
        return

    provider_type = similarity.SimilarityProviderType[PROVIDER_NAME]
    settings = provider_type.get_default_settings()
    check("its settings carry our schema", settings.get_double("qbindiff.tradeoff") > 0)
    provider = provider_type.create(settings)
    check("and it creates a provider", provider is not None)

    session = similarity.SimilaritySession()
    session.add_provider(provider)
    # The same binary on both sides: every function must match itself, which is
    # the one outcome that needs no judgement about what "similar" means.
    reference = similarity.SimilaritySessionNode(binaryninja.load(primary_path))
    target = similarity.SimilaritySessionNode(binaryninja.load(primary_path))
    session.graph.add_node(reference)
    session.graph.add_node(target)
    session.graph.add_edge(reference, target)

    completion = session.run()
    waited = 0.0
    while not completion.is_finished and waited < 300:
        time.sleep(0.2)
        waited += 0.2
    check("the session finished", completion.is_finished, f"after {waited:.0f}s")

    from binja_diff.core.engine import is_generated_name

    scored = []
    sample = None
    for entity_id in target.entities:
        for result_id in target.get_results(entity_id):
            result = target.get_result(result_id)
            scored.append(result.similarity)
            # Sample a pair whose receiving side has no real name: Binary
            # Ninja's own apply keeps an existing symbol, so `_start` would
            # report success and rename nothing.
            func = target.get_entity_function(entity_id)
            if sample is None and func is not None and is_generated_name(func.name):
                sample = (entity_id, result_id, result)

    check("results came back", len(scored) > 0, f"{len(scored)} results")
    if sample is None:
        print("  (every function here carries a symbol; skipping the apply checks)")
        return
    check(
        "a binary against itself is all 255s",
        all(value == 255 for value in scored),
        f"lowest {min(scored)}",
    )

    entity_id, result_id, result = sample
    name = provider.get_name(target, entity_id, result_id)
    check("the match has a name", bool(name), f"{name!r}")

    # Renaming the reference and asking again: the name must come from the live
    # function, not from what the entity recorded when the session ran.
    other = target.incoming_nodes[0] if target.incoming_nodes else None
    renamed = other.get_entity_function(result.target.entity_id) if other else None
    if renamed is not None:
        renamed.name = "renamed_after_the_run"
        check(
            "and it is read live",
            provider.get_name(target, entity_id, result_id) == "renamed_after_the_run",
            f"{provider.get_name(target, entity_id, result_id)!r}",
        )
        status = provider.apply(target, entity_id, result_id)
        check(
            "applying transfers it",
            status == similarity.SimilarityApplyStatus.SimilarityApplySuccess,
            f"{status}",
        )
        check(
            "onto the target function",
            target.get_entity_function(entity_id).name == "renamed_after_the_run",
        )

    context = similarity.SimilarityRenderContext()
    provider.render(target, entity_id, context, result_id)
    views = context.views
    check("rendering produces views", len(views) > 0, f"{len(views)} views")
    check("graph and linear, per side", len(views) >= 4, f"{[v.group for v in views]}")
    tinted, plain = _count_tinted(views)
    # The same function on both sides: colouring anything here would be the
    # whole-block wash this rendering exists to avoid.
    check("identical functions are left plain", tinted == 0, f"{tinted} tinted lines")
    check("and their lines were still rendered", plain > 0, f"{plain} plain lines")

    _check_view_levels(similarity, provider, target, entity_id, result_id)
    _check_changed_pair_is_tinted(similarity, provider_type, primary_path, secondary_path)


def _check_view_levels(similarity, provider, target, entity_id, result_id):
    """The header's view selector must actually change what is rendered.

    The context carries the level as a preference a provider may ignore; a
    provider that does ignore it renders disassembly whichever tab is picked,
    which is indistinguishable from a broken control.
    """

    from binaryninja.enums import FunctionGraphType

    from binja_diff.core.similarity import requested_level

    rendered = {}
    # A language representation is asked for by name, and is a rendering of
    # HLIL rather than a level of its own -- which is why it needs its own row
    # here: falling back to disassembly for it painted the whole function.
    for label, graph_type, expected in (
        ("Disassembly", FunctionGraphType.NormalFunctionGraph, ("Disassembly", None)),
        ("LLIL", FunctionGraphType.LowLevelILFunctionGraph, ("LLIL", None)),
        ("MLIL", FunctionGraphType.MediumLevelILFunctionGraph, ("MLIL", None)),
        ("HLIL", FunctionGraphType.HighLevelILFunctionGraph, ("HLIL", None)),
        ("Pseudo C", "Pseudo C", ("HLIL", "Pseudo C")),
        ("Pseudo Rust", "Pseudo Rust", ("HLIL", "Pseudo Rust")),
    ):
        level = label
        context = similarity.SimilarityRenderContext()
        context.preferred_view_type = graph_type
        asked = requested_level(context)
        check(f"{label} is recognised", (asked.level, asked.language) == expected, f"{asked}")
        provider.render(target, entity_id, context, result_id)
        graphs = [view.graph for view in context.views if view.graph is not None]
        first = ""
        if graphs and graphs[0].nodes and graphs[0].nodes[0].lines:
            first = str(graphs[0].nodes[0].lines[0]).strip()
        rendered[level] = first
        check(f"{label} renders", bool(graphs) and bool(first), f"{len(graphs)} graphs, {first!r}")

    ils = ["Disassembly", "LLIL", "MLIL", "HLIL"]
    check(
        "the IL levels each render their own text",
        len({rendered[level] for level in ils}) == len(ils),
        f"{ {level: rendered[level] for level in ils} }",
    )
    # Two language representations can agree on a function that uses nothing
    # specific to either, so they are only checked against the IL they render.
    check(
        "and a language representation is not raw HLIL",
        rendered["Pseudo C"] != rendered["HLIL"],
        f"{rendered['Pseudo C']!r} vs {rendered['HLIL']!r}",
    )


def _count_tinted(views) -> tuple[int, int]:
    """Lines carrying a highlight, and lines left alone, across every graph."""

    tinted = plain = 0
    for view in views:
        if view.graph is None:
            continue
        for node in view.graph.nodes:
            for line in node.lines:
                if line.highlight is not None and "none" not in str(line.highlight):
                    tinted += 1
                else:
                    plain += 1
    return tinted, plain


def _check_changed_pair_is_tinted(similarity, provider_type, primary_path, secondary_path):
    """A pair that really differs must colour instructions, not everything."""

    session = similarity.SimilaritySession()
    session.add_provider(provider_type.create(provider_type.get_default_settings()))
    reference = similarity.SimilaritySessionNode(binaryninja.load(primary_path))
    target = similarity.SimilaritySessionNode(binaryninja.load(secondary_path))
    session.graph.add_node(reference)
    session.graph.add_node(target)
    session.graph.add_edge(reference, target)
    completion = session.run()
    waited = 0.0
    while not completion.is_finished and waited < 300:
        time.sleep(0.2)
        waited += 0.2

    provider = session.providers[0] if hasattr(session, "providers") else None
    for entity_id in target.entities:
        for result_id in target.get_results(entity_id):
            result = target.get_result(result_id)
            if result.similarity == 255:
                continue
            context = similarity.SimilarityRenderContext()
            (provider or provider_type.create(provider_type.get_default_settings())).render(
                target, entity_id, context, result_id
            )
            tinted, _plain = _count_tinted(context.views)
            check("a differing pair tints instructions", tinted > 0, f"{tinted} tinted")
            return
    print("  (no differing pair between these two binaries; skipping)")


def test_database_round_trip(source_path: str):
    """A .bndb must load with its saved analysis intact and diff normally."""

    print("database (.bndb) support")
    import tempfile

    from binja_diff.core.engine import load_secondary, run_diff

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "saved.bndb")

        bv = binaryninja.load(source_path)
        target = next((f for f in bv.functions if f.basic_blocks), None)
        if target is None:
            print("  (no function to annotate; skipping)")
            bv.file.close()
            return
        original_addr = target.start
        target.name = "renamed_by_test"
        target.set_comment_at(original_addr, "comment from the database")
        bv.update_analysis_and_wait()
        created = bv.create_database(db_path)
        bv.file.close()

        check("database created", created)
        if not created:
            return

        loaded = load_secondary(db_path)
        check("database loads", loaded is not None)
        if loaded is None:
            return
        try:
            names = {f.name for f in loaded.functions}
            check("renamed function preserved", "renamed_by_test" in names)
            restored = loaded.get_function_at(original_addr)
            check(
                "comment preserved",
                restored is not None
                and restored.get_comment_at(original_addr) == "comment from the database",
            )

            fresh = binaryninja.load(source_path)
            try:
                result = run_diff(fresh, loaded)
                check("diff against a database succeeds", result is not None)
                if result is not None:
                    check(
                        "matches the identical content",
                        result.nb_match > 0,
                        f"got {result.nb_match}",
                    )
                    print(f"       {result.nb_match} matches, similarity {result.similarity:.3f}")
            finally:
                fresh.file.close()
        finally:
            loaded.file.close()

    print("  cancelling a load")
    # Aborting via the progress callback makes binaryninja.load() report a
    # generic failure; that must surface as a clean cancellation, never as an
    # error dialog.
    try:
        cancelled_view = load_secondary(source_path, cancelled=lambda: True)
        check("cancelled load raises nothing", True)
        if cancelled_view is not None:
            cancelled_view.file.close()
    except Exception as exc:
        check("cancelled load raises nothing", False, repr(exc))


def main() -> int:
    primary_path, secondary_path = pick_binaries()
    print(f"primary={primary_path} secondary={secondary_path}\n")

    primary_bv = binaryninja.load(primary_path)
    secondary_bv = binaryninja.load(secondary_path)
    try:
        test_backend_against_real_view(primary_bv)
        result = test_real_diff(primary_bv, secondary_bv)
        test_alignment_on_real_functions(result)
        test_il_renders_on_the_first_try(result)
        test_status_agrees_with_the_panes(result)
        test_changes_are_visible(result)
        test_graph_line_highlighting(result)
        test_saved_diff_round_trip(result, primary_path)
        test_kernelcache_scoping()
        test_similarity_provider(primary_path, secondary_path)
        test_database_round_trip(primary_path)
    finally:
        primary_bv.file.close()
        secondary_bv.file.close()

    print()
    if check.failures:
        print(f"{check.failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
