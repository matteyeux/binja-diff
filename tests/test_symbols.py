"""Cover porting function names from one side of a diff to the other.

Every rename here lands in somebody's database, so the tests are mostly about
what porting refuses to do: overwrite work that is already there, propagate a
placeholder name, or act on a weak match.

    .venv/bin/python tests/test_symbols.py
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

from binja_diff.core import symbols  # noqa: E402
from binja_diff.core.engine import DiffResult, FunctionRef, MatchRecord  # noqa: E402


check = _bootstrap.check


def make_view(name: str, functions: dict[int, str]):
    stubs = _bootstrap.stubs()
    bv = stubs.BinaryView(name)
    bv.functions = [stubs.Function(bv, addr, fname, []) for addr, fname in functions.items()]
    return bv


def make_result(primary: dict[int, str], secondary: dict[int, str], similarity: float = 1.0):
    """Pair the two sides up in address order."""

    primary_bv = make_view("/tmp/new.bin", primary)
    secondary_bv = make_view("/tmp/old.bin", secondary)
    matches = [
        MatchRecord(
            FunctionRef(p_addr, primary[p_addr]),
            FunctionRef(s_addr, secondary[s_addr]),
            similarity,
            similarity,
        )
        for p_addr, s_addr in zip(sorted(primary), sorted(secondary), strict=True)
    ]
    return DiffResult(
        primary_bv=primary_bv,
        secondary_bv=secondary_bv,
        similarity=similarity,
        matches=matches,
    )


def test_ports_names_into_the_primary():
    print("names arrive from the symbolicated side")
    result = make_result(
        {0x1000: "sub_1000", 0x2000: "sub_2000"}, {0x8000: "aes_init", 0x9000: "aes_run"}
    )

    plan = symbols.plan_port(result, symbols.PortOptions())
    check("both pairs planned", plan.count == 2, plan.summary())

    applied = symbols.apply_port(result.primary_bv, plan)
    check("both applied", applied == 2, f"got {applied}")
    names = [f.name for f in result.primary_bv.functions]
    check("primary renamed", names == ["aes_init", "aes_run"], f"got {names}")
    check(
        "secondary untouched",
        [f.name for f in result.secondary_bv.functions] == ["aes_init", "aes_run"],
    )


def test_direction_is_respected():
    print("porting the other way writes the other view")
    result = make_result({0x1000: "parse_header"}, {0x8000: "sub_8000"})

    plan = symbols.plan_port(
        result, symbols.PortOptions(direction=symbols.PortDirection.TO_SECONDARY)
    )
    symbols.apply_port(result.secondary_bv, plan)
    check("secondary renamed", result.secondary_bv.functions[0].name == "parse_header")
    check("primary untouched", result.primary_bv.functions[0].name == "parse_header")


def test_direction_survives_a_plain_string():
    """Qt hands a str-valued enum back through QVariant as a plain str.

    Identity comparison silently took the other branch there, so a port aimed
    at the primary rewrote the secondary instead. Values, not identity.
    """

    print("a direction that lost its enum type still routes correctly")
    result = make_result({0x1000: "sub_1000"}, {0x8000: "aes_init"})
    options = symbols.PortOptions(direction=str(symbols.PortDirection.TO_PRIMARY.value))

    check(
        "target is still the primary",
        symbols.target_view(result, options.direction) is result.primary_bv,
    )
    plan = symbols.plan_port(result, options)
    check("planned against the primary", plan.count == 1, plan.summary())
    check("and it is the primary's address", plan.renames[0].addr == 0x1000)


def test_refuses_to_propagate_placeholders():
    print("a sub_ name is not a symbol")
    result = make_result({0x1000: "sub_1000"}, {0x8000: "sub_8000"})

    plan = symbols.plan_port(result)
    check("nothing to port", plan.count == 0, plan.summary())
    check(
        "reported as such",
        plan.skipped.get(symbols.SkipReason.NO_SYMBOL) == 1,
        f"{plan.skipped}",
    )


def test_existing_names_are_kept():
    print("work already in the target is not overwritten")
    result = make_result({0x1000: "my_own_name"}, {0x8000: "their_name"})

    plan = symbols.plan_port(result)
    check("skipped by default", plan.count == 0, plan.summary())
    check("reason recorded", plan.skipped.get(symbols.SkipReason.ALREADY_NAMED) == 1)

    plan = symbols.plan_port(result, symbols.PortOptions(overwrite=True))
    check("overwrite opts in", plan.count == 1, plan.summary())
    check("keeps the old name for the record", plan.renames[0].old_name == "my_own_name")

    symbols.apply_port(result.primary_bv, plan)
    check("renamed", result.primary_bv.functions[0].name == "their_name")


def test_weak_matches_are_left_alone():
    print("a weak match does not get to name anything")
    result = make_result({0x1000: "sub_1000"}, {0x8000: "crypto_verify"}, similarity=0.42)

    plan = symbols.plan_port(result)
    check("skipped", plan.count == 0, plan.summary())
    check("reason recorded", plan.skipped.get(symbols.SkipReason.BELOW_THRESHOLD) == 1)

    plan = symbols.plan_port(result, symbols.PortOptions(min_similarity=0.4))
    check("threshold is the user's call", plan.count == 1, plan.summary())


def test_identical_names_are_not_rewritten():
    print("names that already agree are left alone")
    result = make_result({0x1000: "shared"}, {0x8000: "shared"})
    plan = symbols.plan_port(result)
    check("nothing to do", plan.count == 0, plan.summary())
    check("reason recorded", plan.skipped.get(symbols.SkipReason.SAME_NAME) == 1)


def test_failure_reverts_the_batch():
    print("a failure part-way through leaves nothing renamed")
    result = make_result(
        {0x1000: "sub_1000", 0x2000: "sub_2000"}, {0x8000: "first", 0x9000: "second"}
    )
    plan = symbols.plan_port(result)

    # Blow up on the second rename, after the first has been applied.
    target = result.primary_bv
    original = target.get_function_at

    def exploding(addr: int):
        if addr == 0x2000:
            raise RuntimeError("boom")
        return original(addr)

    target.get_function_at = exploding
    try:
        symbols.apply_port(target, plan)
    except RuntimeError:
        check("the error propagates", True)
    else:
        check("the error propagates", False, "no exception")
    finally:
        target.get_function_at = original

    names = [f.name for f in target.functions]
    check("first rename rolled back too", names == ["sub_1000", "sub_2000"], f"got {names}")


def test_cancel_keeps_what_was_applied():
    print("cancelling stops, it does not roll back")
    result = make_result(
        {0x1000: "sub_1000", 0x2000: "sub_2000"}, {0x8000: "first", 0x9000: "second"}
    )
    plan = symbols.plan_port(result)

    seen = []

    def cancel_after_one() -> bool:
        seen.append(1)
        return len(seen) > 1

    applied = symbols.apply_port(result.primary_bv, plan, cancelled=cancel_after_one)
    check("stopped early", applied == 1, f"got {applied}")
    names = [f.name for f in result.primary_bv.functions]
    check("what was done is kept", names == ["first", "sub_2000"], f"got {names}")


def test_refresh_names_updates_the_result():
    print("the result catches up with the views")
    result = make_result({0x1000: "sub_1000"}, {0x8000: "aes_init"})
    symbols.apply_port(result.primary_bv, symbols.plan_port(result))

    check("record still stale", result.matches[0].primary.name == "sub_1000")
    symbols.refresh_names(result)
    check("record refreshed", result.matches[0].primary.name == "aes_init")
    check(
        "index rebuilt onto the new record",
        result.by_primary[0x1000].primary.name == "aes_init",
    )


def test_missing_functions_are_reported():
    print("addresses that no longer resolve are counted, not crashed on")
    result = make_result({0x1000: "sub_1000"}, {0x8000: "aes_init"})
    result.primary_bv.functions = []

    plan = symbols.plan_port(result)
    check("skipped", plan.count == 0, plan.summary())
    check("reason recorded", plan.skipped.get(symbols.SkipReason.MISSING) == 1)


def test_summary_reads_sensibly():
    print("summary")
    result = make_result(
        {0x1000: "sub_1000", 0x2000: "mine"}, {0x8000: "aes_init", 0x9000: "theirs"}
    )
    summary = symbols.plan_port(result).summary()
    check("counts the renames", summary.startswith("1 function(s) to rename"), summary)
    check("explains the skip", "already has a name" in summary, summary)


def test_only_the_selected_pairs_are_planned():
    """The context menu ports what is selected, not the whole table."""

    print("a selection restricts the plan to those pairs")
    result = make_result(
        {0x1000: "sub_1000", 0x2000: "sub_2000", 0x3000: "sub_3000"},
        {0x8000: "aes_init", 0x9000: "aes_run", 0xA000: "aes_done"},
    )

    plan = symbols.plan_port(result, symbols.PortOptions(), [0x2000])
    check("one pair planned", plan.count == 1, plan.summary())
    check("and it is the chosen one", plan.renames[0].addr == 0x2000, f"{plan.renames}")
    check("nothing counted as skipped", not plan.skipped, f"{plan.skipped}")

    both = symbols.plan_port(result, symbols.PortOptions(), [0x1000, 0x3000])
    check("several at once", [r.addr for r in both.renames] == [0x1000, 0x3000], f"{both.renames}")

    # Keyed on the primary address either way: that is what a table row is,
    # and it is what the context menu has to hand.
    named = make_result(
        {0x1000: "aes_init", 0x2000: "sub_2000"}, {0x8000: "sub_8000", 0x9000: "sub_9000"}
    )
    outward = symbols.plan_port(
        named, symbols.PortOptions(direction=symbols.PortDirection.TO_SECONDARY), [0x1000]
    )
    check("the other direction too", outward.count == 1, outward.summary())
    check("selected by primary address", outward.renames[0].addr == 0x8000, f"{outward.renames}")
    check("but written to the secondary", named.secondary_bv.get_function_at(0x8000) is not None)

    check("an empty selection ports nothing", symbols.plan_port(result, None, []).count == 0)
    check("and None still means everything", symbols.plan_port(result, None, None).count == 3)


def test_a_selected_pair_ignores_the_similarity_floor():
    """The floor guards a whole-table port from acting on noise. Someone who
    right-clicked one row has better evidence than the threshold does."""

    print("an explicit selection is not filtered by similarity")
    result = make_result({0x1000: "sub_1000"}, {0x8000: "aes_init"}, similarity=0.2)

    default = symbols.plan_port(result, symbols.PortOptions(), [0x1000])
    check("the floor would skip it", default.count == 0, default.summary())
    check("and says why", symbols.SkipReason.BELOW_THRESHOLD in default.skipped)

    chosen = symbols.plan_port(result, symbols.PortOptions(min_similarity=0.0), [0x1000])
    check("without it the rename stands", chosen.count == 1, chosen.summary())


def main() -> int:
    return _bootstrap.run(
        [
            test_ports_names_into_the_primary,
            test_only_the_selected_pairs_are_planned,
            test_a_selected_pair_ignores_the_similarity_floor,
            test_direction_is_respected,
            test_direction_survives_a_plain_string,
            test_refuses_to_propagate_placeholders,
            test_existing_names_are_kept,
            test_weak_matches_are_left_alone,
            test_identical_names_are_not_rewritten,
            test_failure_reverts_the_batch,
            test_cancel_keeps_what_was_applied,
            test_refresh_names_updates_the_result,
            test_missing_functions_are_reported,
            test_summary_reads_sensibly,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
