from __future__ import annotations

import ast
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import re

from tests.architecture.semantic_source import (
    PythonSourceIndex,
    SemanticFinding,
    SemanticKey,
    dotted_name,
    qualified_scopes,
)


ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOTS = (ROOT / "backend" / "app", ROOT / "scripts")
REPO_NAME_RE = re.compile(r"^(?:repo\d*|repository|[A-Za-z_]\w*_repo)$")


def _production_files() -> tuple[tuple[Path, str], ...]:
    return tuple(
        (path, path.relative_to(ROOT).as_posix())
        for base in PRODUCTION_ROOTS
        for path in sorted(base.rglob("*.py"))
        if "__pycache__" not in path.parts
    )


@lru_cache(maxsize=1)
def production_source_index() -> PythonSourceIndex:
    return PythonSourceIndex.from_paths(
        ROOT,
        (path for path, _relative in _production_files()),
    )


def _annotation_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return dotted_name(node)


def _is_repository_annotation(node: ast.AST | None) -> bool:
    name = _annotation_name(node).rsplit(".", 1)[-1].strip("'\"")
    return name.endswith(("Repository", "RepositoryPort")) or name in {
        "AskStreamPort",
        "SQLiteMaintenancePort",
    }


def _is_repo_expression(
    node: ast.AST,
    names: set[str],
    attrs: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names or bool(REPO_NAME_RE.match(node.id))
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and (node.attr in attrs or bool(REPO_NAME_RE.match(node.attr)))
        )
    if isinstance(node, ast.Call):
        return dotted_name(node.func).rsplit(".", 1)[-1] in {
            "SQLiteRepository",
            "repository",
        }
    return False


def _repo_like_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Call):
                function = dotted_name(value.func).rsplit(".", 1)[-1]
                if function in {"SQLiteRepository", "repository"}:
                    names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (*node.args.args, *node.args.kwonlyargs):
                if _is_repository_annotation(argument.annotation):
                    names.add(argument.arg)
    return names


def _repo_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    names = _repo_like_names(tree)
    attrs: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            value_is_repo = _is_repo_expression(value, names, attrs)
            if not value_is_repo and isinstance(node, ast.AnnAssign):
                value_is_repo = _is_repository_annotation(node.annotation)
            if not value_is_repo:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr not in attrs
                ):
                    attrs.add(target.attr)
                    changed = True
    return names, attrs


def private_repository_sites() -> tuple[SemanticFinding, ...]:
    diagnostic_lines_by_key: dict[SemanticKey, list[int]] = defaultdict(list)
    for path, relative in _production_files():
        if relative == "backend/app/services/sqlite_repository.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        scopes = qualified_scopes(tree)
        repo_names, repo_attrs = _repo_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not node.attr.startswith("_") or node.attr.startswith("__"):
                continue
            if not _is_repo_expression(node.value, repo_names, repo_attrs):
                continue
            key = SemanticKey(
                path=relative,
                scope=scopes[node],
                kind="attribute",
                target=node.attr,
            )
            diagnostic_lines_by_key[key].append(node.lineno)
    return tuple(
        SemanticFinding(
            key=key,
            count=len(lines),
            diagnostic_lines=tuple(sorted(lines)),
        )
        for key, lines in sorted(diagnostic_lines_by_key.items())
    )
