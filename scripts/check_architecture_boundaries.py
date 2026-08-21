#!/usr/bin/env python3
"""Fast, dependency-free architecture checks for the PR contracts lane."""
from __future__ import annotations

import argparse
import ast
from collections.abc import Callable
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


def import_from_base(module: str, path: Path, node: ast.ImportFrom) -> str:
    if node.level:
        package = (
            module.split(".")
            if path.name == "__init__.py"
            else module.split(".")[:-1]
        )
        keep = len(package) - (node.level - 1)
        return ".".join(
            [*package[:keep], *(node.module or "").split(".")]
        ).rstrip(".")
    return node.module or ""


def import_from_targets(
    module: str, path: Path, node: ast.ImportFrom
) -> set[str]:
    base = import_from_base(module, path, node)
    targets = {base} if base else set()
    targets.update(
        ".".join(part for part in (base, alias.name) if part)
        for alias in node.names
        if alias.name != "*"
    )
    return targets


def imported_modules(
    module: str, path: Path, *, include_members: bool = False
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            targets = import_from_targets(module, path, node)
            base = import_from_base(module, path, node)
            if base:
                imports.add(base)
            if include_members or node.module is None:
                imports.update(targets)
    return imports


def minimal_matching_module_references(
    imports: set[str], predicate: Callable[[str], bool]
) -> set[str]:
    """Keep the shortest matching module reference for each imported branch."""

    matches = {imported for imported in imports if predicate(imported)}
    return {
        imported
        for imported in matches
        if not any(
            imported.startswith(f"{candidate}.")
            for candidate in matches
            if candidate != imported
        )
    }


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
        # Include imported members so legal spellings such as
        # ``from app import services`` cannot hide a forbidden module edge.
        imports = imported_modules(module, path, include_members=True)
        if module == "app.repositories.ports":
            forbidden = minimal_matching_module_references(
                imports, lambda imported: imported.startswith("app.services")
            )
            for imported in sorted(forbidden):
                violations.append(f"{module} imports service {imported}")
        if module == "app.domain" or module.startswith("app.domain."):
            forbidden = minimal_matching_module_references(
                imports,
                lambda imported: imported.startswith(FORBIDDEN_DOMAIN_PREFIXES),
            )
            for imported in sorted(forbidden):
                violations.append(f"{module} imports forbidden {imported}")
        if module == "app.extension_sdk" or module.startswith("app.extension_sdk."):
            forbidden = minimal_matching_module_references(
                imports,
                lambda imported: imported.startswith("app.")
                and not imported.startswith(("app.domain", "app.extension_sdk")),
            )
            for imported in sorted(forbidden):
                violations.append(f"{module} imports forbidden {imported}")
        is_plugin_implementation = (
            module.startswith("app.extensions.builtin.")
            or module.startswith("app.extensions.plugins.")
            or (module.startswith("app.features.") and module.endswith(".plugin"))
        )
        if is_plugin_implementation:
            own_feature = (
                ".".join(module.split(".")[:3])
                if module.startswith("app.features.")
                else ""
            )
            has_own_feature_target = bool(own_feature) and any(
                imported == own_feature
                or imported.startswith(f"{own_feature}.")
                for imported in imports
            )
            forbidden = minimal_matching_module_references(
                imports,
                lambda imported: imported.startswith("app.")
                and not imported.startswith(
                    ("app.domain", "app.extension_sdk")
                )
                and not (
                    own_feature
                    and (
                        imported == own_feature
                        or imported.startswith(f"{own_feature}.")
                    )
                )
                and not (
                    has_own_feature_target
                    and own_feature.startswith(f"{imported}.")
                ),
            )
            for imported in sorted(forbidden):
                violations.append(
                    f"{module} imports forbidden plugin dependency {imported}"
                )
        imports_extension_runtime = any(
            imported == "app.extensions"
            or imported.startswith("app.extensions.")
            for imported in imports
        )
        is_extension_composition = (
            module == "app.bootstrap"
            or module == "app.extensions"
            or module.startswith("app.extensions.")
        )
        imports_extension_sdk = any(
            imported == "app.extension_sdk"
            or imported.startswith("app.extension_sdk.")
            for imported in imports
        )
        is_sdk_consumer = (
            is_extension_composition
            or module == "app.extension_sdk"
            or module.startswith("app.extension_sdk.")
            or is_plugin_implementation
        )
        if imports_extension_runtime and not is_extension_composition:
            violations.append(
                f"{module} imports the extension composition surface outside an approved root"
            )
        if imports_extension_sdk and not is_sdk_consumer:
            violations.append(
                f"{module} imports the extension composition surface outside an approved root"
            )
    return violations


def repository_service_import_counts(app_root: Path) -> dict[str, int]:
    """Count existing adapter-to-service import statements by backend.

    This is an intentional debt ceiling rather than a desired dependency: the
    count may fall as ports are extracted, but any new reverse edge is rejected.
    Counting statements (not imported symbols) keeps the baseline stable under
    harmless changes to a multi-symbol import.
    """

    counts = {"sqlite": 0, "postgres": 0, "other": 0}
    repositories_root = app_root / "repositories"
    for path in sorted(repositories_root.rglob("*.py")):
        relative = path.relative_to(repositories_root)
        backend = (
            relative.parts[0]
            if relative.parts and relative.parts[0] in {"sqlite", "postgres"}
            else "other"
        )
        module = module_name(app_root, path)
        count = 0
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                targets = import_from_targets(module, path, node)
            else:
                continue
            if any(
                target == "app.services" or target.startswith("app.services.")
                for target in targets
            ):
                count += 1
        counts[backend] += count
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
    names.update(f"<base:{ast.unparse(base)}>" for base in class_node.bases)
    for node in class_node.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(
            target.id
            for target in targets
            if isinstance(target, ast.Name) and not target.id.startswith("_")
        )
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
