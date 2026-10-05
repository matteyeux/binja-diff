"""Install the Binary Ninja stubs before ``binja_diff`` is first imported.

``binja_diff/__init__.py`` is the plugin entry point and imports ``binaryninja``
at module scope, so the stubs have to be in ``sys.modules`` before any
``from binja_diff... import`` runs. That rules out importing the stub through
the package itself, hence the load-by-path below.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = TESTS_DIR.parent


def install() -> None:
    if "binaryninja" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "_stub_binaryninja", TESTS_DIR / "stub_binaryninja.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # @dataclass resolves annotations via sys.modules[cls.__module__], so the
        # module has to be registered before its body runs.
        sys.modules["_stub_binaryninja"] = module
        spec.loader.exec_module(module)
        module.install()

    register_package()


def register_package() -> None:
    """Make the checkout importable as ``binja_diff``, whatever it is called.

    The package directory is the checkout itself, whose name ("binja-diff-2")
    is not a valid module name — inside Binary Ninja it goes by its plugin
    folder name anyway. Register it by path under the name the tests import,
    rather than putting its parent on sys.path and hoping the directory there
    is called the right thing.
    """

    if "binja_diff" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "binja_diff",
            PACKAGE_DIR / "__init__.py",
            submodule_search_locations=[str(PACKAGE_DIR)],
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules["binja_diff"] = package
        spec.loader.exec_module(package)


def stubs():
    return sys.modules["_stub_binaryninja"]


class CheckFailed(AssertionError):
    """A ``check`` that did not hold. An AssertionError, so pytest reports it."""


def check(label: str, condition: bool, detail: str = "") -> None:
    """Assert one fact, printing it either way.

    Raises on failure rather than counting it: the old counter let a module run
    green under pytest whatever happened, since nothing ever raised. The print
    stays so a run from the shell still reads as a checklist.
    """

    if condition:
        print(f"  [ok  ] {label}")
        return
    message = f"{label} -- {detail}" if detail else label
    print(f"  [FAIL] {message}")
    raise CheckFailed(message)


def run(tests) -> int:
    """Run each test function in turn, past failures. The module's exit code.

    What ``python tests/test_x.py`` does with its tests; pytest collects the
    same functions itself and never calls this.
    """

    failed: list[str] = []
    for test in tests:
        try:
            test()
        except CheckFailed as exc:
            failed.append(f"{test.__name__}: {exc}")
        except Exception:
            traceback.print_exc()
            failed.append(f"{test.__name__}: raised")
    print()
    if failed:
        for line in failed:
            print(f"FAILED {line}")
        print(f"{len(failed)} test(s) failed")
        return 1
    print("all checks passed")
    return 0


class LiveRunner:
    """``run`` for tests that feed each other: a failed step yields ``None``."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def __call__(self, test, *args):
        try:
            return test(*args)
        except CheckFailed as exc:
            self.failed.append(f"{test.__name__}: {exc}")
        except Exception:
            traceback.print_exc()
            self.failed.append(f"{test.__name__}: raised")
        return None

    def summary(self) -> int:
        print()
        if self.failed:
            for line in self.failed:
                print(f"FAILED {line}")
            print(f"{len(self.failed)} test(s) failed")
            return 1
        print("all checks passed")
        return 0
