"""Every pytest hook a conftest defines is defined there exactly once.

A second module-level ``def pytest_configure`` (or any ``pytest_*`` hook) in
the same conftest silently replaces the first: Python keeps the last binding
and pytest only ever sees that one. It happened once already, when a rebase
put a bytecode-pruning ``pytest_configure`` below the one that installs
``tests/speedups``; the speedups simply stopped being installed and nothing
failed except a pin that happened to check for it. This guard makes the
duplicate itself the failure.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


_TESTS_ROOT = Path(__file__).resolve().parent


def duplicated_hooks(source: str) -> list[str]:
    """Names of ``pytest_*`` functions defined more than once at module level."""
    counts = Counter(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("pytest_")
    )
    return sorted(name for name, count in counts.items() if count > 1)


def test_duplicate_hook_detection_on_a_synthetic_conftest():
    source = (
        "import pytest\n"
        "def pytest_configure(config):\n"
        "    pass\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    pass\n"
        "def pytest_configure(config):\n"
        "    pass\n"
        "async def pytest_sessionstart(session):\n"
        "    pass\n"
        "def helper():\n"
        "    def pytest_configure(config):\n"
        "        pass\n"
    )
    assert duplicated_hooks(source) == ["pytest_configure"]
    assert duplicated_hooks(
        "def pytest_configure(config):\n    pass\n"
        "def pytest_unconfigure(config):\n    pass\n"
    ) == []


def test_every_conftest_defines_each_pytest_hook_once():
    conftests = sorted(_TESTS_ROOT.rglob("conftest.py"))
    assert _TESTS_ROOT / "conftest.py" in conftests
    offenders = {
        str(path.relative_to(_TESTS_ROOT)): duplicated
        for path in conftests
        if (duplicated := duplicated_hooks(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        "a later definition silently replaces the earlier one; merge the "
        f"bodies into a single hook: {offenders}"
    )
