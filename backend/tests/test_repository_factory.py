from __future__ import annotations

import ast
import builtins
import sys
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.repository_runtime import (
    RepositoryCompatibilitySeams,
)


SQLITE_PERSISTENCE_CONSTRUCTORS = {
    "AskStateStore",
    "ChunkStore",
    "EmbeddingStore",
    "GovernanceStore",
    "IdentityStore",
    "IndexProjectionStore",
    "KgBuildJobStore",
    "KnowhowStore",
    "KnowhowTransferStore",
    "KnowledgeStore",
    "MemoryStore",
    "NotebookStore",
    "QueryStore",
    "ReportStore",
    "SharingStore",
    "SourceStore",
    "SqliteDatabase",
    "UnifiedKgStore",
}

SQLITE_CONSTRUCTOR_MODULES = {
    "AskStateStore": "ask_state_store",
    "ChunkStore": "chunk_store",
    "EmbeddingStore": "embedding_store",
    "GovernanceStore": "governance_store",
    "IdentityStore": "identity_store",
    "IndexProjectionStore": "index_projection_store",
    "KgBuildJobStore": "kg_build_job_store",
    "KnowhowStore": "knowhow_store",
    "KnowhowTransferStore": "knowhow_transfer_store",
    "KnowledgeStore": "knowledge_store",
    "MemoryStore": "memory_store",
    "NotebookStore": "notebook_store",
    "QueryStore": "query_store",
    "ReportStore": "report_store",
    "SharingStore": "sharing_store",
    "SourceStore": "source_store",
    "SqliteDatabase": "database",
    "UnifiedKgStore": "unified_kg_store",
}

SQLITE_CONSTRUCTOR_PATHS = {
    name: f"app.repositories.sqlite.{module}.{name}"
    for name, module in SQLITE_CONSTRUCTOR_MODULES.items()
}
SQLITE_PATH_CONSTRUCTORS = {
    path: name for name, path in SQLITE_CONSTRUCTOR_PATHS.items()
}
SQLITE_MODULE_CONSTRUCTORS = {
    f"app.repositories.sqlite.{module}": name
    for name, module in SQLITE_CONSTRUCTOR_MODULES.items()
}
SQLITE_TAINT_ROOTS = {
    "app",
    "app.repositories.sqlite",
    "app.services.sqlite_repository",
}

# Only existing direct static/class helper calls are exempt. A constructor class
# reference stored or passed at runtime remains forbidden, even for these methods.
SQLITE_DIRECT_HELPER_ALLOWLIST = {
    (
        "app/repositories/sqlite/governance_store.py",
        "KnowledgeStore",
        "replace_object_sources",
    ),
    (
        "app/repositories/sqlite/governance_store.py",
        "KnowledgeStore",
        "valid_object_ids",
    ),
    (
        "app/repositories/sqlite/maintenance.py",
        "KnowledgeStore",
        "source_ids_from_evidence",
    ),
    (
        "app/repositories/sqlite/notebook_store.py",
        "NotebookStore",
        "resolve_participants",
    ),
    ("app/services/sqlite_repository.py", "AskStateStore", "read_trace"),
    ("app/services/sqlite_repository.py", "GovernanceStore", "merge_evidence"),
    ("app/services/sqlite_repository.py", "GovernanceStore", "seed_for"),
    ("app/services/sqlite_repository.py", "KnowledgeStore", "delete_object_sources"),
    (
        "app/services/sqlite_repository.py",
        "KnowledgeStore",
        "source_ids_from_evidence",
    ),
    ("app/services/sqlite_repository.py", "SharingStore", "insert_row_values"),
}


def _sqlite_persistence_construction_sites_from_sources(
    sources: dict[str, str],
) -> list[str]:
    """Reject concrete SQLite constructor references outside the sole bundle root."""
    offenders: list[str] = []
    seen: set[tuple[str, ast.AST, str]] = set()

    for relative, source in sources.items():
        # bundle.py is the sole construction ROOT; database.py is the DEFINITION
        # module of SqliteDatabase — its own module-level introspection of the
        # class (e.g. ``SqliteDatabase.write.__wrapped__.__code__`` for the
        # write-lock guard, predating the bundle factory) references the name but
        # never CONSTRUCTS it, so it is a definition-site self-reference, not an
        # off-root construction.
        if relative in {
            "app/repositories/sqlite/bundle.py",
            "app/repositories/sqlite/database.py",
        }:
            continue
        tree = ast.parse(source, filename=relative)
        module_parts = list(Path(relative).with_suffix("").parts)
        module_name = ".".join(module_parts)
        package_parts = module_parts[:-1]

        def record(node: ast.AST, constructor: str) -> None:
            key = (relative, node, constructor)
            if key not in seen:
                seen.add(key)
                offenders.append(f"{relative}:{node.lineno}:{constructor}")

        def dotted_name(node: ast.AST) -> str:
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                parent = dotted_name(node.value)
                return f"{parent}.{node.attr}" if parent else ""
            return ""

        def repository_sqlite_namespace(value: str) -> bool:
            return value == "app.repositories.sqlite" or value.startswith(
                "app.repositories.sqlite."
            )

        def compatibility_sqlite_namespace(value: str) -> bool:
            return value.startswith("app.services.sqlite")

        def sqlite_namespace(value: str) -> bool:
            return repository_sqlite_namespace(value) or compatibility_sqlite_namespace(
                value
            )

        def navigation_namespace(value: str) -> bool:
            return value in {"app.repositories", "app.services"}

        def canonicalize(value):
            name = value.rsplit(".", 1)[-1]
            if sqlite_namespace(value):
                return SQLITE_CONSTRUCTOR_PATHS.get(name, value)
            return value

        def resolve(node: ast.AST, aliases: dict[str, str]) -> str:
            dotted = dotted_name(node)
            head, separator, tail = dotted.partition(".")
            if head not in aliases:
                return ""
            value = aliases[head]
            if separator:
                value = f"{value}.{tail}"
            return canonicalize(value)

        def constructor_name(node, aliases):
            return SQLITE_PATH_CONSTRUCTORS.get(resolve(node, aliases))

        def taint_label(value):
            value = canonicalize(value)
            return (
                SQLITE_PATH_CONSTRUCTORS.get(value)
                or SQLITE_MODULE_CONSTRUCTORS.get(value)
                or (
                    "*"
                    if value in SQLITE_TAINT_ROOTS
                    or (
                        compatibility_sqlite_namespace(value)
                        and value.count(".") == 2
                    )
                    else None
                )
            )

        def import_module(node: ast.ImportFrom) -> str:
            if not node.level:
                return node.module or ""
            base = package_parts[: len(package_parts) - node.level + 1]
            if node.module:
                base.extend(node.module.split("."))
            return ".".join(base)

        def argument_bindings(args):
            arguments = (
                *args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg
            )
            return [(arg.arg, arg) for arg in arguments if arg]

        def pattern_bindings(pattern: ast.pattern) -> list[tuple[str, ast.AST]]:
            bindings = []
            if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
                bindings.append((pattern.name, pattern))
            if isinstance(pattern, ast.MatchMapping) and pattern.rest:
                bindings.append((pattern.rest, pattern))
            for nested in ast.iter_child_nodes(pattern):
                if isinstance(nested, ast.pattern):
                    bindings.extend(pattern_bindings(nested))
            return bindings

        def static_assignment_pairs(target, value):
            if isinstance(target, ast.Name):
                return [(target.id, target, value)]
            if (
                isinstance(target, (ast.Tuple, ast.List))
                and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)
            ):
                return [
                    pair
                    for target_item, value_item in zip(target.elts, value.elts)
                    for pair in static_assignment_pairs(target_item, value_item)
                ]
            return []

        class ScopeCollector(ast.NodeVisitor):
            def __init__(self) -> None:
                self.imports: list[tuple[str, str, ast.AST]] = []
                self.bindings: list[tuple[str, ast.AST]] = []
                self.assignments: list[tuple[str, ast.AST, ast.AST]] = []

            def visit_Import(self, node: ast.Import) -> None:
                for imported in node.names:
                    local = imported.asname or imported.name.split(".", 1)[0]
                    value = imported.name if imported.asname else local
                    self.imports.append((local, value, node))

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                module = import_module(node)
                for imported in node.names:
                    if imported.name == "*":
                        if sqlite_namespace(module):
                            record(node, "*")
                        continue
                    local = imported.asname or imported.name
                    value = canonicalize(f"{module}.{imported.name}")
                    self.imports.append((local, value, node))

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.bindings.append((node.name, node))

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                own_definition = SQLITE_CONSTRUCTOR_PATHS.get(
                    node.name
                ) == f"{module_name}.{node.name}"
                if not own_definition:
                    self.bindings.append((node.name, node))

            def visit_Lambda(self, node: ast.Lambda) -> None:
                return

            def visit_ListComp(self, node: ast.ListComp) -> None:
                return

            visit_SetComp = visit_ListComp
            visit_DictComp = visit_ListComp
            visit_GeneratorExp = visit_ListComp

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Store):
                    self.bindings.append((node.id, node))

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    self.assignments.extend(
                        static_assignment_pairs(target, node.value)
                    )
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if node.value is not None:
                    self.assignments.extend(
                        static_assignment_pairs(node.target, node.value)
                    )
                self.generic_visit(node)

            def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
                self.assignments.extend(
                    static_assignment_pairs(node.target, node.value)
                )
                self.generic_visit(node)

            def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                if node.name:
                    self.bindings.append((node.name, node))
                self.generic_visit(node)

            def visit_match_case(self, node: ast.match_case) -> None:
                self.bindings.extend(pattern_bindings(node.pattern))
                self.generic_visit(node)

        def scan_annotation(node: ast.AST | None, aliases: dict[str, str]) -> None:
            if node is None:
                return
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    scan_expr(child, aliases)

        def scan_comprehension(node: ast.AST, aliases: dict[str, str]) -> None:
            generator_target_names = {
                name.id
                for gen in node.generators
                for name in ast.walk(gen.target)
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
            }
            local = dict(aliases)
            for gen in node.generators:
                scan_expr(gen.iter, local)
                bindings = [
                    (name.id, name)
                    for name in ast.walk(gen.target)
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
                ]
                for name, binding in bindings:
                    if name in local:
                        if label := taint_label(local[name]):
                            record(binding, label)
                        local.pop(name)
                scan_expr(gen.target, local)
                for condition in gen.ifs:
                    scan_expr(condition, local)
            if isinstance(node, ast.DictComp):
                scan_expr(node.key, local)
                scan_expr(node.value, local)
            else:
                scan_expr(node.elt, local)
            for name, value in local.items():
                if name not in generator_target_names and navigation_namespace(value):
                    aliases[name] = value

        def scan_expr(node: ast.AST | None, aliases: dict[str, str]) -> None:
            if node is None:
                return
            if isinstance(node, ast.Name):
                label = taint_label(resolve(node, aliases))
                if isinstance(node.ctx, ast.Load) and label:
                    record(node, label)
                return
            if isinstance(node, ast.Attribute):
                resolved = resolve(node, aliases)
                if (name := constructor_name(node, aliases)):
                    record(node, name)
                elif label := taint_label(resolved):
                    record(node, label)
                elif (
                    compatibility_sqlite_namespace(resolved)
                    and resolved.count(".") > 2
                ):
                    return
                else:
                    head = dotted_name(node).partition(".")[0]
                    if aliases.get(head) != "app" or sqlite_namespace(resolved):
                        scan_expr(node.value, aliases)
                return
            if isinstance(node, ast.Call):
                allowed = False
                if isinstance(node.func, ast.Attribute):
                    owner = constructor_name(node.func.value, aliases)
                    allowed = (
                        relative, owner, node.func.attr
                    ) in SQLITE_DIRECT_HELPER_ALLOWLIST
                if not allowed:
                    scan_expr(node.func, aliases)
                for arg in node.args:
                    scan_expr(arg, aliases)
                for keyword in node.keywords:
                    scan_expr(keyword.value, aliases)
                return
            if isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                scan_comprehension(node, aliases)
                return
            if isinstance(node, ast.Lambda):
                for default in (*node.args.defaults, *node.args.kw_defaults):
                    scan_expr(default, aliases)
                local = dict(aliases)
                for name, binding in argument_bindings(node.args):
                    if name in local:
                        if label := taint_label(local[name]):
                            record(binding, label)
                        local.pop(name)
                scan_expr(node.body, local)
                return
            if isinstance(node, ast.NamedExpr):
                if node.target.id in aliases:
                    if label := taint_label(aliases[node.target.id]):
                        record(node.target, label)
                scan_expr(node.value, aliases)
                resolved = resolve(node.value, aliases)
                if navigation_namespace(resolved):
                    aliases[node.target.id] = resolved
                return
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    scan_expr(child, aliases)

        def scan_definition_expressions(node, aliases: dict[str, str]) -> None:
            for decorator in node.decorator_list:
                scan_expr(decorator, aliases)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in (*node.args.defaults, *node.args.kw_defaults):
                    scan_expr(default, aliases)
                for _, argument in argument_bindings(node.args):
                    scan_annotation(argument.annotation, aliases)
                scan_annotation(node.returns, aliases)
            else:
                for base in node.bases:
                    scan_expr(base, aliases)
                for keyword in node.keywords:
                    scan_expr(keyword.value, aliases)

        def scan_statement(node, aliases, kind, class_outer) -> None:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan_definition_expressions(node, aliases)
                inherited = class_outer if kind == "class" else aliases
                scan_scope(node.body, inherited, "function", None, argument_bindings(node.args))
                return
            if isinstance(node, ast.ClassDef):
                scan_definition_expressions(node, aliases)
                inherited = class_outer if kind == "class" else aliases
                scan_scope(node.body, inherited, "class", inherited)
                return
            if isinstance(node, ast.AnnAssign):
                scan_annotation(node.annotation, aliases)
                scan_expr(node.value, aliases)
                scan_expr(node.target, aliases)
                return
            for child in ast.iter_child_nodes(node):
                scan_runtime(child, aliases, kind, class_outer)

        def scan_runtime(node, aliases, kind, class_outer) -> None:
            if isinstance(node, ast.expr):
                scan_expr(node, aliases)
            elif isinstance(node, ast.stmt):
                scan_statement(node, aliases, kind, class_outer)
            else:
                for child in ast.iter_child_nodes(node):
                    scan_runtime(child, aliases, kind, class_outer)

        def scan_scope(statements, inherited, kind, class_outer, parameters=()) -> None:
            collector = ScopeCollector()
            for statement in statements:
                collector.visit(statement)
            aliases = dict(inherited)
            names = {name for name, _, _ in collector.imports}
            for name in names:
                declarations = [item for item in collector.imports if item[0] == name]
                tainted = [
                    (value, node)
                    for _, value, node in declarations
                    if taint_label(value)
                ]
                navigation = [
                    (value, node)
                    for _, value, node in declarations
                    if navigation_namespace(value)
                ]
                inherited_value = inherited.get(name)
                inherited_tainted = inherited_value and taint_label(
                    inherited_value
                )
                if not tainted and not navigation and inherited_value is None:
                    continue
                chosen = (
                    inherited_value
                    if inherited_tainted
                    else tainted[0][0]
                    if tainted
                    else inherited_value
                    if inherited_value is not None
                    else navigation[0][0]
                )
                label = taint_label(chosen)
                for _, value, node in declarations:
                    if label and value != chosen:
                        record(node, label)
                aliases[name] = chosen
            navigation_names: set[str] = set()
            changed = True
            while changed:
                changed = False
                for name, _, value in collector.assignments:
                    resolved = resolve(value, aliases)
                    if not navigation_namespace(resolved):
                        continue
                    if name not in aliases:
                        aliases[name] = resolved
                        changed = True
                    if aliases.get(name) == resolved:
                        navigation_names.add(name)
            for name, binding in parameters:
                if name in aliases:
                    if label := taint_label(aliases[name]):
                        record(binding, label)
                    aliases.pop(name)
            for name, binding in collector.bindings:
                if name in aliases:
                    if name in navigation_names or navigation_namespace(aliases[name]):
                        continue
                    if label := taint_label(aliases[name]):
                        record(binding, label)
                    aliases.pop(name)
            for statement in statements:
                scan_statement(statement, aliases, kind, class_outer)

        own_name = SQLITE_MODULE_CONSTRUCTORS.get(module_name)
        initial = {own_name: SQLITE_CONSTRUCTOR_PATHS[own_name]} if own_name else {}
        scan_scope(tree.body, initial, "module", None)

    return offenders


def _sqlite_bundle_factory_class():
    module_name = "app.repositories.sqlite.bundle"
    assert importlib.util.find_spec(module_name) is not None, (
        "SQLite persistence bundle composition root is missing"
    )
    from app.repositories.sqlite.bundle import SqlitePersistenceBundleFactory

    return SqlitePersistenceBundleFactory


def _repository_factory_module():
    module_name = "app.repositories.factory"
    assert importlib.util.find_spec(module_name) is not None, (
        "central repository backend factory is missing"
    )
    from app.repositories import factory

    return factory


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'repository.db'}",
        "storage_dir": str(tmp_path / "storage"),
        "event_log_enabled": False,
        "llm_log_enabled": False,
        **overrides,
    }
    return Settings(**values)


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-sentinel",
        now=lambda: "2026-07-22T00:00:00",
        copy_chunk_size=lambda: 1000,
        remap_json_ids=lambda value, _maps: value,
        in_chunk_size=lambda: 900,
    )


def test_postgresql_repository_import_is_lazy(monkeypatch, tmp_path):
    module_name = "app.repositories.postgres.repository"
    sys.modules.pop(module_name, None)
    factory = _repository_factory_module()

    assert module_name not in sys.modules
    postgres_module = ModuleType(module_name)
    postgres_sentinel = object()
    postgres_module.PostgresRepository = lambda _settings: postgres_sentinel
    monkeypatch.setitem(sys.modules, module_name, postgres_module)
    settings = _settings(
        tmp_path,
        database_url="postgresql://active:secret@db.example/notebook",
    )
    assert factory.create_repository(settings) is postgres_sentinel


def test_create_repository_fails_closed_if_validated_identity_is_impossible(monkeypatch):
    factory = _repository_factory_module()

    monkeypatch.setattr(
        factory,
        "database_identity",
        lambda _url: SimpleNamespace(scheme="mysql"),
    )

    with pytest.raises(
        AssertionError,
        match="validated settings returned an unsupported scheme",
    ):
        factory.create_repository(SimpleNamespace(database_url="ignored"))


def test_real_postgresql_selection_uses_adapter_and_redacts_startup_failure(tmp_path):
    factory = _repository_factory_module()
    settings = _settings(
        tmp_path,
        database_url="postgresql://secret-user:secret-password@127.0.0.1:1/notebook",
        postgres_pool_acquire_timeout_seconds=1,
    )

    from app.repositories.postgres.database import PostgresDatabaseError

    with pytest.raises(PostgresDatabaseError) as captured:
        factory.create_repository(settings)

    assert "PostgreSQL pool startup failed" in str(captured.value)
    assert "secret-user" not in str(captured.value)
    assert "secret-password" not in str(captured.value)


def test_postgresql_selection_does_not_mask_nested_import_failures(monkeypatch, tmp_path):
    factory = _repository_factory_module()
    sys.modules.pop("app.repositories.postgres.repository", None)
    sys.modules.pop("app.repositories.postgres", None)
    real_import = builtins.__import__

    def import_with_missing_driver(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.repositories.postgres.repository":
            raise ModuleNotFoundError(
                "missing nested driver", name="missing_pg_driver"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_with_missing_driver)
    settings = _settings(
        tmp_path,
        database_url="postgresql://active:secret@db.example/notebook",
    )

    with pytest.raises(ModuleNotFoundError, match="missing nested driver"):
        factory.create_repository(settings)


def test_backend_status_delegates_database_identity_to_python_helper():
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts" / "backend.sh").read_text(encoding="utf-8")

    assert "from app.core.database_url import database_status" in script
    assert "print(database_status(Settings().database_url))" in script
    assert "urlsplit" not in script
    assert "DATABASE_URL#" not in script


@pytest.mark.parametrize("relative", ("scripts/prod.sh", "packaging/start.sh"))
def test_production_launchers_remain_single_worker(relative):
    import re

    root = Path(__file__).resolve().parents[2]
    script = (root / relative).read_text(encoding="utf-8")

    command_lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    assert re.findall(r"--workers\s+(\S+)", "\n".join(command_lines)) == ["1"]
