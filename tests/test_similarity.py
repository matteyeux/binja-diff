"""Cover the QBinDiff similarity provider's adapter logic.

The provider itself only runs inside a Binary Similarity session, which needs
the Ultimate edition; what is checked here is everything around that — the
score conversion a resolver thresholds on, reading settings that may not carry
our schema, the entity lookup, and that the module keeps quiet on a Binary
Ninja with no similarity API at all.

    .venv/bin/python binja_diff/tests/test_similarity.py
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

from binja_diff.core import similarity  # noqa: E402
from binja_diff.core.engine import DiffOptions  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        check.failures += 1


check.failures = 0


class FakeEntityInfo:
    def __init__(self, address: int, name: str):
        self.address = address
        self.name = name


class FakeNode:
    """The slice of SimilaritySessionNode the provider actually uses."""

    def __init__(self, node_id: int, functions: dict[int, str]):
        self.id = node_id
        self._entities = dict(enumerate(functions.items(), start=1))
        self.incoming_nodes: list = []
        self.outgoing_nodes: list = []

    @property
    def entities(self):
        return list(self._entities)

    def get_entity(self, entity_id):
        pair = self._entities.get(entity_id)
        return FakeEntityInfo(*pair) if pair else None

    def create_entity(self, info):
        entity_id = max(self._entities, default=0) + 1
        self._entities[entity_id] = (info.address, info.name)
        return entity_id


class FakeSettings:
    """Settings that hold some of our keys and raise for the rest.

    A session hands back whatever schema it was created with, so reading has to
    survive a key that is simply not there.
    """

    def __init__(self, values: dict):
        self._values = values

    def _get(self, key):
        if key not in self._values:
            raise KeyError(key)
        return self._values[key]

    get_double = _get
    get_string = _get
    get_bool = _get


def test_scores_span_the_byte_range():
    """A result carries 0..255 and a resolver thresholds on it, so the ends
    have to be exact rather than nearly."""

    print("a 0..1 ratio becomes the 0..255 a result carries")
    check("nothing in common", similarity._score(0.0) == 0)
    check("identical", similarity._score(1.0) == 255)
    check("half", similarity._score(0.5) == 128, f"{similarity._score(0.5)}")
    check("out of range is clamped", similarity._score(1.5) == 255 and similarity._score(-1) == 0)


def test_settings_fall_back_to_the_defaults():
    print("settings are read defensively, key by key")
    options, by_instructions = similarity.options_from(None)
    check("no settings at all is fine", options.tradeoff == DiffOptions.tradeoff)
    check("and scoring by instructions is the default", by_instructions)

    partial = FakeSettings({"qbindiff.tradeoff": 0.5, "qbindiff.scoreByInstructions": False})
    options, by_instructions = similarity.options_from(partial)
    check("what is there is read", options.tradeoff == 0.5, f"{options.tradeoff}")
    check("what is missing keeps its default", options.sparsity_ratio == DiffOptions.sparsity_ratio)
    check("including the distance", options.distance == DiffOptions.distance)
    check("and a false setting is honoured", by_instructions is False)


def test_entities_are_found_by_address():
    print("matches are attached to entities by address")
    node = FakeNode(1, {0x1000: "main", 0x2000: "helper"})
    index = similarity.entity_index(node)
    check("one entry per entity", sorted(index) == [0x1000, 0x2000], f"{index}")
    check("ids point back", node.get_entity(index[0x2000]).name == "helper")

    created = node.create_entity(FakeEntityInfo(0x3000, "late"))
    check("a new entity is reachable", similarity.entity_index(node)[0x3000] == created)


def test_the_other_side_is_found_through_the_graph():
    """A result points into another node, and the provider is handed only one.
    Holding a reference instead would keep that binary's database open."""

    print("the neighbouring node is found by id")
    left, right = FakeNode(1, {0x1000: "main"}), FakeNode(2, {0x8000: "main"})
    right.incoming_nodes = [left]
    left.outgoing_nodes = [right]

    check("itself", similarity.node_by_id(right, 2) is right)
    check("its neighbour", similarity.node_by_id(right, 1) is left)
    check("and the other way", similarity.node_by_id(left, 2) is right)
    check("a stranger is None", similarity.node_by_id(right, 99) is None)


def test_registration_is_skipped_without_the_api():
    """Binary Similarity is Ultimate-only and recent; on anything else the
    plugin has to load exactly as it did before."""

    print("no similarity API means no provider, and no error")
    available, reason = similarity.is_available()
    check("the stub has no similarity module", available is False)
    check("and says why", "similarity" in reason, reason)
    check("registering is a quiet no-op", similarity.register() is False)


def main() -> int:
    for test in (
        test_scores_span_the_byte_range,
        test_settings_fall_back_to_the_defaults,
        test_entities_are_found_by_address,
        test_the_other_side_is_found_through_the_graph,
        test_registration_is_skipped_without_the_api,
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
