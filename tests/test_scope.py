"""Cover scoping a diff to one part of a container.

A kernelcache holds hundreds of kexts and matching is quadratic, so the whole
container is not a slower diff, it is one that does not finish. The kernelcache
half needs Binary Ninja and lives in test_live.py; what is here is the SEP half,
which is derived from section names and so can be checked against sep-binja's
naming without loading anything.

    .venv/bin/python tests/test_scope.py
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

from binja_diff.core import scope  # noqa: E402


check = _bootstrap.check
_stubs = _bootstrap.stubs()


def sep_view():
    """A SEP view with two modules loaded, named the way sep-binja names them."""

    bv = _stubs.BinaryView("/tmp/sep-firmware.bin")
    bv.view_type = scope.SEP_VIEW
    for name, start, end in (
        ("SEPOS:HEADER", 0x1000, 0x1100),
        ("SEPOS:__TEXT:__text", 0x1100, 0x4000),
        ("SEPOS:__DATA:__data", 0x4000, 0x4800),
        ("SEPD:HEADER", 0x8000, 0x8100),
        ("SEPD:__TEXT:__text", 0x8100, 0x9000),
        (".synthetic_builtins", 0x100, 0x130),
    ):
        bv.sections[name] = _stubs.Section(name, start, end)
    return bv


def test_sep_modules_are_discovered():
    print("SEP modules come from the sections sep-binja maps")
    bv = sep_view()
    regions = scope.available_regions(bv)
    check("one region per module", [r.name for r in regions] == ["SEPOS", "SEPD"], f"{regions}")

    sepos, sepd = regions
    check("extent spans every section", (sepos.start, sepos.end) == (0x1000, 0x4800), f"{sepos}")
    check("and the next module is separate", (sepd.start, sepd.end) == (0x8000, 0x9000), f"{sepd}")
    check("both count as loaded", all(r.loaded for r in regions))
    check("sections with no module prefix are ignored", all(":" not in r.name for r in regions))


def test_regions_are_matched_by_name():
    print("a region is found on the other side by name, not by address")
    bv = sep_view()
    check("found", scope.find_region(bv, "SEPD") is not None)
    check("missing is None", scope.find_region(bv, "nope") is None)


def test_functions_are_filtered_by_extent():
    print("only the chosen module's functions are diffed")
    bv = sep_view()
    bv.functions = [
        _stubs.Function(bv, 0x1200, "sepos_one", []),
        _stubs.Function(bv, 0x3000, "sepos_two", []),
        _stubs.Function(bv, 0x8200, "sepd_one", []),
    ]
    sepos = scope.find_region(bv, "SEPOS")
    names = [f.name for f in scope.functions_in(bv, sepos)]
    check("kept its own", names == ["sepos_one", "sepos_two"], f"got {names}")
    check("unscoped means everything", len(scope.functions_in(bv, None)) == 3)


def test_plain_binaries_offer_nothing():
    print("an ordinary binary has no parts to choose from")
    bv = _stubs.BinaryView("/bin/ls")
    check("no regions", scope.available_regions(bv) == [])
    check("and nothing to load", scope.ensure_loaded(bv, scope.Region("x", 0, 0)) is True)


class FakeSepApi:
    """Stands in for sep-binja's sep_api, published under its well-known key."""

    def __init__(self, names, version=3):
        self.API_VERSION = version
        self.names = names
        self.loaded: list[str] = []
        #: One entry per batch mapped in a single call, so a test can tell a
        #: batched load from a loop that settles the view every time.
        self.batches: list[list[str]] = []
        if version >= 2:
            # An older sep-binja does not have these attributes at all, which
            # is what a consumer has to cope with.
            self.load_all_modules = self._load_all_modules
        if version >= 3:
            self.load_modules = self._load_modules

    def module_names(self, bv):
        return list(self.names)

    def _map(self, bv, name):
        """Mapping a module is what makes it visible as a loaded region."""

        section = f"{name}:__TEXT:__text"
        start = 0x10000 + 0x1000 * self.names.index(name)
        bv.sections[section] = _stubs.Section(section, start, start + 0x800)

    def load_module(self, bv, name):
        if name not in self.names:
            return False
        self.loaded.append(name)
        self.batches.append([name])
        self._map(bv, name)
        return True

    def _load_modules(self, bv, names):
        names = list(names)
        if any(name not in self.names for name in names):
            return False
        self.loaded.extend(names)
        self.batches.append(names)
        for name in names:
            self._map(bv, name)
        return True

    def _load_all_modules(self, bv):
        return self._load_modules(bv, self.names)


def with_api(api):
    """Publish a fake API the way sep-binja publishes the real one."""

    import sys

    if api is None:
        sys.modules.pop(scope._SEP_API_KEY, None)
    else:
        sys.modules[scope._SEP_API_KEY] = api


def test_sep_api_offers_modules_that_are_not_mapped_yet():
    """With sep-binja's API, an unloaded module can be offered and loaded.

    Without it only mapped modules are visible, because the loader lives on a
    Python object binaryninja.load() does not hand back.
    """

    print("sep-binja's API exposes the modules that are not loaded yet")
    bv = sep_view()
    api = FakeSepApi(["SEPBOOT", "SEPOS", "SEPD", "xART"])
    with_api(api)
    try:
        regions = scope.available_regions(bv)
        check("every module is offered", [r.name for r in regions] == api.names, f"{regions}")
        loaded = {r.name: r.loaded for r in regions}
        check("mapped ones are marked loaded", loaded["SEPOS"] and loaded["SEPD"])
        check("and the rest are not", not loaded["SEPBOOT"] and not loaded["xART"])

        absent = scope.find_region(bv, "xART")
        check("an unmapped module loads on demand", scope.ensure_loaded(bv, absent))
        check("through the API", api.loaded == ["xART"], f"{api.loaded}")

        check(
            "a mapped one needs no loading", scope.ensure_loaded(bv, scope.find_region(bv, "SEPD"))
        )
        check("so the API is not called again", api.loaded == ["xART"], f"{api.loaded}")
        check("no hint is needed", scope.missing_region_hint(bv) == "")
    finally:
        with_api(None)


def test_without_the_api_only_mapped_modules_appear():
    print("an older sep-binja still works, with less")
    bv = sep_view()
    with_api(None)
    names = [r.name for r in scope.available_regions(bv)]
    check("only what is mapped", names == ["SEPOS", "SEPD"], f"{names}")
    check("and the reason is explained", "sep-binja" in scope.missing_region_hint(bv))


def test_an_unloaded_sep_module_cannot_be_conjured():
    """sep-binja's loader lives on a Python object binaryninja.load() does not
    return, so a module that is not mapped cannot be mapped from here."""

    print("an unloaded SEP module is reported, not faked")
    bv = sep_view()
    absent = scope.Region("xART", 0, 0, loaded=False)
    check("says so", scope.ensure_loaded(bv, absent) is False)


def test_the_secondary_mirrors_whatever_the_primary_has():
    """An unscoped diff of a container: the secondary was opened seconds ago and
    holds nothing, so "everything" means everything the primary holds."""

    print("an unscoped container diff mirrors the primary's parts across")
    primary = sep_view()
    secondary = _stubs.BinaryView("/tmp/sep-firmware-2.bin")
    secondary.view_type = scope.SEP_VIEW
    api = FakeSepApi(["SEPBOOT", "SEPOS", "SEPD", "xART"])
    with_api(api)
    try:
        seen: list[str] = []
        mirrored = scope.mirror_loaded(primary, secondary, progress=seen.append)
        check("only the primary's own parts", mirrored == ["SEPOS", "SEPD"], f"{mirrored}")
        check(
            "and they are loaded on the other side",
            api.loaded == ["SEPOS", "SEPD"],
            f"{api.loaded}",
        )
        check("each one reported", seen == mirrored, f"{seen}")
        # Binary Ninja sweeps a view once, so parts mapped after an analysis
        # has completed lose most of their functions: they go in together.
        check("mapped in one pass", api.batches == [["SEPOS", "SEPD"]], f"{api.batches}")
    finally:
        with_api(None)


def test_an_untouched_sep_image_diffs_whole():
    """Nothing loaded on either side is the plain "diff these two files" case,
    and a SEP image is small enough to mean it literally."""

    print("an untouched SEP image loads in full on both sides")
    api = FakeSepApi(["SEPBOOT", "SEPOS", "SEPD", "xART"])
    with_api(api)
    try:
        primary = _stubs.BinaryView("/tmp/sep-a.bin")
        secondary = _stubs.BinaryView("/tmp/sep-b.bin")
        for bv in (primary, secondary):
            bv.view_type = scope.SEP_VIEW
        mirrored = scope.mirror_loaded(primary, secondary)
        check("every module is diffed", mirrored == api.names, f"{mirrored}")
        check(
            "and both sides hold them",
            all(scope.find_region(secondary, name).loaded for name in api.names),
        )
    finally:
        with_api(None)


def test_an_older_sep_binja_still_loads_one_at_a_time():
    """Without the batched loader the parts still arrive, just less completely.
    Reporting nothing would be worse than the older behaviour."""

    print("an older sep-binja falls back to one module at a time")
    primary = sep_view()
    secondary = _stubs.BinaryView("/tmp/sep-old.bin")
    secondary.view_type = scope.SEP_VIEW
    # More modules than the primary has mapped, so this is the mirror path and
    # not the "everything" shortcut, which batches on its own.
    api = FakeSepApi(["SEPBOOT", "SEPOS", "SEPD", "xART"], version=2)
    with_api(api)
    try:
        mirrored = scope.mirror_loaded(primary, secondary)
        check("still mirrored", mirrored == ["SEPOS", "SEPD"], f"{mirrored}")
        check("one call each", api.batches == [["SEPOS"], ["SEPD"]], f"{api.batches}")
    finally:
        with_api(None)


def test_an_untouched_container_that_cannot_be_loaded_is_refused():
    print("a container that cannot be loaded in full says so")
    api = FakeSepApi(["SEPOS"], version=1)
    with_api(api)
    try:
        primary = _stubs.BinaryView("/tmp/sep-a.bin")
        primary.view_type = scope.SEP_VIEW
        try:
            scope.mirror_loaded(primary, _stubs.BinaryView("/tmp/sep-b.bin"))
            check("refused", False)
        except RuntimeError as exc:
            check("refused, with a way out", "Choose one part" in str(exc), str(exc))
    finally:
        with_api(None)

    kc = _stubs.BinaryView("/tmp/kernelcache")
    kc.view_type = scope.KERNELCACHE_VIEW
    # The real controller needs Binary Ninja; what matters here is a container
    # holding parts that none of the loaders above can map.
    original = scope._kernelcache_regions
    scope._kernelcache_regions = lambda bv: [
        scope.Region(f"com.apple.kext.{i}", 0x1000 * i, loaded=False) for i in range(3)
    ]
    try:
        scope.mirror_loaded(kc, _stubs.BinaryView("/tmp/kernelcache2"))
        check("a kernelcache is refused", False)
    except RuntimeError as exc:
        check("a kernelcache explains why", "quadratic" in str(exc), str(exc))
    finally:
        scope._kernelcache_regions = original


def test_a_plain_binary_is_left_alone():
    print("mirroring is a no-op for an ordinary binary")
    plain = _stubs.BinaryView("/bin/ls")
    check("nothing to mirror", scope.mirror_loaded(plain, _stubs.BinaryView("/bin/dir")) == [])


def _function(bv, start: int, name: str):
    """One small function at ``start``, enough for QBinDiff to extract features from."""

    TT = _stubs.InstructionTextTokenType
    tokens = [
        _stubs.Token(TT.InstructionToken, "mov"),
        _stubs.Token(TT.TextToken, " "),
        _stubs.Token(TT.RegisterToken, "x0", 0),
        _stubs.Token(TT.OperandSeparatorToken, ", "),
        _stubs.Token(TT.IntegerToken, hex(start & 0xFF), start & 0xFF),
    ]
    ret = [_stubs.Token(TT.InstructionToken, "ret")]
    block = _stubs.BasicBlock(start, [(tokens, 4), (ret, 4)])
    function = _stubs.Function(bv, start, name, [block])
    bv.functions.append(function)
    return function


def test_a_scoped_diff_is_checked_and_sized_on_the_part():
    """The view's function count says nothing about the part being diffed.

    Both the "contains no functions" guard and the size-based sparsity used
    to read the whole view: a part with no code passed the guard and a small
    kext inside a big view got the large-diff settings.
    """

    print("a scoped diff is checked and sized on the part, not the view")
    from binja_diff.core import engine

    primary, secondary = sep_view(), sep_view()
    # Code in SEPOS only; SEPD is mapped but empty on both sides.
    for bv in (primary, secondary):
        _function(bv, 0x1100, "a")
        _function(bv, 0x1200, "b")

    try:
        engine.run_diff(primary, secondary, region_name="SEPD")
    except RuntimeError as exc:
        check("an empty part is refused although the view has code", "SEPD" in str(exc), str(exc))
    else:
        check("an empty part is refused although the view has code", False, "no error")

    sized: list[tuple[int, int]] = []
    original = engine.scale_options_for_size

    def spy(options, primary_count, secondary_count):
        sized.append((primary_count, secondary_count))
        return original(options, primary_count, secondary_count)

    engine.scale_options_for_size = spy
    try:
        result = engine.run_diff(primary, secondary, region_name="SEPOS")
    finally:
        engine.scale_options_for_size = original
    check("the scoped diff ran", result is not None)
    check("sized on the part's functions", sized == [(2, 2)], f"{sized}")
    if result is not None:
        check("the result records its scope", result.scope == ["SEPOS"], f"{result.scope}")

    with_api(None)
    plain = _stubs.BinaryView("/tmp/plain.bin")
    plain_other = _stubs.BinaryView("/tmp/plain2.bin")
    for bv in (plain, plain_other):
        _function(bv, 0x1000, "f")
    result = engine.run_diff(plain, plain_other)
    check("a plain binary has no scope", result is not None and result.scope == [], f"{result}")


def test_named_parts_are_loaded_in_one_batch():
    print("restoring a scope maps the named parts in, once")
    api = FakeSepApi(["SEPOS", "SEPD", "AESS"])
    with_api(api)
    try:
        bv = _stubs.BinaryView("/tmp/sep-firmware.bin")
        bv.view_type = scope.SEP_VIEW
        missing = scope.ensure_named_loaded(bv, ["SEPD", "AESS", "NOPE"])
        check("the unknown name is reported", missing == ["NOPE"], f"{missing}")
        check(
            "the known ones are mapped in one call",
            api.batches == [["SEPD", "AESS"]],
            f"{api.batches}",
        )
        check("a loaded part is loaded", scope.find_region(bv, "SEPD").loaded)
        check("nothing to load is fine", scope.ensure_named_loaded(bv, []) == [])
    finally:
        with_api(None)


def main() -> int:
    return _bootstrap.run(
        [
            test_sep_modules_are_discovered,
            test_regions_are_matched_by_name,
            test_functions_are_filtered_by_extent,
            test_plain_binaries_offer_nothing,
            test_sep_api_offers_modules_that_are_not_mapped_yet,
            test_without_the_api_only_mapped_modules_appear,
            test_an_unloaded_sep_module_cannot_be_conjured,
            test_the_secondary_mirrors_whatever_the_primary_has,
            test_an_untouched_sep_image_diffs_whole,
            test_an_older_sep_binja_still_loads_one_at_a_time,
            test_an_untouched_container_that_cannot_be_loaded_is_refused,
            test_a_plain_binary_is_left_alone,
            test_a_scoped_diff_is_checked_and_sized_on_the_part,
            test_named_parts_are_loaded_in_one_batch,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
