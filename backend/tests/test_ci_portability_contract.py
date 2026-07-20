"""CI-executed tests must be independent of a developer checkout path."""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath, PureWindowsPath


ROOT = Path(__file__).resolve().parents[2]
HARNESS_TESTS = ROOT / "fangan" / "testcases" / "harness" / "tests"


def _absolute_path_literals(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            matches.append((node.lineno, value))
    return tuple(matches)


def test_ci_harness_tests_do_not_embed_absolute_paths() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): _absolute_path_literals(path)
        for path in sorted(HARNESS_TESTS.glob("test_*.py"))
        if _absolute_path_literals(path)
    }
    assert offenders == {}
