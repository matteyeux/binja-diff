# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Binary Ninja plugin entry point.

Kept deliberately thin: it registers the view type and menu action, and does
nothing that would fail at startup. Heavy imports (qbindiff, Qt widgets) are
deferred so a missing dependency produces an actionable message instead of a
broken plugin load.
"""

from __future__ import annotations

__version__ = "1.0.0"

from binaryninja import core_ui_enabled, log_error, log_warn


def dependency_error() -> str | None:
    """Why QBinDiff is unusable, or ``None`` if it imports cleanly.

    The compiled extension is probed separately: a version-mismatched wheel
    imports the pure-Python package fine and only fails deeper in, which is
    confusing to diagnose from a bare "not installed" message.
    """

    try:
        import qbindiff
    except Exception as exc:
        return f"cannot import qbindiff: {exc.__class__.__name__}: {exc}"

    location = getattr(qbindiff, "__file__", None) or list(getattr(qbindiff, "__path__", []))

    try:
        from qbindiff.matcher.squares import find_squares  # noqa: F401
        from qbindiff.passes.fast_metrics import sparse_haussmann  # noqa: F401
    except Exception as exc:
        hint = (
            "the wheel was built for a different Python version"
            if getattr(qbindiff, "__file__", None)
            else "qbindiff resolved to a namespace package, so a directory named "
            "'qbindiff' on sys.path is shadowing the installed one"
        )
        return (
            f"qbindiff imported from {location} but is incomplete "
            f"({exc.__class__.__name__}: {exc}); {hint}"
        )
    return None


def dependencies_available() -> bool:
    """Whether QBinDiff (including its compiled extensions) can be imported."""

    return dependency_error() is None


def _install_hint(reason: str) -> str:
    import sys

    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"binja-diff could not load QBinDiff: {reason}\n"
        f"Binary Ninja is running Python {version}, so QBinDiff must be installed "
        f"for that exact version:\n"
        f"    python{version} -m venv ~/.binja-diff-venv\n"
        f"    ~/.binja-diff-venv/bin/pip install qbindiff\n"
        f"Then set Settings > Python > Python Virtual Environment Site-Packages to:\n"
        f"    ~/.binja-diff-venv/lib/python{version}/site-packages\n"
        f"and restart Binary Ninja. Current sys.path[0:3]: {sys.path[:3]}"
    )


#: Kept alive for the process lifetime; the registry does not own it on the
#: Python side.
_view_type = None


def _register_ui() -> None:
    global _view_type

    from binaryninjaui import Menu, UIAction, UIActionHandler, ViewType

    from .ui.diffview import DiffViewType

    _view_type = DiffViewType()
    ViewType.registerViewType(_view_type)

    def open_diff(context) -> None:
        frame = context.context.getCurrentViewFrame() if context.context else None
        if frame is None:
            return
        # getCurrentView() is "<ViewType>:<DataType>"; keep the data type so the
        # frame stays on the same BinaryView.
        current = frame.getCurrentView()
        data_type = current.split(":")[-1] if ":" in current else "Raw"
        if not frame.setViewType(f"Diff:{data_type}"):
            log_warn(f"Could not switch to the Diff view for data type {data_type}", "binja-diff")

    def can_open(context) -> bool:
        return bool(context.context and context.binaryView)

    UIAction.registerAction("Binary Diff")
    UIActionHandler.globalActions().bindAction("Binary Diff", UIAction(open_diff, can_open))
    Menu.mainMenu("Plugins").addAction("Binary Diff", "Binary Diff")


def _register_similarity_provider() -> None:
    """Offer QBinDiff to Binary Similarity sessions, headless included.

    Separate from the UI registration on purpose: a provider is driven by the
    session API, which works in a headless script as well as in the sidebar,
    and it is skipped quietly on a Binary Ninja that has no similarity API.
    """

    from .core.similarity import register

    register()


_reason = dependency_error()
if _reason is not None:
    log_warn(_install_hint(_reason), "binja-diff")
else:
    try:
        _register_similarity_provider()
    except Exception as exc:  # pragma: no cover - depends on the host version
        import traceback

        log_error(traceback.format_exc(), "binja-diff")
        log_error(f"binja-diff could not register its similarity provider: {exc}", "binja-diff")

    if core_ui_enabled():
        try:
            _register_ui()
        except Exception as exc:  # pragma: no cover - depends on the host UI
            import traceback

            log_error(traceback.format_exc(), "binja-diff")
            log_error(f"binja-diff failed to register its UI: {exc}", "binja-diff")
