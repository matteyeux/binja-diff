# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Diffing one part of a container: a kext, a SEP module.

A kernelcache holds hundreds of kexts and a SEP image a couple of dozen
modules. Matching is quadratic, so diffing a whole kernelcache is not merely
slow, it is a different order of problem — one kext out of an iPhone cache
diffs in under a minute where the cache entire would not finish.

Two container formats, discovered the same way and scoped the same way, but
loaded differently:

* **Kernelcache** (``KCView``). Binary Ninja's own loader starts with no
  functions at all and maps a kext in on demand, through the public
  ``KernelCacheController``. So a region can be *made* to exist here.
* **SEP firmware** (sep-binja's ``SEP Firmware`` view). Also lazy, and its
  loader lives on the view's Python object, which ``binaryninja.load()`` does
  not hand back — only a generic ``BinaryView`` wrapper. sep-binja bridges that
  gap by publishing ``sep_binja_api`` in ``sys.modules``, which maps a wrapper
  back to the instance; with it, a module loads on demand exactly like a kext.
  Without it — an older sep-binja — only modules already mapped are offered,
  found by the sections they left behind.

Both name their pieces the same way on both sides of a diff, which is what
makes a region selectable before the second binary has even been opened.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from binaryninja import BinaryView, log_warn

#: Binary Ninja's native kernelcache view.
KERNELCACHE_VIEW = "KCView"

#: sep-binja's view (see SEPFirmwareView.name).
SEP_VIEW = "SEP Firmware"

#: sep-binja names every section it maps ``module:segment:section``.
_SEP_SEPARATOR = ":"

#: Where sep-binja publishes its loader (sep_api.REGISTRY_KEY). Looked up
#: rather than imported: the plugin's module name is whatever folder it was
#: installed under, and importing its package pulls in the UI half.
_SEP_API_KEY = "sep_binja_api"


def _sep_api():
    """sep-binja's loader API, or ``None`` if it is absent or too old."""

    api = sys.modules.get(_SEP_API_KEY)
    if api is None or getattr(api, "API_VERSION", 0) < 1:
        return None
    return api


@dataclass(frozen=True)
class Region:
    """One selectable part of a container.

    ``end`` is 0 when the extent is unknown, which is the kernelcache case:
    an image announces where its header sits and nothing more, so membership
    is asked of the controller rather than computed from a range.
    """

    name: str
    start: int
    end: int = 0
    #: Whether this region's code is mapped into the view already. A
    #: kernelcache can load one on demand; a SEP image cannot.
    loaded: bool = True

    @property
    def has_extent(self) -> bool:
        return self.end > self.start


def _kernelcache_controller(bv: BinaryView):
    """The controller for ``bv``, or ``None`` if this is not a kernelcache."""

    if bv.view_type != KERNELCACHE_VIEW:
        return None
    try:
        from binaryninja.kernelcache import KernelCacheController

        return KernelCacheController(bv)
    except Exception as exc:
        log_warn(f"Kernelcache support unavailable: {exc}", "QBinDiff")
        return None


def _kernelcache_regions(bv: BinaryView) -> list[Region]:
    controller = _kernelcache_controller(bv)
    if controller is None:
        return []
    loaded = {image.name for image in controller.loaded_images}
    return [
        Region(image.name, image.header_virtual_address, loaded=image.name in loaded)
        for image in controller.images
    ]


def _sep_regions(bv: BinaryView) -> list[Region]:
    """SEP modules: every one in the image when sep-binja can be asked, else
    the ones whose sections are already mapped."""

    extents: dict[str, tuple[int, int]] = {}
    for name, section in bv.sections.items():
        module, separator, _rest = name.partition(_SEP_SEPARATOR)
        if not separator or not module:
            continue
        low, high = extents.get(module, (section.start, section.end))
        extents[module] = (min(low, section.start), max(high, section.end))
    mapped = [
        Region(module, low, high)
        for module, (low, high) in sorted(extents.items(), key=lambda kv: kv[1])
    ]

    api = _sep_api()
    if api is None:
        return mapped

    # With the API, unmapped modules can be offered too: they load on demand.
    known = {region.name: region for region in mapped}
    regions = []
    for name in api.module_names(bv):
        regions.append(known.get(name) or Region(name, 0, 0, loaded=False))
    # Anything mapped but unknown to the loader still belongs in the list.
    regions.extend(
        region for name, region in known.items() if name not in {r.name for r in regions}
    )
    return regions


def available_regions(bv: BinaryView) -> list[Region]:
    """Parts of ``bv`` that can be diffed on their own. Empty for a plain binary."""

    if bv is None:
        return []
    if bv.view_type == KERNELCACHE_VIEW:
        return _kernelcache_regions(bv)
    if bv.view_type == SEP_VIEW:
        return _sep_regions(bv)
    return []


def regions_by_name(bv: BinaryView) -> dict[str, Region]:
    """Every region of ``bv``, keyed by name, enumerated once.

    Enumerating regions constructs a controller and walks every image, or
    re-scans the sections and asks sep-binja for its module list; a lookup per
    name over hundreds of kexts made that quadratic.
    """

    return {region.name: region for region in available_regions(bv)}


def find_region(bv: BinaryView, name: str) -> Region | None:
    """The region called ``name``, matched across the two sides of a diff.

    Names are what a kext and a SEP module have in common between two builds;
    addresses are not.
    """

    return regions_by_name(bv).get(name)


def ensure_named_loaded(bv: BinaryView, names: list[str]) -> list[str]:
    """Map the regions called ``names`` in, analyzing once. Returns the names
    ``bv`` has no region for.

    This is what a restored diff of a container runs before any of its
    addresses can resolve: the saved result names the parts, and the view just
    opened holds none of them.
    """

    if not names:
        return []
    known = regions_by_name(bv)
    found = [known[name] for name in names if name in known]
    missing = [name for name in names if name not in known]
    if found and not ensure_all_loaded(bv, found):
        log_warn(f"Could not load every part of {bv.file.filename}", "QBinDiff")
    return missing


def ensure_loaded(bv: BinaryView, region: Region) -> bool:
    """Map a region's code into the view if it is not there yet.

    A kernelcache always can. A SEP module can when sep-binja publishes its
    loader API; without it this reports False rather than pretending, and the
    module has to be loaded in sep-binja's own UI first.
    """

    return ensure_all_loaded(bv, [region])


def ensure_all_loaded(bv: BinaryView, regions: list[Region]) -> bool:
    """Map several regions in, settling the view once at the end.

    Deliberately not a loop over ``ensure_loaded``. Binary Ninja sweeps a view
    on its *first* completed analysis only; a region mapped after one has
    finished contributes just the functions recursive descent reaches from an
    entry point. Mapping a whole SEP image one module at a time, analyzing
    after each, finds 26959 functions where mapping all of them first and
    analyzing once finds 31499 — no error, no warning, 15% simply missing.
    """

    absent = [region for region in regions if not region.loaded]
    if not absent:
        return True

    api = _sep_api()
    if api is not None and bv.view_type == SEP_VIEW:
        loader = getattr(api, "load_modules", None)
        if loader is not None:
            return bool(loader(bv, [region.name for region in absent]))
        # An older sep-binja can only do one at a time, and pays for it.
        return all(api.load_module(bv, region.name) for region in absent)

    controller = _kernelcache_controller(bv)
    if controller is None:
        return False
    for region in absent:
        image = controller.get_image_with_name(region.name)
        if image is None:
            return False
        controller.apply_image(bv, image)
    bv.update_analysis_and_wait()
    return True


def _load_everything(bv: BinaryView) -> bool:
    """Map every part of a container in. Only SEP: a kernelcache holds hundreds
    of kexts, where this would be a multi-hour analysis and an unfinishable diff.
    """

    api = _sep_api()
    loader = getattr(api, "load_all_modules", None) if api is not None else None
    if bv.view_type == SEP_VIEW and loader is not None:
        return bool(loader(bv))
    return False


def mirror_loaded(
    primary_bv: BinaryView,
    secondary_bv: BinaryView,
    progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Load into the secondary whatever parts the primary has, by name.

    Both container views start empty and are filled on demand, so an unscoped
    diff has nothing to compare unless someone says what to map: the primary
    holds whatever its owner loaded, while the secondary was opened moments ago
    by the plugin and holds nothing at all. "Everything" therefore means every
    part the primary has, mirrored across — which for a curated primary is the
    handful its owner cared about, and for an untouched one is the whole file,
    where the file is a SEP image. A 256-kext cache is refused instead: matching
    is quadratic, and that diff does not finish.

    Returns the names mirrored; empty when this is an ordinary binary.
    """

    regions = available_regions(primary_bv)
    if not regions:
        return []

    loaded = [region for region in regions if region.loaded]
    if not loaded:
        # An untouched container: "everything" can only mean the whole file.
        if progress is not None:
            progress("everything")
        if not _load_everything(primary_bv):
            raise RuntimeError(
                "Nothing is loaded in the primary, so there is nothing to diff against. "
                "Choose one part to diff, or load what you want in the primary first."
                + (
                    " A whole kernelcache cannot be diffed in one go: matching is "
                    "quadratic in the number of functions, so it is the individual "
                    "kext that is worth diffing."
                    if primary_bv.view_type == KERNELCACHE_VIEW
                    else missing_region_hint(primary_bv)
                )
            )
        loaded = [region for region in available_regions(primary_bv) if region.loaded]

    if len(loaded) == len(regions):
        # Every part: one call maps them all and analyzes once, which is both
        # faster and more complete than mapping them one at a time.
        _load_everything(secondary_bv)

    # Enumerated once: a lookup per part over a 256-kext cache re-walked every
    # image each time.
    known = regions_by_name(secondary_bv)
    targets = []
    for region in loaded:
        if progress is not None:
            progress(region.name)
        target = known.get(region.name)
        if target is None:
            log_warn(f"{region.name} is not in the secondary; skipping it", "QBinDiff")
            continue
        targets.append(target)
    if targets and not ensure_all_loaded(secondary_bv, targets):
        log_warn("Could not load every part in the secondary", "QBinDiff")
    return [region.name for region in loaded]


def missing_region_hint(bv: BinaryView) -> str:
    """Why a name might be absent, when that depends on what is installed."""

    if bv.view_type == SEP_VIEW and _sep_api() is None:
        return (
            " This sep-binja does not publish its loader, so only modules already "
            "loaded in its UI can be diffed; update it, or load the module first."
        )
    return ""


def functions_in(bv: BinaryView, region: Region | None):
    """The functions belonging to ``region``, or all of them when unscoped."""

    if region is None:
        return list(bv.functions)

    controller = _kernelcache_controller(bv)
    if controller is not None:
        # An image knows its own extent; the view only knows where it starts.
        return [
            func
            for func in bv.functions
            if (containing := controller.get_image_containing(func.start)) is not None
            and containing.name == region.name
        ]

    if region.has_extent:
        return [func for func in bv.functions if region.start <= func.start < region.end]
    return list(bv.functions)
