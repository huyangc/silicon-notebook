"""CI-executed tests must be independent of developer-local state."""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


ROOT = Path(__file__).resolve().parents[2]
HARNESS_TESTS = ROOT / "fangan" / "testcases" / "harness" / "tests"
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _absolute_path_literals(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            matches.append(value)
    return tuple(matches)


def _declared_requirement_names() -> frozenset[str]:
    names: set[str] = set()
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        match = _REQUIREMENT_NAME.match(line)
        assert match is not None, f"unsupported requirement line: {raw_line!r}"
        names.add(re.sub(r"[-_.]+", "-", match.group(0)).lower())
    return frozenset(names)


def test_ci_harness_tests_do_not_embed_absolute_paths() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): _absolute_path_literals(path)
        for path in sorted(HARNESS_TESTS.glob("test_*.py"))
        if _absolute_path_literals(path)
    }
    assert offenders == {}


def test_pytest_controller_matplotlib_import_is_declared() -> None:
    assert "matplotlib" in _declared_requirement_names()
