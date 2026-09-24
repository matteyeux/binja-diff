# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""QBinDiff as a provider for Binary Ninja's Binary Similarity sessions.

Binary Similarity compares a graph of binaries with pluggable *providers*;
Binary Ninja ships Google BinDiff and WARP, and this adds QBinDiff alongside
them. A session can run several providers at once and let a resolver weigh
their results against each other, so this is worth having even where the diff
view already exists: the sidebar handles families of binaries, scheduling, and
applying names, and QBinDiff's belief propagation matches functions that no
signature scheme will.

Everything the provider needs is already here — `backend.ProgramBackendBinja`
puts a `BinaryView` in front of QBinDiff, and `align` compares two functions
line by line — so this module is the adapter between the two object models and
nothing more.

Three things about that adapter are worth knowing:

* **The score is ours, not QBinDiff's.** A result carries a similarity in
  0..255 that a resolver thresholds on, and QBinDiff's own number is a MinHash
  over whole basic blocks: a single-block function that gained one instruction
  scores zero against its own previous build (see `ui/matchtable.py`). Feeding
  that to a resolver would refuse to apply names for exactly the functions that
  changed a little. `align.text_similarity` answers the question the resolver
  is actually asking, and `MAX_CLASSIFY_INSTRUCTIONS` bounds the cost; QBinDiff
  supplies the confidence, which is what it is good at.
* **Matching is global.** QBinDiff solves an assignment over every function
  pair, so a node's *scheduled* entities cannot narrow the computation — they
  narrow which results are reported. Excluding functions in Node Configuration
  therefore makes a session quieter, not faster.
* **The other side is reached through the graph.** Naming and rendering a
  result need the node holding the target, and `incoming_nodes()` /
  `outgoing_nodes()` are how a provider finds it without holding references
  that would outlive the visit — which for a node backed by a `.bndb` would
  mean holding its database open.
"""

from __future__ import annotations

import json
import traceback

from binaryninja import BinaryView, log_error, log_info, log_warn

from . import align
from .engine import DiffOptions, DiffResult, run_diff

#: What the session and the sidebar call this provider.
PROVIDER_NAME = "QBinDiff"

PROVIDER_DESCRIPTION = "Uses qbindiff to perform binary diffing"

#: Settings schema, and the QBinDiff parameters worth exposing. Everything else
#: is left at QBinDiff's defaults, which are also ours -- measured on two builds
#: of one binary, no other value of the tradeoff scored better.
_SETTINGS_GROUP = "qbindiff"
_SETTINGS: tuple[tuple[str, dict], ...] = (
    (
        "tradeoff",
        {
            "title": "Feature/structure tradeoff",
            "type": "number",
            "default": DiffOptions.tradeoff,
            "minValue": 0.0,
            "maxValue": 1.0,
            "description": (
                "Weight between what a function contains (1.0) and where it sits in the "
                "call graph (0.0). The default suits two builds of one program; raise it "
                "when only part of each binary is loaded, since the call graph is then "
                "truncated and says less."
            ),
        },
    ),
    (
        "sparsityRatio",
        {
            "title": "Sparsity ratio",
            "type": "number",
            "default": DiffOptions.sparsity_ratio,
            "minValue": 0.0,
            "maxValue": 1.0,
            "description": (
                "Share of the least likely candidate pairs discarded before matching. "
                "Lower is more accurate and slower; it is raised automatically as far "
                "as a program's size requires."
            ),
        },
    ),
    (
        "distance",
        {
            "title": "Distance",
            "type": "string",
            "default": DiffOptions.distance,
            "description": "Distance between feature vectors.",
            "enum": ["haussmann", "canberra", "cosine", "euclidean", "correlation"],
        },
    ),
    (
        "scoreByInstructions",
        {
            "title": "Score by comparing instructions",
            "type": "boolean",
            "default": True,
            "description": (
                "Report similarity as the share of a function's lines that did not "
                "change. With this off, QBinDiff's own score is reported instead, which "
                "is a hash over whole basic blocks and reads zero for a one-block "
                "function that gained a single instruction."
            ),
        },
    ),
)


def _setting_key(name: str) -> str:
    return f"{_SETTINGS_GROUP}.{name}"


def is_available() -> tuple[bool, str]:
    """Whether this Binary Ninja exposes the similarity API, and why not."""

    try:
        import binaryninja.similarity  # noqa: F401
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    return True, ""


def default_settings():
    """A settings instance carrying this provider's schema."""

    from binaryninja import Settings

    instance = Settings("qbindiff-similarity")
    instance.register_group(_SETTINGS_GROUP, PROVIDER_NAME)
    for name, properties in _SETTINGS:
        instance.register_setting(_setting_key(name), json.dumps(properties))
    return instance


def options_from(settings) -> tuple[DiffOptions, bool]:
    """Read the settings into DiffOptions. Missing keys keep their defaults.

    Read defensively: the settings a session hands back are whatever schema it
    was created with, and a provider that raises here would take the whole run
    down over a value it could have defaulted.
    """

    options = DiffOptions()
    by_instructions = True
    if settings is None:
        return options, by_instructions

    def read(name: str, reader, fallback):
        try:
            return reader(_setting_key(name))
        except Exception:
            return fallback

    options.tradeoff = float(read("tradeoff", settings.get_double, options.tradeoff))
    options.sparsity_ratio = float(
        read("sparsityRatio", settings.get_double, options.sparsity_ratio)
    )
    distance = str(read("distance", settings.get_string, options.distance))
    if distance:
        options.distance = distance
    by_instructions = bool(read("scoreByInstructions", settings.get_bool, True))
    return options, by_instructions


def _score(value: float) -> int:
    """A 0..1 ratio as the 0..255 a similarity result carries."""

    return max(0, min(255, round(value * 255)))


def line_similarity(
    primary_bv: BinaryView, secondary_bv: BinaryView, primary_addr: int, secondary_addr: int
) -> float | None:
    """How much of a matched pair is unchanged, or None if it cannot be said.

    None covers both a function that is not there any more and one too large to
    classify, which `align.classify_pair` reports by handing back no rows.
    """

    left = primary_bv.get_function_at(primary_addr)
    right = secondary_bv.get_function_at(secondary_addr)
    if left is None or right is None:
        return None
    try:
        _status, rows = align.classify_pair(left, right)
    except Exception:
        return None
    return align.text_similarity(rows) if rows else None


def entity_index(node) -> dict[int, int]:
    """Address -> entity id for one node.

    Built per visit rather than kept: entity ids change as a session runs, and
    a stale index would attach results to the wrong function.
    """

    index: dict[int, int] = {}
    for entity_id in node.entities:
        info = node.get_entity(entity_id)
        if info is not None:
            index.setdefault(info.address, entity_id)
    return index


def node_by_id(node, node_id: int):
    """Find a neighbour by id, or ``node`` itself. None when it is not adjacent.

    A provider is handed one node and a result pointing into another; the graph
    is how it gets there without holding a reference of its own.
    """

    if node.id == node_id:
        return node
    for other in list(node.incoming_nodes) + list(node.outgoing_nodes):
        if other.id == node_id:
            return other
    return None


def register() -> bool:
    """Register the provider type. False when this Binary Ninja has no similarity API."""

    available, reason = is_available()
    if not available:
        log_info(
            f"Binary Similarity is unavailable, not registering {PROVIDER_NAME}: {reason}",
            "QBinDiff",
        )
        return False

    from binaryninja.similarity import (
        SimilarityEntityInfo,
        SimilarityEntityType,
        SimilarityProvider,
        SimilarityProviderType,
        SimilaritySessionCompletionQuery,
    )

    class QBinDiffProvider(SimilarityProvider):
        """Matches the functions of two connected nodes with QBinDiff."""

        def __init__(self, provider_type, settings=None):
            super().__init__(provider_type)
            self._options, self._by_instructions = options_from(settings)

        def perform_update_settings(self, settings) -> bool:
            self._options, self._by_instructions = options_from(settings)
            return True

        def perform_visit_node(self, node, results, completion) -> bool:
            # Nothing to say about a binary on its own: QBinDiff compares two.
            return True

        def perform_visit_node_edge(self, from_node, to_node, results, completion) -> bool:
            try:
                return self._visit_edge(from_node, to_node, results, completion)
            except Exception as exc:
                log_error(traceback.format_exc(), "QBinDiff")
                log_error(f"QBinDiff provider failed: {exc}", "QBinDiff")
                return False

        def _visit_edge(self, from_node, to_node, results, completion) -> bool:
            reference, target = from_node.view, to_node.view
            if reference is None or target is None:
                log_warn(
                    "QBinDiff needs both binaries loaded; skipping this edge",
                    "QBinDiff",
                )
                return False

            scheduled = set(to_node.scheduled_entities)
            if not scheduled:
                return True

            progress_query = SimilaritySessionCompletionQuery.for_node(to_node.id).with_provider(
                self.id
            )

            def report(_label: str, fraction: float) -> None:
                # Leave the last tenth for scoring, which is the part that
                # reads both binaries again.
                if fraction >= 0:
                    completion.set_progress(progress_query, fraction * 0.9)

            result = run_diff(
                reference,
                target,
                options=self._options,
                progress=report,
                cancelled=lambda: completion.is_stop_requested,
            )
            if result is None:
                return False

            added = self._report(result, from_node, to_node, scheduled, results, completion)
            completion.set_progress(progress_query, 1.0)
            log_info(
                f"QBinDiff: {added} result(s) for {target.file.filename} "
                f"against {reference.file.filename}",
                "QBinDiff",
            )
            return True

        def _report(self, result: DiffResult, from_node, to_node, scheduled, results, completion):
            """Turn matches into results, creating target entities as needed."""

            target_index = entity_index(to_node)
            reference_index = entity_index(from_node)
            added = 0

            for match in result.matches:
                if completion.is_stop_requested:
                    break
                entity_id = target_index.get(match.secondary.addr)
                if entity_id is None or entity_id not in scheduled:
                    continue

                reference_id = reference_index.get(match.primary.addr)
                if reference_id is None:
                    # A function QBinDiff matched that the session never listed:
                    # it has to exist as an entity to be pointed at.
                    reference_id = from_node.create_entity(
                        SimilarityEntityInfo(
                            SimilarityEntityType.SimilarityEntityFunction,
                            match.primary.addr,
                            match.primary.name,
                        )
                    )
                    if not reference_id:
                        continue
                    reference_index[match.primary.addr] = reference_id

                similarity = None
                if self._by_instructions:
                    similarity = line_similarity(
                        result.primary_bv,
                        result.secondary_bv,
                        match.primary.addr,
                        match.secondary.addr,
                    )
                if similarity is None:
                    similarity = match.similarity

                results.add_result(
                    source=_ref(to_node.id, entity_id),
                    target=_ref(from_node.id, reference_id),
                    similarity=_score(similarity),
                    confidence=_score(match.confidence),
                )
                added += 1
            return added

        def perform_get_name(self, node, entity, result) -> str | None:
            """The matched function's name, read from the node that holds it.

            From the live function where there is one: renaming a function and
            then looking at the results is the normal order of events, and the
            entity carries the name it had when the session ran.
            """

            match = node.get_result(result)
            if match is None:
                return None
            other = node_by_id(node, match.target.node_id)
            if other is None:
                return None
            func = other.get_entity_function(match.target.entity_id)
            if func is not None:
                return func.name
            info = other.get_entity(match.target.entity_id)
            return info.name if info is not None else None

        def perform_render(self, node, entity, context, result) -> None:
            try:
                self._render_result(node, entity, context, result)
            except Exception:
                # A session that cannot draw a result is still a useful session.
                log_error(traceback.format_exc(), "QBinDiff")

        def _render_result(self, node, entity, context, result) -> None:
            # Not named _render: the base class binds its own _render as the C
            # callback, and shadowing it means the core calls this instead,
            # with the arguments that dispatcher expects.
            from binaryninja import DisassemblySettings
            from binaryninja.similarity import DiffRenderer

            match = node.get_result(result)
            if match is None:
                return
            other = node_by_id(node, match.target.node_id)
            if other is None:
                return
            this_func = node.get_entity_function(entity)
            other_func = other.get_entity_function(match.target.entity_id)
            if this_func is None or other_func is None:
                return

            reference_ref = _ref(other.id, match.target.entity_id)
            current_ref = _ref(node.id, entity)
            level = requested_level(context)

            # Graphs are ours, so the tinting is per instruction.
            left_graph, right_graph = diff_graphs(other_func, this_func, level)
            context.add_flow_graph("Graph", left_graph, reference_ref)
            context.add_flow_graph("Graph", right_graph, current_ref)

            # The linear views stay with the core's renderer: it owns that
            # rendering, and address ranges are all it takes to annotate them.
            _status, rows = align.classify_pair(other_func, this_func, level.level)
            if not rows:
                return
            settings = DisassemblySettings()
            for func, side, entity_ref in (
                (other_func, "left", reference_ref),
                (this_func, "right", current_ref),
            ):
                renderer = DiffRenderer()
                for annotation in _annotations(rows, side=side):
                    renderer.add_range_annotation(annotation)
                linear = level.linear_object(func, settings)
                renderer.render_linear_view(context, "Linear", func.view, linear, entity_ref)

    class QBinDiffProviderType(SimilarityProviderType):
        name = PROVIDER_NAME
        description = PROVIDER_DESCRIPTION

        def get_default_settings(self):
            return default_settings()

        def create(self, settings):
            return QBinDiffProvider(self, settings)

    QBinDiffProviderType().register()
    log_info(f"Registered the {PROVIDER_NAME} similarity provider", "QBinDiff")
    return True


#: What a differing line is tinted, following the colours every binary diff
#: uses. Operand-only differences (`LineStatus.MINOR`) are deliberately absent:
#: they are most of the lines in a rebased binary, and colouring them washes out
#: the whole block and hides the one instruction that actually changed.
def _line_highlight(status):
    from binaryninja import HighlightColor, HighlightStandardColor

    color = {
        align.LineStatus.CHANGED: HighlightStandardColor.YellowHighlightColor,
        align.LineStatus.ADDED: HighlightStandardColor.GreenHighlightColor,
        align.LineStatus.REMOVED: HighlightStandardColor.RedHighlightColor,
    }.get(status)
    return HighlightColor(color) if color is not None else None


def _block_highlight(status):
    """Whole-node colour, for a block that exists on one side only."""

    from binaryninja import HighlightColor, HighlightStandardColor

    return HighlightColor(
        HighlightStandardColor.GreenHighlightColor
        if status is align.LineStatus.ADDED
        else HighlightStandardColor.RedHighlightColor
    )


#: Kept importable from here; the UI's view selector uses the same type.
RenderLevel = align.RenderLevel


def requested_level(context) -> RenderLevel:
    """The level the render header is asking for.

    The context calls it a *preference* a provider may ignore, but ignoring it
    renders disassembly whichever tab the reader picks — a view selector that
    does nothing. Anything unrecognised falls back to disassembly rather than
    failing to render.
    """

    try:
        wanted = context.preferred_view_type
    except Exception:
        return RenderLevel("Disassembly")

    name = getattr(wanted, "name", None)
    if name:
        # A language representation: HLIL underneath, that language on screen.
        return RenderLevel("HLIL", name)
    for level, (graph_type, _factory) in align.IL_LEVELS.items():
        if graph_type == wanted.view_type:
            return RenderLevel(level)
    return RenderLevel("Disassembly")


def _graph_for(func, level: RenderLevel):
    """Lay out a function's CFG at one level, indexed by block start."""

    from binaryninja import DisassemblySettings

    # The IL has to exist before a graph of it can be laid out; asking for one
    # that has not been generated yields a single "Loading..." node. A language
    # representation is rendered from HLIL and generated separately on top.
    align.ensure_rendering(func, level)
    graph = func.create_graph(graph_type=level.graph_type, settings=DisassemblySettings())
    # Nodes carry no lines until layout has run.
    graph.layout_and_wait()
    nodes = {}
    for node in graph.nodes:
        block = node.basic_block
        if block is not None:
            nodes[block.start] = node
    return graph, nodes


def _tint_lines(node, lines, statuses) -> None:
    """Tint the differing lines of one node, leaving the rest plain.

    ``lines`` must be the very list the statuses were computed from: the getter
    builds fresh objects on each call, so re-reading it here would tint lines
    that were graded from something else. Assigning back is what sends the
    highlights through the core.
    """

    if not lines or len(lines) != len(statuses):
        return
    touched = False
    for line, status in zip(lines, statuses, strict=True):
        color = _line_highlight(status)
        if color is not None:
            line.highlight = color
            touched = True
    if touched:
        node.lines = lines


def diff_graphs(left_func, right_func, level: RenderLevel | None = None):
    """One flow graph per side, with only the instructions that differ tinted.

    Built here rather than handed to `DiffRenderer` because that renders the
    range annotations itself, a whole basic block at a time: one changed
    instruction colours everything around it. A flow graph the provider builds
    carries `DisassemblyTextLine.highlight` per line, which the core stores and
    the widget renders — the same mechanism the plugin's own graph pane uses.
    """

    level = level or RenderLevel("Disassembly")
    # Blocks are paired on the underlying IL, whose structure a language
    # representation shares; only the lines drawn into them differ.
    alignment = align.align_blocks(left_func, right_func, level.level)
    left_graph, left_nodes = _graph_for(left_func, level)
    right_graph, right_nodes = _graph_for(right_func, level)

    # A block on one side only is a whole-block fact, so the node is filled.
    for nodes, statuses, kind in (
        (left_nodes, alignment.left_status, align.LineStatus.REMOVED),
        (right_nodes, alignment.right_status, align.LineStatus.ADDED),
    ):
        for addr, status in statuses.items():
            node = nodes.get(addr)
            if node is not None and status is align.BlockStatus.UNMATCHED:
                node.highlight = _block_highlight(kind)

    for left_addr, right_addr in alignment.left_to_right.items():
        if alignment.left_status.get(left_addr) is not align.BlockStatus.CHANGED:
            continue
        left_node, right_node = left_nodes.get(left_addr), right_nodes.get(right_addr)
        if left_node is None or right_node is None:
            continue
        # Align the nodes' own lines: a node prepends a symbol label that the
        # basic block's text does not have, and only for some blocks.
        left_lines, right_lines = left_node.lines, right_node.lines
        left_statuses, right_statuses = align.align_line_statuses(left_lines, right_lines)
        _tint_lines(left_node, left_lines, left_statuses)
        _tint_lines(right_node, right_lines, right_statuses)

    return left_graph, right_graph


def _ref(node_id: int, entity_id: int):
    from binaryninja.similarity import SimilarityEntityRef

    return SimilarityEntityRef(node_id, entity_id)


def _annotations(rows, side: str):
    """Address ranges to highlight on one side of a comparison.

    A line differs, is only on this side, or matches; the first two are what a
    reader wants pointed out. Ranges run to the next line's address, since a
    rendered line knows where it starts and not how long it is.
    """

    from binaryninja.similarity import SimilarityAnnotationType, SimilarityRangeAnnotation

    lines = [
        (row, getattr(row.left if side == "left" else row.right, "address", None)) for row in rows
    ]
    known = [address for _row, address in lines if address is not None]
    if not known:
        return []

    end_of_last = max(known) + 1
    annotations = []
    for index, (row, address) in enumerate(lines):
        if address is None or row.status in (
            align.LineStatus.EQUAL,
            align.LineStatus.GAP,
            align.LineStatus.COMMENT,
        ):
            continue
        following = next(
            (later for _row, later in lines[index + 1 :] if later is not None and later > address),
            end_of_last,
        )
        if row.status is align.LineStatus.MINOR:
            # An address or a register that moved is not what a reader is
            # looking for, and there are hundreds of them in a rebased binary.
            continue
        if row.status is align.LineStatus.CHANGED:
            kind = SimilarityAnnotationType.SimilarityAnnotationChanged
        elif side == "right":
            kind = SimilarityAnnotationType.SimilarityAnnotationAdded
        else:
            kind = SimilarityAnnotationType.SimilarityAnnotationRemoved
        annotations.append(SimilarityRangeAnnotation(address, following, kind))
    return annotations
