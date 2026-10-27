#!/usr/bin/env python3
"""Fast, dependency-free architecture checks for the PR contracts lane."""
from __future__ import annotations

import argparse
import ast
import functools
from collections.abc import Callable
import json
from pathlib import Path
import re


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
    # The typed retrieval terminal state travels ReasoningResult ->
    # ReasoningEvidenceSnapshot -> ResponseDraftInput.  Admitted as one named
    # module rather than the whole ``app.domain`` package, so the next domain
    # type still needs its own decision.
    "app.domain.retrieval_termination",
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


def _dynamic_import_target(node: ast.AST) -> str | None:
    """The literal module name of ``import_module("x")`` / ``__import__("x")``.

    ``importlib.import_module``, a bare ``import_module`` and ``__import__``
    all count; a non-literal first argument is not resolvable statically and
    is left to review.
    """
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else None
    )
    if name not in {"import_module", "__import__"}:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def repository_service_import_counts(app_root: Path) -> dict[str, int]:
    """Count existing adapter-to-service import statements by backend.

    This is an intentional debt ceiling rather than a desired dependency: the
    count may fall as ports are extracted, but any new reverse edge is rejected.
    Counting statements (not imported symbols) keeps the baseline stable under
    harmless changes to a multi-symbol import. A literal dynamic import
    (``importlib.import_module("app.services...")`` / ``__import__``) counts
    as one statement too, so the edge cannot be hidden from the ceiling by
    spelling it at run time. Every ceiling is 0 since the filesystem scale
    artifact store took its codec by injection: a repository needs a service
    capability → declare a protocol and let the composer pass it in.
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
                dynamic = _dynamic_import_target(node)
                if dynamic is None:
                    continue
                targets = {dynamic}
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


def facade_surface_stale(
    path: Path, class_name: str, allowed_names: set[str]
) -> list[str]:
    return sorted(allowed_names - public_class_surface(path, class_name))


def facade_violations(root: Path) -> list[str]:
    """Zero-slack public-surface freeze: a seat the allowlist does not name
    is a violation, and an allowlisted name the class no longer has is *also*
    a violation (retiring a facade method must remove it from
    ``facade_public_surface`` in the same change), so the allowlist always
    equals the live surface instead of silently keeping retired names."""

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
        stale = facade_surface_stale(path, class_name, allowed)
        if stale:
            violations.append(
                f"{class_name} facade surface allowlist is stale, remove: "
                f"{', '.join(stale)} (update "
                "scripts/architecture_boundary_baseline.json :: "
                f"facade_public_surface.{class_name}.allowed_names)"
            )
    return violations


FACADE_CLASS_FILES = {
    "RepositoryFacade": "backend/app/services/repository_facade.py",
    "SQLiteRepository": "backend/app/services/sqlite_repository.py",
    "PostgresRepository": "backend/app/services/postgres_repository.py",
}
# Receiver shapes that hold the facade instance itself: the notion
# scripts/audit_facade_callers.py (REPO_NAME_RE / RECEIVER_ATTR_NAMES) and
# backend/tests/architecture/repository_callers.py use, widened to the bare
# local names ``_repo`` / ``_repository``. Inside the three repository classes
# ``self`` is a receiver too.
#
# Known blind spots (no live code uses them today; the caller audit shares
# them): an alias (``r = repo; getattr(r, ...)``), ``operator.attrgetter``,
# a name built at run time (concatenation, a module constant, a variable), a
# receiver named outside the shapes above (``owner``, ``service``), and an
# aliased ``getattr``.
FACADE_RECEIVER_NAME_RE = re.compile(
    r"^(?:_?repo\d*|_?repository|facade|[A-Za-z_]\w*_repo)$"
)
FACADE_RECEIVER_ATTRS = frozenset(
    {"repository", "repo", "_repo", "_repository", "repository_facade"}
)
# Every receiver shape above contains one of these substrings, and every probe
# call names ``getattr``/``hasattr``, so a file lacking either set cannot hold
# a probe and is not parsed. Plain substrings, not a pattern over the call
# shape: comments, line breaks and parentheses inside the call do not matter.
_PROBE_CALL_WORDS = ("getattr", "hasattr")
_PROBE_RECEIVER_WORDS = ("repo", "facade")
# (path, enclosing function qualname, attribute) -> why a literal probe on a
# facade-shaped receiver may name an attribute no repository class defines.
DYNAMIC_FACADE_REFERENCE_EXEMPTIONS = {
    (
        "backend/app/services/startup_warmup.py",
        "_clear_repository_cache",
        "cache_clear",
    ): (
        "the receiver is the lru_cache-wrapped repository factory, not a "
        "facade instance"
    ),
}
# Call names that return the facade instance itself; a protocol a function
# returns them as (``-> P`` or ``cast(P, ...)``) is a promise about the facade.
FACADE_FACTORY_CALLS = frozenset({"repository", "create_application_repository"})
# Every spelling of a call to one of those names: the name, then any mix of
# whitespace, line continuations, closing grouping parens and comments, then
# ``(``. Files without a match cannot hold a binding and are not parsed.
_FACADE_FACTORY_CALL_RE = re.compile(
    r"\b(?:repository|create_application_repository)\b"
    r"(?:[\s)\\]|#[^\n]*)*\("
)


@functools.lru_cache(maxsize=256)
def _parse_source(source: str, filename: str) -> ast.Module:
    """Parse once per distinct source text (keyed by content, so a file
    rewritten in place is reparsed). Callers must not mutate the tree."""

    return ast.parse(source, filename=filename)


def _parse_file(path: Path) -> ast.Module:
    return _parse_source(path.read_text(encoding="utf-8"), str(path))


def _class_attribute_names(path: Path, class_name: str) -> set[str]:
    tree = _parse_file(path)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    names: set[str] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    for node in ast.walk(class_node):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(
                t.attr
                for t in targets
                if isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
            )
    return names


def _is_facade_receiver(node: ast.AST, self_is_facade: bool) -> bool:
    if isinstance(node, ast.Name):
        return bool(FACADE_RECEIVER_NAME_RE.match(node.id)) or (
            self_is_facade and node.id == "self"
        )
    return isinstance(node, ast.Attribute) and node.attr in FACADE_RECEIVER_ATTRS


def _literal_attribute_probes(
    tree: ast.AST, self_is_facade: bool = False
) -> list[tuple[str, str]]:
    """``(enclosing qualname, name)`` for every ``getattr(<facade receiver>,
    "<name>"[, default])`` and ``hasattr(<facade receiver>, "<name>")`` --
    the probes that degrade silently (``None`` / ``False``) instead of
    raising once ``<name>`` is retired. Module-level probes get qualname
    ``<module>``."""

    probes: list[tuple[str, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, (*scope, child.name))
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in _PROBE_CALL_WORDS
                and len(child.args) >= 2
                and _is_facade_receiver(child.args[0], self_is_facade)
                and isinstance(child.args[1], ast.Constant)
                and isinstance(child.args[1].value, str)
            ):
                probes.append((".".join(scope) or "<module>", child.args[1].value))
            visit(child, scope)

    visit(tree, ())
    return probes


def _weakref_seam_attributes(tree: ast.AST) -> list[str]:
    """Attributes read off ``_repository_from_weakref(...)`` -- the late-bound
    seams ``RepositoryFacade.__init__`` hands the runtime. The caller audit
    never attributes these to a facade method (the receiver is a call
    result), and the runtime callers swallow the AttributeError a retired
    name raises."""

    return [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_repository_from_weakref"
    ]


RUNTIME_CLASS_FILE = "backend/app/services/repository_runtime.py"


def _weakref_runtime_chains(tree: ast.AST) -> list[tuple[str, str]]:
    """``(component, member)`` for every
    ``_repository_from_weakref(...)._runtime.<component>.<member>`` -- a seam
    that skips the facade and resolves a runtime component's method at call
    time. Retiring or renaming that method raises inside the same swallowing
    runtime callers as a facade seam does."""

    chains: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Attribute)
            and node.value.value.attr == "_runtime"
        ):
            continue
        call = node.value.value.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_repository_from_weakref"
        ):
            chains.append((node.value.attr, node.attr))
    return chains


def _weakref_seam_shape_violations(tree: ast.AST) -> list[str]:
    """Every ``_repository_from_weakref(...)`` call must have one of the two
    shapes the seam checks can resolve: ``.<facade member>`` or
    ``._runtime.<component>.<member>``, followed by at most a call. Anything
    else -- the result bound to a name (plain or walrus), handed to
    ``getattr``, or a deeper attribute chain -- names attributes no check
    reads, so it fails. Identity is the enclosing qualname plus the
    expression, never a line number."""

    parents: dict[ast.AST, ast.AST] = {}
    scopes: dict[ast.AST, str] = {}

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, (*scope, child.name))
                continue
            scopes[child] = ".".join(scope) or "<module>"
            visit(child, scope)

    visit(tree, ())
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_repository_from_weakref"
        ):
            continue
        chain: list[str] = []
        end: ast.AST = node
        while isinstance(parents.get(end), ast.Attribute) and parents[end].value is end:
            end = parents[end]
            chain.append(end.attr)
        follower = parents.get(end)
        recognised = (
            (len(chain) == 1 and chain[0] != "_runtime")
            or (len(chain) == 3 and chain[0] == "_runtime")
        )
        if recognised and not isinstance(follower, ast.Subscript):
            continue
        violations.append(
            "facade weak-reference seam has a shape the seam checks cannot "
            f"read: {scopes.get(node, '<module>')} :: {ast.unparse(end)}"
        )
    return sorted(violations)


def _runtime_component_class(
    root: Path, component: str
) -> tuple[Path, ast.ClassDef] | None:
    """The class ``RepositoryRuntime.<component>`` holds: a class-body
    annotation or ``<component> = <Class>(...)``, a property's return
    annotation, then a ``self.<component>`` annotation or
    ``self.<component> = <Class>(...)`` inside a method. The first name that
    resolves to a class wins (``X | None`` resolves ``X``)."""

    path = root / RUNTIME_CLASS_FILE
    if not path.is_file():
        return None
    resolved = _resolve_class(root, path, "RepositoryRuntime")
    if resolved is None:
        return None

    def is_self_component(target: ast.AST) -> bool:
        return (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == component
        )

    def constructed(value: ast.AST) -> list[ast.AST]:
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            return [value.func]
        return []

    # Class-body statements may bind the bare name; inside a method only
    # ``self.<component>`` counts -- a same-named local there is not the
    # component.
    class_level: list[ast.AST] = []
    in_methods: list[ast.AST] = []
    for statement in resolved[1].body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == component
        ):
            class_level.append(statement.annotation)
        elif isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == component
            for target in statement.targets
        ):
            class_level.extend(constructed(statement.value))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name == component and statement.returns is not None:
                class_level.append(statement.returns)
            for node in ast.walk(statement):
                if isinstance(node, ast.AnnAssign) and is_self_component(node.target):
                    in_methods.append(node.annotation)
                elif isinstance(node, ast.Assign) and any(
                    is_self_component(target) for target in node.targets
                ):
                    in_methods.extend(constructed(node.value))
    candidates = class_level + in_methods
    for annotation in candidates:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            annotation = ast.parse(annotation.value, mode="eval").body
        for name in (n.id for n in ast.walk(annotation) if isinstance(n, ast.Name)):
            found = _resolve_class(root, resolved[0], name)
            if found is not None:
                return found
    return None


def _class_members_with_bases(
    root: Path, path: Path, class_node: ast.ClassDef,
    seen: frozenset[tuple[Path, str]] = frozenset(),
) -> set[str]:
    if (path, class_node.name) in seen:
        return set()
    names = _class_attribute_names(path, class_node.name)
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            found = _resolve_class(root, path, base.id)
            if found is not None:
                names |= _class_members_with_bases(
                    root, found[0], found[1], seen | {(path, class_node.name)}
                )
    return names


def _weakref_runtime_chain_violations(root: Path, tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for component, member in sorted(set(_weakref_runtime_chains(tree))):
        resolved = _runtime_component_class(root, component)
        if resolved is None:
            violations.append(
                "facade weak-reference seam reads a runtime component whose "
                f"class cannot be resolved: _runtime.{component}"
            )
            continue
        if member not in _class_members_with_bases(root, *resolved):
            violations.append(
                "facade weak-reference seam reads a missing "
                f"{resolved[1].name} attribute: _runtime.{component}.{member}"
            )
    return violations


def dynamic_facade_reference_violations(root: Path) -> list[str]:
    """Every facade attribute named only at run time must still exist: the
    weak-reference seams in ``repository_facade.py`` must resolve on
    ``RepositoryFacade`` itself (they serve both backends, so a name only one
    wrapper defines does not count; a seam that goes through
    ``._runtime.<component>.<member>`` must name a member of the component's
    class), and a literal ``getattr``/``hasattr``
    probe on a facade-shaped receiver in ``backend/app`` or ``scripts`` must
    resolve on some repository class (inside ``RepositoryFacade``, on the
    facade itself)."""

    facade_names = _class_attribute_names(
        root / FACADE_CLASS_FILES["RepositoryFacade"], "RepositoryFacade"
    )
    repository_names = set(facade_names)
    for class_name, rel in FACADE_CLASS_FILES.items():
        repository_names |= _class_attribute_names(root / rel, class_name)
    facade_path = root / FACADE_CLASS_FILES["RepositoryFacade"]
    violations = [
        "facade weak-reference seam reads a missing RepositoryFacade "
        f"attribute: {name}"
        for name in sorted(
            set(_weakref_seam_attributes(_parse_file(facade_path)))
            - facade_names
        )
    ]
    violations.extend(
        _weakref_runtime_chain_violations(root, _parse_file(facade_path))
    )
    violations.extend(_weakref_seam_shape_violations(_parse_file(facade_path)))
    class_files = set(FACADE_CLASS_FILES.values())
    used_exemptions: set[tuple[str, str, str]] = set()
    for base in (root / "backend" / "app", root / "scripts"):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8")
            in_class_file = rel in class_files
            if not in_class_file and not (
                any(word in source for word in _PROBE_CALL_WORDS)
                and any(word in source for word in _PROBE_RECEIVER_WORDS)
            ):
                continue
            known = (
                facade_names
                if rel == FACADE_CLASS_FILES["RepositoryFacade"]
                else repository_names
            )
            tree = _parse_source(source, str(path))
            for qualname, name in sorted(
                set(_literal_attribute_probes(tree, self_is_facade=in_class_file))
            ):
                if name in known:
                    continue
                if (rel, qualname, name) in DYNAMIC_FACADE_REFERENCE_EXEMPTIONS:
                    used_exemptions.add((rel, qualname, name))
                    continue
                violations.append(
                    "literal getattr/hasattr on a facade receiver names a "
                    f"missing repository attribute: {rel} :: {qualname} :: {name}"
                )
    violations.extend(
        "dynamic facade reference exemption is stale, remove: "
        f"{rel} :: {qualname} :: {name}"
        for rel, qualname, name in sorted(
            set(DYNAMIC_FACADE_REFERENCE_EXEMPTIONS) - used_exemptions
        )
    )
    return violations


def _module_file(root: Path, module: str) -> Path | None:
    base = root / "backend" / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _resolve_class(
    root: Path, path: Path, name: str, seen: frozenset[tuple[Path, str]] = frozenset()
) -> tuple[Path, ast.ClassDef] | None:
    """Find ``class <name>`` in ``path`` or follow ``from <module> import
    <name>`` (absolute ``app.*`` imports) to the module defining it."""

    if (path, name) in seen:
        return None
    tree = _parse_file(path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return path, node
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    target = _module_file(root, node.module)
                    if target is not None:
                        return _resolve_class(
                            root, target, alias.name, seen | {(path, name)}
                        )
    return None


def _protocol_members(root: Path, path: Path, name: str) -> set[str] | None:
    """Non-dunder members a protocol class and its protocol bases declare."""

    resolved = _resolve_class(root, path, name)
    if resolved is None:
        return None
    class_path, class_node = resolved
    members: set[str] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (
            node.name.startswith("__") and node.name.endswith("__")
        ):
            members.add(node.name)
    for base in class_node.bases:
        base_name = base.id if isinstance(base, ast.Name) else None
        if base_name in {None, "Protocol", "Generic"}:
            continue
        inherited = _protocol_members(root, class_path, base_name)
        if inherited is not None:
            members |= inherited
    return members


def _facade_call(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in FACADE_FACTORY_CALLS)
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in FACADE_FACTORY_CALLS
            )
        )
    )


def facade_protocol_bindings(root: Path) -> list[tuple[str, str, str]]:
    """``(path, function, protocol)`` for every place ``backend/app`` hands the
    facade out typed as a protocol: ``cast(P, repository())`` anywhere, and
    a function annotated ``-> P`` that returns ``repository()`` /
    ``create_application_repository(...)`` itself."""

    bindings: set[tuple[str, str, str]] = set()
    for path in sorted((root / "backend" / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if not _FACADE_FACTORY_CALL_RE.search(source):
            continue
        rel = path.relative_to(root).as_posix()
        tree = _parse_source(source, str(path))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "cast"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Name)
                    and _facade_call(node.args[1])
                ):
                    bindings.add((rel, function.name, node.args[0].id))
                elif (
                    isinstance(node, ast.Return)
                    and _facade_call(node.value)
                    and isinstance(function.returns, ast.Name)
                ):
                    bindings.add((rel, function.name, function.returns.id))
    return sorted(bindings)


def facade_protocol_binding_violations(
    root: Path, known_gaps: dict[str, list[str]]
) -> list[str]:
    """A protocol the facade is handed out as must not declare a method the
    facade lacks -- retiring a facade method that a ``memory_service()`` /
    ``ask_state_repository()``-style accessor still promises turns a typed
    call into an AttributeError at run time (nothing type-checks the cast).
    Gaps that predate this guard are listed per protocol in the baseline
    (``facade_protocol_binding_gaps``) with zero slack: a new gap fails, and a
    listed gap that closed must be removed."""

    facade_names = _class_attribute_names(
        root / FACADE_CLASS_FILES["RepositoryFacade"], "RepositoryFacade"
    )
    violations: list[str] = []
    protocols: dict[str, set[str]] = {}
    for rel, function, protocol in facade_protocol_bindings(root):
        members = _protocol_members(root, root / rel, protocol)
        if members is None:
            violations.append(
                f"cannot resolve protocol {protocol} bound to the facade at "
                f"{rel} :: {function}"
            )
            continue
        protocols[protocol] = members - facade_names
    for protocol, missing in sorted(protocols.items()):
        allowed = set(known_gaps.get(protocol, ()))
        added = sorted(missing - allowed)
        if added:
            violations.append(
                f"protocol {protocol} is bound to the facade but declares "
                f"methods RepositoryFacade lacks: {', '.join(added)}"
            )
        stale = sorted(allowed - missing)
        if stale:
            violations.append(
                f"facade protocol gap list for {protocol} is stale, remove: "
                f"{', '.join(stale)} (update "
                "scripts/architecture_boundary_baseline.json :: "
                "facade_protocol_binding_gaps)"
            )
    violations.extend(
        f"facade protocol gap list names an unbound protocol, remove: {protocol} "
        "(update scripts/architecture_boundary_baseline.json :: "
        "facade_protocol_binding_gaps)"
        for protocol in sorted(set(known_gaps) - set(protocols))
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


_PROTOCOL_HOME_MODULES = ("typing", "typing_extensions")


def _protocol_spellings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Resolve how ``Protocol`` may be spelled in this module.

    Returns ``(names, module_aliases)``: ``names`` are local identifiers bound
    to ``Protocol`` itself (``from typing import Protocol``, ``... as P``, or a
    ``from typing import *`` that binds the bare name), ``module_aliases`` are
    local identifiers bound to a module that exports it (``import typing``,
    ``import typing_extensions as te``) so ``t.Protocol`` is recognised too. A
    count that only knew the bare spelling could be dodged by aliasing the
    import (codex #566 R2 P2); resolving the module's own import statements
    closes that without guessing.
    """

    names: set[str] = set()
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _PROTOCOL_HOME_MODULES:
            for alias in node.names:
                if alias.name == "Protocol":
                    names.add(alias.asname or alias.name)
                elif alias.name == "*":
                    names.add("Protocol")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _PROTOCOL_HOME_MODULES:
                    module_aliases.add(alias.asname or alias.name)
    return names, module_aliases


def _has_protocol_base(
    base: ast.expr, names: set[str], module_aliases: set[str]
) -> bool:
    """True when ``base`` names ``Protocol`` under any spelling this module
    binds: a bare or aliased name (``Protocol``, ``P``), a qualified attribute
    on an imported typing module (``typing.Protocol``, ``te.Protocol``), or a
    subscripted generic of either (``Protocol[T]``) -- the last by recursing
    into the subscript's ``value``."""

    if isinstance(base, ast.Name):
        return base.id in names
    if isinstance(base, ast.Attribute):
        return (
            base.attr == "Protocol"
            and isinstance(base.value, ast.Name)
            and base.value.id in module_aliases
        )
    if isinstance(base, ast.Subscript):
        return _has_protocol_base(base.value, names, module_aliases)
    return False


def ports_protocol_method_count(root: Path) -> int:
    """Count every ``FunctionDef``/``AsyncFunctionDef`` nested directly inside
    a ``ClassDef`` that subclasses ``Protocol`` in
    ``backend/app/repositories/ports.py`` -- i.e. actual Protocol methods.
    Module-level functions such as ``project_run_row`` do not count, and
    neither do methods on the file's plain (non-Protocol) classes, such as
    the handful of exception classes defined here (``ConversationBusyError``
    and friends) -- only the consumer-facing method surface the Protocols
    themselves declare does."""

    path = root / "backend" / "app" / "repositories" / "ports.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names, module_aliases = _protocol_spellings(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(_has_protocol_base(base, names, module_aliases) for base in node.bases)
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def ports_method_count_violations(root: Path, ceiling: int) -> list[str]:
    """Zero-slack ceiling on ``ports.py``'s total Protocol method surface --
    same shape and rationale as ``function_length_violations`` above: grown
    past the recorded ceiling is a violation, and shrunk below it is *also* a
    violation (the baseline must be lowered in the same change). An only-grow
    ceiling would accumulate silent headroom a later regression could spend
    unnoticed; zero slack means the file's Protocol surface may only shrink,
    one reviewed step at a time."""

    actual = ports_protocol_method_count(root)
    if actual > ceiling:
        return [
            f"ports.py gained protocol methods: {actual} > {ceiling} "
            "(update scripts/architecture_boundary_baseline.json :: "
            "ports_protocol_method_count)"
        ]
    if actual < ceiling:
        return [
            "ports.py protocol method ceiling is stale, lower it: "
            f"{actual} < {ceiling} (update "
            "scripts/architecture_boundary_baseline.json :: "
            "ports_protocol_method_count)"
        ]
    return []


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
    violations.extend(dynamic_facade_reference_violations(root))
    violations.extend(
        facade_protocol_binding_violations(
            root, baseline["facade_protocol_binding_gaps"]
        )
    )
    violations.extend(
        function_length_violations(root, baseline["function_length_ceiling"])
    )
    violations.extend(
        ports_method_count_violations(root, baseline["ports_protocol_method_count"])
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
        "facade surface equals its allowlist; "
        "dynamic facade references resolve; facade-bound protocols "
        "match the facade; hot functions may only shrink; "
        "ports surface may only shrink)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
