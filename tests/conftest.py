"""Let pytest collect the stubbed tier.

Each module installs the stubs itself when run as a script; under pytest they
are installed here first, before any module is imported, which keeps
``bootstrap.install`` the one place that knows how. ``test_live.py`` is left
to ``run_all.py``: it drives a real Binary Ninja, exits at import when there is
none, and its test functions feed one another rather than taking fixtures.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

collect_ignore = ["test_live.py", "run_all.py", "bootstrap.py", "stub_binaryninja.py"]

_spec = importlib.util.spec_from_file_location(
    "_bootstrap", Path(__file__).resolve().parent / "bootstrap.py"
)
assert _spec is not None and _spec.loader is not None
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.install()
