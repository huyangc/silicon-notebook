from __future__ import annotations

import ast
import re
from pathlib import Path


HISTORICAL_ALLOWLIST = re.compile(r"\bTASK\d+_ALLOWED_[A-Z0-9_]+\b")
PATH_LINE_IDENTITY = re.compile(
    r"(?:backend|frontend|scripts)/[^:\n\"']+:\d+\b"
)
_SKIP_MARKERS = {
    "pytest.mark.skip": "pytest-skip",
    "pytest.mark.skipif": "pytest-skip",
    "pytest.mark.xfail": "pytest-xfail",
}


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _ancestors(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.AST, ...]:
    result: list[ast.AST] = []
    current = node
    while current in parents:
        current = parents[current]
        result.append(current)
    return tuple(result)


def _is_assert_diagnostic(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    ancestors = _ancestors(node, parents)
    joined = next(
        (item for item in ancestors if isinstance(item, ast.JoinedStr)),
        None,
    )
    if joined is None:
        return False
    return any(
        isinstance(item, ast.Assert) and item.msg is joined
        for item in ancestors
    )


def _is_diagnostic_keyword(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    return any(
        isinstance(item, ast.keyword) and item.arg == "diagnostic_lines"
        for item in _ancestors(node, parents)
    )


def _is_named_diagnostic_append(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    for item in _ancestors(node, parents):
        if not isinstance(item, ast.Call):
            continue
        if not isinstance(item.func, ast.Attribute):
            return False
        owner = _dotted_name(item.func.value).lower()
        return (
            item.func.attr == "append"
            and any(
                token in owner
                for token in ("diagnostic", "offender", "violation", "error")
            )
            and any(
                isinstance(arg, (ast.Constant, ast.JoinedStr))
                for arg in item.args
            )
        )
    return False


def _lineno_is_identity(
    node: ast.Attribute,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if _is_diagnostic_keyword(node, parents):
        return False
    if _is_assert_diagnostic(node, parents):
        return False
    if _is_named_diagnostic_append(node, parents):
        return False
    return any(
        isinstance(
            ancestor,
            (
                ast.Assign,
                ast.AnnAssign,
                ast.Compare,
                ast.Set,
                ast.Dict,
                ast.List,
                ast.Tuple,
            ),
        )
        or (
            isinstance(ancestor, ast.Call)
            and isinstance(ancestor.func, ast.Attribute)
            and ancestor.func.attr in {"add", "append", "extend", "update"}
        )
        for ancestor in _ancestors(node, parents)
    )


def _display_path(path: Path) -> str:
    for anchor in ("backend", "frontend", "scripts"):
        if anchor in path.parts:
            index = path.parts.index(anchor)
            return Path(*path.parts[index:]).as_posix()
    return path.name


def policy_offenders(paths: tuple[Path, ...]) -> list[str]:
    violations: set[tuple[str, int, str]] = set()
    for path in paths:
        source = path.read_text(encoding="utf-8")
        relative = _display_path(path)
        tree = ast.parse(source, filename=relative)
        parents = _parents(tree)

        for line_number, line in enumerate(source.splitlines(), start=1):
            if HISTORICAL_ALLOWLIST.search(line):
                violations.add(
                    (relative, line_number, "historical-task-allowlist")
                )

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if PATH_LINE_IDENTITY.search(node.value):
                    violations.add(
                        (relative, node.lineno, "path-line-identity")
                    )
            if isinstance(node, ast.Attribute):
                marker = _SKIP_MARKERS.get(_dotted_name(node))
                if marker:
                    violations.add((relative, node.lineno, marker))
                if node.attr == "lineno" and _lineno_is_identity(node, parents):
                    violations.add(
                        (relative, node.lineno, "line-number-identity")
                    )

    return [
        f"{path}:{line}: {kind}"
        for path, line, kind in sorted(violations)
    ]
