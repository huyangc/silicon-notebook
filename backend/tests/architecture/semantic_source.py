from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True, order=True)
class SemanticKey:
    path: str
    scope: str
    kind: str
    target: str


@dataclass(frozen=True)
class SemanticFinding:
    key: SemanticKey
    count: int
    diagnostic_lines: tuple[int, ...]


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scopes = ["<module>"]
        self.sites: list[tuple[SemanticKey, int]] = []

    @property
    def scope(self) -> str:
        return ".".join(self.scopes)

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scopes.append(name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.sites.append(
                (
                    SemanticKey(
                        path=self.path,
                        scope=self.scope,
                        kind="import",
                        target=alias.name,
                    ),
                    node.lineno,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            self.sites.append(
                (
                    SemanticKey(
                        path=self.path,
                        scope=self.scope,
                        kind="import",
                        target=f"{module}:{alias.name}",
                    ),
                    node.lineno,
                )
            )

    def visit_Call(self, node: ast.Call) -> None:
        target = dotted_name(node.func)
        if target:
            self.sites.append(
                (
                    SemanticKey(
                        path=self.path,
                        scope=self.scope,
                        kind="call",
                        target=target,
                    ),
                    node.lineno,
                )
            )
        self.generic_visit(node)


class PythonSourceIndex:
    def __init__(self, findings: tuple[SemanticFinding, ...]) -> None:
        self.findings = findings

    @classmethod
    def from_sources(cls, sources: Mapping[str, str]) -> PythonSourceIndex:
        grouped: dict[SemanticKey, list[int]] = defaultdict(list)
        for path, source in sorted(sources.items()):
            visitor = _Visitor(path)
            visitor.visit(ast.parse(source, filename=path))
            for key, line in visitor.sites:
                grouped[key].append(line)
        findings = tuple(
            SemanticFinding(
                key=key,
                count=len(lines),
                diagnostic_lines=tuple(sorted(lines)),
            )
            for key, lines in sorted(grouped.items())
        )
        return cls(findings)

    @classmethod
    def from_paths(
        cls,
        root: Path,
        paths: Iterable[Path],
    ) -> PythonSourceIndex:
        return cls.from_sources(
            {
                path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                for path in paths
            }
        )

    def imports(self, *, target: str | None = None) -> tuple[SemanticFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.key.kind == "import"
            and (target is None or finding.key.target == target)
        )

    def calls(self, *, target: str | None = None) -> tuple[SemanticFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.key.kind == "call"
            and (target is None or finding.key.target == target)
        )

    def import_keys(self) -> frozenset[SemanticKey]:
        return frozenset(finding.key for finding in self.imports())

    def call_keys(self) -> frozenset[SemanticKey]:
        return frozenset(finding.key for finding in self.calls())
