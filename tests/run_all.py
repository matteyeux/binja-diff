#!/usr/bin/env python3
"""Run every headless test module.

.venv-qbindiff/bin/python binja_diff/tests/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Stubbed modules, runnable anywhere.
TESTS = (
    "test_backend.py",
    "test_align.py",
    "test_engine.py",
    "test_persist.py",
    "test_symbols.py",
    "test_scope.py",
    "test_cli.py",
    "test_similarity.py",
)

#: Needs a real Binary Ninja; skips itself cleanly when unavailable.
LIVE_TESTS = ("test_live.py",)


def main() -> int:
    here = Path(__file__).resolve().parent
    skip_live = "--no-live" in sys.argv

    failed = []
    for name in TESTS + (() if skip_live else LIVE_TESTS):
        print(f"=== {name} ===")
        if subprocess.call([sys.executable, str(here / name)]) != 0:
            failed.append(name)
        print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("all test modules passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
