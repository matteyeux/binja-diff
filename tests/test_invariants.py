"""Static guards for the rules that crash the process rather than a test.

None of these can be caught by running the plugin headlessly, and each has
bitten before (see CLAUDE.md): the import order under ``ui/`` takes Binary
Ninja down when it is wrong, Qt in ``core/`` makes the headless tests
impossible, a helper named after one of ``SimilarityProvider``'s callbacks
silently replaces that callback, and a version string that drifts from
``plugin.json`` misreports every bug. All are plain text and AST checks.

    .venv/bin/python tests/test_invariants.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)

check = _bootstrap.check

ROOT = Path(__file__).resolve().parent.parent

#: Modules that may not import Qt: the engine, and the worker runners that the
#: engine's tests drive.
QT_FREE = [*sorted((ROOT / "core").glob("*.py")), ROOT / "ui" / "background.py"]

QT_PACKAGES = ("PySide6", "PySide2", "shiboken6", "binaryninjaui")

#: The methods the base class binds as C callbacks. A subclass defining one of
#: these replaces the dispatcher, and the core then calls it with the
#: dispatcher's arguments.
PROVIDER_CALLBACKS = {
    "_visit_node",
    "_visit_node_edge",
    "_get_name",
    "_apply",
    "_render",
    "_free",
    "_add_ref",
    "_release",
}


def _imports(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, top-level package) for every import, module level or not."""

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module.split(".")[0]))
    return sorted(found)


def test_core_imports_no_qt():
    print("core/ and the worker runners import no Qt")
    for path in QT_FREE:
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders = [name for _line, name in _imports(tree) if name in QT_PACKAGES]
        check(f"{path.relative_to(ROOT)} is Qt-free", not offenders, f"imports {offenders}")


def test_ui_imports_binaryninjaui_before_pyside():
    """Binary Ninja ships a PySide6 ABI-matched to its own UI library; loading
    PySide6 first loads the wrong one and crashes rather than raising."""

    print("every ui module imports binaryninjaui before PySide6")
    for path in sorted((ROOT / "ui").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = _imports(tree)
        pyside = [line for line, name in imports if name in ("PySide6", "shiboken6")]
        if not pyside:
            continue
        ui = [line for line, name in imports if name == "binaryninjaui"]
        check(
            f"{path.relative_to(ROOT)} imports binaryninjaui first",
            bool(ui) and min(ui) < min(pyside),
            f"binaryninjaui at {ui}, PySide6 at {pyside}",
        )


def test_isort_stays_off():
    print("ruff's import sorter stays off, for the same reason")
    config = (ROOT / "ruff.toml").read_text()
    select = re.search(r"select\s*=\s*\[(.*?)\]", config, re.S)
    check("a select list exists", select is not None)
    assert select is not None
    rules = re.findall(r'"([A-Z0-9]+)"', select.group(1))
    check("I is not selected", "I" not in rules, f"{rules}")


def test_provider_defines_no_callback_names():
    print("the similarity provider does not shadow the base class callbacks")
    tree = ast.parse((ROOT / "core" / "similarity.py").read_text())
    defined = {
        node.name
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    }
    clashes = sorted(defined & PROVIDER_CALLBACKS)
    check("no method is named after a callback", not clashes, f"{clashes}")


def test_version_matches_the_manifest():
    print("__version__ agrees with plugin.json")
    manifest = json.loads((ROOT / "plugin.json").read_text())
    source = (ROOT / "__init__.py").read_text()
    found = re.search(r'^__version__ = "([^"]+)"', source, re.M)
    check("__version__ is defined", found is not None)
    assert found is not None
    check(
        "the two versions agree",
        found.group(1) == manifest["version"],
        f"__init__ {found.group(1)} vs plugin.json {manifest['version']}",
    )
    check("the manifest declares qbindiff", "qbindiff" in " ".join(manifest["dependencies"]["pip"]))


def test_no_vendored_tree_is_tracked_or_linted():
    print("the configs exclude the reference checkouts")
    for name in ("ruff.toml", "ty.toml", ".gitignore"):
        text = (ROOT / name).read_text()
        check(f"{name} knows binaryninja-api", "binaryninja-api" in text)


def main() -> int:
    return _bootstrap.run(
        [
            test_core_imports_no_qt,
            test_ui_imports_binaryninjaui_before_pyside,
            test_isort_stays_off,
            test_provider_defines_no_callback_names,
            test_version_matches_the_manifest,
            test_no_vendored_tree_is_tracked_or_linted,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
