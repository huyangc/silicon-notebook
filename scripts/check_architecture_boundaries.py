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
ALLOWED_APPLICATION_PREFIXES = (
    "app.application",
    "app.core.ask_retrieval_policy",
    "app.domain.cancellation",
    "app.models.ask",
)


def module_name(app_root: Path, path: Path) -> str:
    relative = path.relative_to(app_root.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def matches_module_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in prefixes
    )


def imports_bare_module(path: Path, module: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Import)
        and any(
            alias.name == module
            or (
                alias.name.startswith(f"{module}.")
                and alias.asname is None
            )
            for alias in node.names
        )
        for node in ast.walk(tree)
    )


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
        if module == "app.application" or module.startswith("app.application."):
            forbidden = minimal_matching_module_references(
                imports,
                lambda imported: imported.startswith("app.")
                and not matches_module_prefix(imported, ALLOWED_APPLICATION_PREFIXES),
            )
            if imports_bare_module(path, "app"):
                forbidden.add("app")
            for imported in sorted(forbidden):
                violations.append(
                    f"{module} imports forbidden implementation {imported}"
                )
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


CORE_MODELS_SERVICE_PREFIXES = ("app.core", "app.models")


def _normalized_service_edge(module: str, reference: str) -> set[str]:
    """Normalize one raw ``app.services...`` module reference to an edge.

    A reference is normalized to ``<module> -> app.services.<submodule>``
    whenever it names a specific submodule, no matter how many path segments
    beyond that submodule it carries (``app.services.kg`` and
    ``app.services.kg.ppr`` both normalize to ``app.services.kg``). A
    reference that cannot be attributed to any specific submodule -- the bare
    ``app.services`` package itself -- normalizes to its own unnormalizable
    edge instead, which can never satisfy an allowlist entry that always
    names a concrete submodule. Non-``app.services`` references produce no
    edge at all.
    """

    if reference == "app.services":
        return {f"{module} -> app.services"}
    if not reference.startswith("app.services."):
        return set()
    submodule = reference.split(".")[2]
    if not submodule:
        return {f"{module} -> app.services"}
    return {f"{module} -> app.services.{submodule}"}


def core_models_service_import_edges(app_root: Path) -> set[str]:
    """Reverse-dependency edges from ``app.core``/``app.models`` into
    ``app.services``, normalized to the immediate ``app.services.<name>``
    submodule regardless of which import spelling reached it.

    This is an intentional debt allowlist, not a desired dependency: every
    edge here predates this guard and may be removed, but no new edge may be
    added and no existing edge may go stale without the baseline being
    updated in the same change. Unlike the facade/repository ceilings above
    (which only grow, so a lowered call the ceiling could still absorb goes
    unnoticed), this allowlist and the hot-function ceiling below carry zero
    slack, which also catches a stub collapsing to its first overload match.

    The edge is deliberately normalized rather than taken verbatim from
    whichever import spelling was used: a raw ``from app.services import
    cancellation`` and a raw ``from app.services.cancellation import X``
    reach the exact same submodule and must produce the exact same edge, or
    the allowlist could be satisfied by one spelling while the other slips
    past unrecorded. Left unnormalized, a bare package-level edge
    (``app.core.llm -> app.services``) could also end up in the allowlist by
    accident and then silently authorize importing *any* submodule of
    ``app.services`` from that module, not just the one originally reviewed
    -- so any reference that cannot be attributed to a specific submodule (a
    bare ``import app.services``, a wildcard ``from app.services import *``,
    or a bare ``import app`` reached only through later attribute access) is
    recorded as its own edge that can never satisfy an allowlist entry naming
    a concrete submodule.
    """

    modules = python_modules(app_root)
    edges: set[str] = set()
    for module, path in modules.items():
        if not matches_module_prefix(module, CORE_MODELS_SERVICE_PREFIXES):
            continue
        if imports_bare_module(path, "app"):
            # A bare ``import app`` can reach any ``app.services`` submodule
            # through attribute access alone, with no further import
            # statement to normalize, so it is flagged outright -- mirroring
            # the ``app.application`` boundary check's own use of
            # ``imports_bare_module`` above.
            edges.add(f"{module} -> app")
        # ast.walk reaches imports nested inside ``if TYPE_CHECKING:`` blocks
        # and other conditionals, same as the include_members=True walk this
        # replaces.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.update(_normalized_service_edge(module, alias.name))
            elif isinstance(node, ast.ImportFrom):
                base = import_from_base(module, path, node)
                if base == "app":
                    # ``from app import services`` (or the relative
                    # ``from .. import services`` inside app/core) binds the
                    # whole package under a local name, after which any
                    # submodule is one attribute access away with no further
                    # import to normalize -- the same hole as a bare
                    # ``import app.services``, so it gets the same
                    # unnormalizable edge (codex #565 R1 P2).
                    if any(alias.name == "services" for alias in node.names):
                        edges.add(f"{module} -> app.services")
                    continue
                if base != "app.services" and not base.startswith(
                    "app.services."
                ):
                    continue
                if base != "app.services":
                    edges.update(_normalized_service_edge(module, base))
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        edges.add(f"{module} -> app.services")
                        continue
                    edges.update(
                        _normalized_service_edge(module, f"{base}.{alias.name}")
                    )
    return edges


def core_models_service_import_violations(
    app_root: Path, allowed_edges: set[str]
) -> list[str]:
    actual = core_models_service_import_edges(app_root)
    violations = [
        f"core/models gained a service import: {edge}"
        for edge in sorted(actual - allowed_edges)
    ]
    violations.extend(
        "core/models service import allowlist is stale, remove: "
        f"{edge} (update scripts/architecture_boundary_baseline.json :: "
        "core_models_service_imports.allowed)"
        for edge in sorted(allowed_edges - actual)
    )
    return violations


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


def find_qualname_node(
    tree: ast.Module, qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Resolve a dotted qualname (``method`` / ``Class.method`` /
    ``outer_func.inner_func``) to its function node by walking direct
    ``body`` children at each level — this reaches methods nested in a
    class and functions nested in another function (e.g. a tool registered
    inside a ``register_*`` closure), but not conditionally-defined
    functions.
    """

    node: ast.AST = tree
    for part in qualname.split("."):
        found = None
        for child in getattr(node, "body", []):
            if (
                isinstance(
                    child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
                and child.name == part
            ):
                found = child
                break
        if found is None:
            return None
        node = found
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node
    return None


def function_length(root: Path, qualified_path: str) -> int | None:
    """Line count for ``<repo-relative path>::<qualname>``.

    Counts from the ``def``/``async def`` line through the last line of the
    body inclusive (``node.end_lineno - node.lineno + 1``); decorator lines
    are excluded because ``ast`` already points ``lineno`` at the ``def``
    keyword rather than the first decorator. Returns ``None`` when the file
    or the qualname inside it cannot be resolved.
    """

    repo_relative, _, qualname = qualified_path.partition("::")
    path = root / repo_relative
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = find_qualname_node(tree, qualname)
    if node is None:
        return None
    return node.end_lineno - node.lineno + 1


def function_length_violations(
    root: Path, ceilings: dict[str, int]
) -> list[str]:
    """Zero-slack ceiling on a fixed set of hot functions: grew above the
    recorded ceiling is a violation, and shrank below it is *also* a
    violation (the baseline must be lowered in the same change), unlike the
    facade/repository ceilings above, which only grow. Those two only check
    for growth -- a shrink is accepted silently, so their recorded baselines
    can lag behind reality without tripping anything. This ceiling and the
    core-models allowlist above it are zero-slack on purpose: an only-grow
    ceiling accumulates silent headroom a later regression can spend
    unnoticed (say, 2250 lines quietly dropping to 2000 and then climbing
    back to 2240 without ever being caught), and a zero-slack ceiling is also
    the only one of the two that can catch a function collapsing to a stub
    that happens to match its first ``@overload``."""

    violations: list[str] = []
    for qualified_path, ceiling in sorted(ceilings.items()):
        actual = function_length(root, qualified_path)
        if actual is None:
            violations.append(
                "function length ceiling target not found: "
                f"{qualified_path} (update "
                "scripts/architecture_boundary_baseline.json :: "
                "function_length_ceiling)"
            )
            continue
        if actual > ceiling:
            violations.append(f"function grew: {qualified_path} {actual} > {ceiling}")
        elif actual < ceiling:
            violations.append(
                "function length ceiling is stale, lower it: "
                f"{qualified_path} {actual} < {ceiling} (update "
                "scripts/architecture_boundary_baseline.json :: "
                "function_length_ceiling)"
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
    violations.extend(
        core_models_service_import_violations(
            app_root, set(baseline["core_models_service_imports"]["allowed"])
        )
    )
    components = strongly_connected_components(import_graph(app_root))
    violations.extend(f"static import SCC: {', '.join(item)}" for item in components)
    violations.extend(facade_violations(root))
    violations.extend(
        function_length_violations(root, baseline["function_length_ceiling"])
    )
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
        "(0 SCCs; boundaries frozen; reverse imports capped; "
        "core/models service imports allowlisted; "
        "facade and hot functions may only shrink)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
