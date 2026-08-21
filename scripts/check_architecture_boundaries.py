#!/usr/bin/env python3
"""Fast, dependency-free architecture checks for the PR contracts lane."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


FORBIDDEN_DOMAIN_PREFIXES = (
    "app.api",
    "app.extension_sdk",
    "app.extensions",
    "app.features",
    "app.infrastructure",
    "app.repositories",
    "app.services",
)
EMPTY_REGISTRY_IMPORTERS = frozenset(
    {
        "app.main",
        "app.extensions",
        "app.extensions.bootstrap",
        "app.extensions.registry",
    }
)


def module_name(app_root: Path, path: Path) -> str:
    relative = path.relative_to(app_root.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def python_modules(app_root: Path) -> dict[str, Path]:
    return {
        module_name(app_root, path): path
        for path in sorted(app_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def imported_modules(
    module: str, path: Path, *, include_members: bool = False
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = (
                    module.split(".")
                    if path.name == "__init__.py"
                    else module.split(".")[:-1]
                )
                keep = len(package) - (node.level - 1)
                base = ".".join(
                    [*package[:keep], *(node.module or "").split(".")]
                ).rstrip(".")
            else:
                base = node.module or ""
            if base:
                imports.add(base)
            if include_members or node.module is None:
                for alias in node.names:
                    if alias.name != "*":
                        imports.add(
                            ".".join(part for part in (base, alias.name) if part)
                        )
    return imports


def import_graph(app_root: Path) -> dict[str, set[str]]:
    modules = python_modules(app_root)
    graph = {module: set() for module in modules}
    for module, path in modules.items():
        for imported in imported_modules(module, path, include_members=True):
            target = imported
            while target and target not in modules:
                target = target.rpartition(".")[0]
            if target and target != module:
                graph[module].add(target)
    return graph


def strongly_connected_components(
    graph: dict[str, set[str]],
) -> list[tuple[str, ...]]:
    index = 0
    indexes: dict[str, int] = {}
    lows: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = lows[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indexes:
                visit(target)
                lows[node] = min(lows[node], lows[target])
            elif target in on_stack:
                lows[node] = min(lows[node], indexes[target])
        if lows[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node in graph:
        if node not in indexes:
            visit(node)
    return sorted(components)


def boundary_violations(app_root: Path) -> list[str]:
    modules = python_modules(app_root)
    violations: list[str] = []
    for module, path in modules.items():
        imports = imported_modules(module, path)
        if module == "app.repositories.ports":
            for imported in sorted(imports):
                if imported.startswith("app.services"):
                    violations.append(f"{module} imports service {imported}")
        if module == "app.domain" or module.startswith("app.domain."):
            for imported in sorted(imports):
                if imported.startswith(FORBIDDEN_DOMAIN_PREFIXES):
                    violations.append(f"{module} imports forbidden {imported}")
        if module == "app.extension_sdk" or module.startswith("app.extension_sdk."):
            for imported in sorted(imports):
                if imported.startswith("app.") and not imported.startswith(
                    ("app.domain", "app.extension_sdk")
                ):
                    violations.append(f"{module} imports forbidden {imported}")
        if any(
            imported == "app.extensions" or imported.startswith("app.extensions.")
            for imported in imports
        ) and module not in EMPTY_REGISTRY_IMPORTERS:
            violations.append(
                f"{module} consumes the Phase-0 empty registry; existing workflows must stay untouched"
            )
    return violations


def repository_service_import_counts(app_root: Path) -> dict[str, int]:
    """Count existing adapter-to-service import statements by backend.

    This is an intentional debt ceiling rather than a desired dependency: the
    count may fall as ports are extracted, but any new reverse edge is rejected.
    Counting statements (not imported symbols) keeps the baseline stable under
    harmless changes to a multi-symbol import.
    """

    counts: dict[str, int] = {}
    for backend in ("sqlite", "postgres"):
        count = 0
        for path in sorted((app_root / "repositories" / backend).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports_service = any(
                        alias.name.startswith("app.services") for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom):
                    imports_service = (node.module or "").startswith("app.services")
                else:
                    continue
                if imports_service:
                    count += 1
        counts[backend] = count
    return counts


def repository_service_import_violations(
    app_root: Path, ceilings: dict[str, int]
) -> list[str]:
    actual = repository_service_import_counts(app_root)
    return [
        f"repositories/{backend} service imports grew: {count} > {ceilings[backend]}"
        for backend, count in sorted(actual.items())
        if count > ceilings[backend]
    ]


def public_class_surface(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    names = {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    for node in ast.walk(class_node):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and not target.attr.startswith("_")
            ):
                names.add(target.attr)
    return names


def facade_surface_additions(
    path: Path, class_name: str, allowed_names: set[str]
) -> list[str]:
    return sorted(public_class_surface(path, class_name) - allowed_names)


def facade_violations(root: Path) -> list[str]:
    baseline_path = root / "scripts" / "architecture_boundary_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    targets = {
        "RepositoryFacade": root / "backend/app/services/repository_facade.py",
        "SQLiteRepository": root / "backend/app/services/sqlite_repository.py",
    }
    violations: list[str] = []
    for class_name, path in targets.items():
        allowed = set(baseline["facade_public_surface"][class_name]["allowed_names"])
        additions = facade_surface_additions(path, class_name, allowed)
        if additions:
            violations.append(
                f"{class_name} gained public facade seats: {', '.join(additions)}"
            )
    return violations


def check(root: Path) -> list[str]:
    app_root = root / "backend/app"
    baseline = json.loads(
        (root / "scripts" / "architecture_boundary_baseline.json").read_text(
            encoding="utf-8"
        )
    )
    violations = boundary_violations(app_root)
    violations.extend(
        repository_service_import_violations(
            app_root, baseline["repository_service_import_ceiling"]
        )
    )
    components = strongly_connected_components(import_graph(app_root))
    violations.extend(f"static import SCC: {', '.join(item)}" for item in components)
    violations.extend(facade_violations(root))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    violations = check(args.root.resolve())
    if violations:
        for violation in violations:
            print(f"architecture guard: {violation}")
        return 1
    print(
        "architecture guard: OK "
        "(0 SCCs; boundaries frozen; reverse imports capped; facade may only shrink)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
