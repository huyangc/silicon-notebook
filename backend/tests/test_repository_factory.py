from __future__ import annotations

import ast
import builtins
import inspect
import sys
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.repository_runtime import (
    RepositoryCompatibilitySeams,
    RepositoryRuntime,
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
    ("app/services/knowhow/projection.py", "EmbeddingStore", "rows_by_ids"),
    ("app/services/knowhow/projection.py", "KnowledgeStore", "legacy_typed_table_ids"),
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
        if relative == "app/repositories/sqlite/bundle.py":
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

        def canonicalize(value):
            name = value.rsplit(".", 1)[-1]
            sqlite_namespace = value.startswith("app.repositories.sqlite.")
            compat_namespace = value.startswith("app.services.sqlite")
            if sqlite_namespace or compat_namespace:
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
                or ("*" if value in SQLITE_TAINT_ROOTS else None)
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

        class ScopeCollector(ast.NodeVisitor):
            def __init__(self) -> None:
                self.imports: list[tuple[str, str, ast.AST]] = []
                self.bindings: list[tuple[str, ast.AST]] = []

            def visit_Import(self, node: ast.Import) -> None:
                for imported in node.names:
                    local = imported.asname or imported.name.split(".", 1)[0]
                    value = imported.name if imported.asname else local
                    self.imports.append((local, value, node))

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                module = import_module(node)
                for imported in node.names:
                    if imported.name == "*":
                        if module.startswith(
                            "app.repositories.sqlite"
                        ) or module == "app.services.sqlite_repository":
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
            bindings = [
                (name.id, name)
                for gen in node.generators
                for name in ast.walk(gen.target)
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
            ]
            local = dict(aliases)
            for name, binding in bindings:
                if name in local:
                    record(binding, local[name].rsplit(".", 1)[-1])
                    local.pop(name)
            for gen in node.generators:
                scan_expr(gen.iter, local)
                scan_expr(gen.target, local)
                for condition in gen.ifs:
                    scan_expr(condition, local)
            if isinstance(node, ast.DictComp):
                scan_expr(node.key, local)
                scan_expr(node.value, local)
            else:
                scan_expr(node.elt, local)

        def scan_expr(node: ast.AST | None, aliases: dict[str, str]) -> None:
            if node is None:
                return
            if isinstance(node, ast.Name):
                label = taint_label(resolve(node, aliases))
                if isinstance(node.ctx, ast.Load) and label:
                    record(node, label)
                return
            if isinstance(node, ast.Attribute):
                if (name := constructor_name(node, aliases)):
                    record(node, name)
                else:
                    resolved = resolve(node, aliases)
                    head = dotted_name(node).partition(".")[0]
                    if aliases.get(head) != "app" or resolved.startswith(
                        ("app.repositories.sqlite", "app.services.sqlite_repository")
                    ):
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
                        record(binding, local[name].rsplit(".", 1)[-1])
                        local.pop(name)
                scan_expr(node.body, local)
                return
            if isinstance(node, ast.NamedExpr):
                if node.target.id in aliases:
                    record(node.target, aliases[node.target.id].rsplit(".", 1)[-1])
                scan_expr(node.value, aliases)
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
                tainted = [(value, node) for _, value, node in declarations if taint_label(value)]
                inherited_value = inherited.get(name)
                if not tainted and inherited_value is None:
                    continue
                chosen = inherited_value or tainted[0][0]
                label = taint_label(chosen)
                for _, value, node in declarations:
                    if value != chosen:
                        record(node, label)
                aliases[name] = chosen
            for name, binding in (*parameters, *collector.bindings):
                if name in aliases:
                    record(binding, taint_label(aliases[name]))
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


def test_runtime_consumes_the_injected_persistence_bundle(tmp_path):
    delegate = _sqlite_bundle_factory_class()()
    recorded: dict[str, object] = {}

    class RecordingFactory:
        def create(self, **kwargs):
            recorded.update(kwargs)
            bundle = delegate.create(**kwargs)
            recorded["bundle"] = bundle
            return bundle

    settings = _settings(tmp_path)
    seams = _seams()
    runtime = RepositoryRuntime(
        settings=settings,
        root_dir=tmp_path,
        seams=seams,
        persistence_factory=RecordingFactory(),
    )
    bundle = recorded["bundle"]

    assert recorded["settings"] is settings
    assert recorded["root_dir"] is tmp_path
    assert recorded["seams"] is seams
    assert recorded["model_config_cache"] is runtime.model_config_cache
    assert runtime.identity.model_config_cache is runtime.model_config_cache
    assert runtime.database is bundle.database
    assert runtime.identity is bundle.identity
    assert runtime.notebook_store is bundle.notebooks
    assert runtime.sharing_store is bundle.sharing
    assert runtime.source_store is bundle.sources
    assert runtime.chunk_store is bundle.chunks
    assert runtime.embedding_store is bundle.embeddings
    assert runtime.knowledge is bundle.knowledge
    assert runtime.governance is bundle.governance
    assert runtime.index_projections is bundle.index_projection
    assert runtime.kg_build_jobs is bundle.kg_build_jobs
    assert runtime.knowhow_store is bundle.knowhow
    assert runtime.knowhow_transfer_store is bundle.knowhow_transfer
    assert runtime.memory_store is bundle.memory
    assert runtime.queries is bundle.queries
    assert runtime.report_store is bundle.reports
    assert runtime.ask_state is bundle.ask_state
    assert runtime.unified_kg is bundle.unified_kg


def test_runtime_late_wiring_uses_declared_methods_on_slot_only_store_ports(tmp_path):
    class SlotBindingStore:
        __slots__ = ("delegate", "binding", "method_name")

        def __init__(self, wrapped, method_name):
            self.delegate = wrapped
            self.binding = None
            self.method_name = method_name

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def bind_write(self, write):
            assert self.method_name == "bind_write"
            self.binding = write

        def bind_runtime_callbacks(self, **callbacks):
            assert self.method_name == "bind_runtime_callbacks"
            self.binding = callbacks

        def bind_insert_row(self, insert_row):
            assert self.method_name == "bind_insert_row"
            self.binding = insert_row

    class SlotBundle:
        __slots__ = (
            "database", "identity", "notebooks", "sharing", "sources", "chunks",
            "embeddings", "knowledge", "governance", "index_projection",
            "kg_build_jobs", "knowhow", "knowhow_transfer", "memory", "queries",
            "reports", "ask_state", "unified_kg",
        )

        def __init__(self, delegate):
            for name in self.__slots__:
                object.__setattr__(self, name, getattr(delegate, name))

    captured = {}
    class Factory:
        def create(self, **kwargs):
            delegate = _sqlite_bundle_factory_class()().create(**kwargs)
            bundle = SlotBundle(delegate)
            bundle.embeddings = SlotBindingStore(delegate.embeddings, "bind_write")
            bundle.index_projection = SlotBindingStore(
                delegate.index_projection, "bind_runtime_callbacks"
            )
            bundle.sharing = SlotBindingStore(delegate.sharing, "bind_insert_row")
            captured["bundle"] = bundle
            return bundle

    runtime = RepositoryRuntime(
        settings=_settings(tmp_path),
        root_dir=tmp_path,
        seams=_seams(),
        persistence_factory=Factory(),
    )
    runtime.wire_persistence(write=lambda: None)
    runtime.wire_scale_artifacts(
        connect=lambda: None,
        in_batches=lambda ids: [list(ids)],
        ent_chunk_map=lambda _notebook_id: {},
        mention_extra_edges=lambda _notebook_id: [],
        vector_matrix=lambda *_args: None,
        version=lambda _notebook_id: [],
        scale_cache=lambda: {},
        load_lock=lambda: object(),
        load_locks=lambda: {},
        note_model_error=lambda *_args, **_kwargs: None,
    )
    runtime.wire_sharing(
        insert_row=lambda *_args: None,
        copy_stats=lambda _notebook_id: {},
        storage_dir=lambda: tmp_path,
        schedule_projection=lambda _table_id: None,
    )

    bundle = captured["bundle"]
    assert bundle.embeddings.binding is not None
    assert set(bundle.index_projection.binding) == {
        "connect", "in_batches", "ent_chunk_map", "mention_extra_edges", "vector_matrix"
    }
    assert bundle.sharing.binding is not None


@pytest.mark.parametrize(
    ("port_name", "store_name", "method_name"),
    [
        ("EmbeddingStorePort", "EmbeddingStore", "bind_write"),
        ("IndexProjectionStorePort", "IndexProjectionStore", "bind_runtime_callbacks"),
        ("SharingStorePort", "SharingStore", "bind_insert_row"),
    ],
)
def test_portable_late_binding_signatures_match_sqlite_stores(
    port_name, store_name, method_name
):
    from app.repositories import ports
    from app.repositories.sqlite import embedding_store, index_projection_store, sharing_store

    concrete_modules = {
        "EmbeddingStore": embedding_store,
        "IndexProjectionStore": index_projection_store,
        "SharingStore": sharing_store,
    }
    port_method = getattr(getattr(ports, port_name), method_name)
    store_method = getattr(getattr(concrete_modules[store_name], store_name), method_name)
    assert inspect.signature(port_method) == inspect.signature(store_method)


def test_sqlite_bundle_factory_is_the_only_persistence_construction_root():
    app_root = Path(__file__).resolve().parents[1] / "app"
    bundle_path = app_root / "repositories" / "sqlite" / "bundle.py"
    sources = {
        path.relative_to(app_root.parent).as_posix(): path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
        if path != bundle_path
    }
    offenders = _sqlite_persistence_construction_sites_from_sources(sources)

    assert offenders == []


@pytest.mark.parametrize(
    ("relative", "source", "constructor"),
    [
        (
            "app/escape.py",
            "import app.repositories.sqlite.database as sqlite_db\n"
            "sqlite_db.SqliteDatabase(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "DB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.embedding_store import EmbeddingStore as Vectors\n"
            "Vectors(write=write)\n",
            "EmbeddingStore",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite import sharing_store as stores\n"
            "stores.SharingStore(database, settings, now=now, insert_row=insert)\n",
            "SharingStore",
        ),
        (
            "app/repositories/sqlite/escape.py",
            "from .database import SqliteDatabase as DB\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/repositories/sqlite/escape.py",
            "from . import database as db\ndb.SqliteDatabase(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/repositories/sqlite/nested/escape.py",
            "from ..database import SqliteDatabase as DB\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "DB = SqliteDatabase\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "import app.repositories.sqlite.database as sqlite_db\n"
            "Ctor = sqlite_db.SqliteDatabase\nCtor(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "DB = SqliteDatabase\nCtor = DB\nCtor(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "DB: type = SqliteDatabase\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "DB = Alias = SqliteDatabase\nAlias(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "DB, ignored = SqliteDatabase, object\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "[DB, ignored] = [SqliteDatabase, object]\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import SqliteDatabase\n"
            "(DB := SqliteDatabase)\nDB(settings, root)\n",
            "SqliteDatabase",
        ),
    ],
)
def test_sqlite_construction_guard_resolves_qualified_and_aliased_calls(
    relative, source, constructor
):
    findings = _sqlite_persistence_construction_sites_from_sources(
        {relative: source}
    )
    assert len(findings) == 1
    assert findings[0].endswith(f":{constructor}")


def test_sqlite_construction_guard_allows_wrapper_static_helper_imports():
    source = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "result = KnowledgeStore.source_ids_from_evidence(evidence)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/services/sqlite_repository.py": source}
    ) == []


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        (
            "app/escape.py",
            "from app.repositories.sqlite.database import *\n",
        ),
        (
            "app/repositories/sqlite/escape.py",
            "from .database import *\n",
        ),
    ],
)
def test_sqlite_construction_guard_fails_closed_on_sqlite_star_imports(
    relative, source
):
    assert _sqlite_persistence_construction_sites_from_sources(
        {relative: source}
    ) == [f"{relative}:1:*"]


def test_sqlite_construction_guard_ignores_non_sqlite_star_imports_and_aliases():
    source = (
        "from app.models.sources import *\n"
        "from app.models.sources import SourceDetail as helper\n"
        "ordinary = helper\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/services/sqlite_repository.py": source}
    ) == []


@pytest.mark.parametrize(
    "binding",
    [
        "DB: type = Fake",
        "DB = Alias = Fake",
        "DB, ignored = Fake, object",
        "[DB, ignored] = [Fake, object]",
        "(DB := Fake)",
    ],
)
def test_sqlite_construction_guard_ignores_non_sqlite_assignment_forms(binding):
    source = f"from app.models.sources import SourceDetail as Fake\n{binding}\nDB()\n"
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/ordinary.py": source}
    ) == []


def test_sqlite_construction_guard_fails_closed_on_lexical_shadowing():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "def violation():\n"
        "    DB = SqliteDatabase\n"
        "    DB(settings, root)\n"
        "def parameter_shadow(SqliteDatabase):\n"
        "    SqliteDatabase(settings, root)\n"
        "def local_shadow():\n"
        "    SqliteDatabase = Fake\n"
        "    SqliteDatabase(settings, root)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/scoped.py": source}
    ) == [
        "app/scoped.py:3:SqliteDatabase",
        "app/scoped.py:5:SqliteDatabase",
        "app/scoped.py:8:SqliteDatabase",
    ]


def test_sqlite_construction_guard_scans_definition_expressions_in_parent_scope():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "def escaped(default=SqliteDatabase(settings, root)):\n"
        "    return default\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/defaults.py": source}
    ) == ["app/defaults.py:2:SqliteDatabase"]


def test_sqlite_construction_guard_does_not_close_over_class_aliases():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "class Example:\n"
        "    ClassAlias = SqliteDatabase\n"
        "    def violation(self):\n"
        "        SqliteDatabase(settings, root)\n"
        "    def class_alias_is_not_a_closure(self):\n"
        "        ClassAlias(settings, root)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/class_scope.py": source}
    ) == [
        "app/class_scope.py:3:SqliteDatabase",
        "app/class_scope.py:5:SqliteDatabase",
    ]


@pytest.mark.parametrize(
    "expression",
    [
        "[SqliteDatabase() for SqliteDatabase in values]",
        "{SqliteDatabase() for SqliteDatabase in values}",
        "(SqliteDatabase() for SqliteDatabase in values)",
        "{SqliteDatabase(): SqliteDatabase() for SqliteDatabase in values}",
    ],
)
def test_sqlite_construction_guard_rejects_comprehension_target_shadowing(expression):
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        f"result = {expression}\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/comprehension_shadow.py": source}
    ) == ["app/comprehension_shadow.py:2:SqliteDatabase"]


@pytest.mark.parametrize(
    "pattern",
    [
        "SqliteDatabase",
        "[head, *SqliteDatabase]",
        "{'key': value, **SqliteDatabase}",
        "[SqliteDatabase, [nested]]",
        "Box(value=SqliteDatabase)",
    ],
)
def test_sqlite_construction_guard_rejects_match_pattern_shadowing(pattern):
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "match value:\n"
        f"    case {pattern}:\n"
        "        SqliteDatabase(settings, root)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/match_capture.py": source}
    ) == ["app/match_capture.py:3:SqliteDatabase"]


def test_sqlite_construction_guard_does_not_treat_match_class_as_a_capture():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "match value:\n"
        "    case SqliteDatabase():\n"
        "        SqliteDatabase(settings, root)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/match_class.py": source}
    ) == [
        "app/match_class.py:3:SqliteDatabase",
        "app/match_class.py:4:SqliteDatabase",
    ]


@pytest.mark.parametrize(
    "runtime_use",
    [
        "wrapped = partial(KnowledgeStore)",
        "def escaped(default=KnowledgeStore):\n    return default",
        "@decorate(KnowledgeStore)\ndef escaped():\n    pass",
        "def escaped():\n    return KnowledgeStore",
        "def escaped():\n    yield KnowledgeStore",
        "values = [KnowledgeStore]",
        "holder.value = KnowledgeStore",
        "holder[0] = KnowledgeStore",
        "KnowledgeStore.__new__(KnowledgeStore)",
        "KnowledgeStore.__call__()",
        "class Escaped(KnowledgeStore):\n    pass",
    ],
)
def test_sqlite_construction_guard_rejects_tainted_runtime_loads(runtime_use):
    source = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        f"{runtime_use}\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/runtime_escape.py": source}
    )


@pytest.mark.parametrize(
    "source",
    [
        "from app.services.sqlite_repository import KnowledgeStore as Store\n"
        "consume(Store)\n",
        "import app.services.sqlite_repository as legacy\n"
        "consume(legacy.KnowledgeStore)\n",
    ],
)
def test_sqlite_construction_guard_taints_compat_reexports(source):
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/compat_escape.py": source}
    )


@pytest.mark.parametrize(
    "source",
    [
        "from app.repositories.sqlite.governance_store import KnowledgeStore\n"
        "consume(KnowledgeStore)\n",
        "import app.repositories.sqlite.governance_store as stores\n"
        "consume(stores.KnowledgeStore)\n",
        "from app.services.sqlite_compat import KnowledgeStore\n"
        "consume(KnowledgeStore)\n",
    ],
)
def test_sqlite_construction_guard_taints_same_name_compat_reexports(source):
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/reexport_escape.py": source}
    ) == ["app/reexport_escape.py:2:KnowledgeStore"]


def test_sqlite_construction_guard_ignores_postgres_same_name_classes():
    source = (
        "from app.repositories.postgres.knowledge_store import KnowledgeStore\n"
        "consume(KnowledgeStore)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/postgres_control.py": source}
    ) == []


@pytest.mark.parametrize(
    ("source", "label"),
    [
        (
            "from app.repositories.sqlite.knowledge_store import KnowledgeStore as Store\n"
            "from app.models.sources import SourceDetail as Store\n",
            "KnowledgeStore",
        ),
        (
            "from app.models.sources import SourceDetail as Store\n"
            "from app.repositories.sqlite.knowledge_store import KnowledgeStore as Store\n",
            "KnowledgeStore",
        ),
        (
            "import app.repositories.sqlite.database as store_module\n"
            "import app.models.sources as store_module\n",
            "SqliteDatabase",
        ),
        (
            "import app.models.sources as store_module\n"
            "import app.repositories.sqlite.database as store_module\n",
            "SqliteDatabase",
        ),
    ],
)
def test_sqlite_construction_guard_rejects_ambiguous_mixed_imports(source, label):
    findings = _sqlite_persistence_construction_sites_from_sources(
        {"app/import_shadow.py": source}
    )
    assert len(findings) == 1
    assert findings[0].endswith(f":{label}")


def test_sqlite_construction_guard_rejects_import_shadow_of_inherited_taint():
    source = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "def escaped():\n"
        "    from app.models.sources import SourceDetail as KnowledgeStore\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/import_shadow.py": source}
    ) == ["app/import_shadow.py:3:KnowledgeStore"]


@pytest.mark.parametrize(
    ("source", "label"),
    [
        (
            "import app.repositories.sqlite.database as db\nconsume(db)\n",
            "SqliteDatabase",
        ),
        (
            "from app.repositories.sqlite import database as db\nholder.mod = db\n",
            "SqliteDatabase",
        ),
        (
            "import app.services.sqlite_repository as legacy\nconsume(legacy)\n",
            "*",
        ),
    ],
)
def test_sqlite_construction_guard_rejects_module_alias_runtime_loads(source, label):
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/module_escape.py": source}
    ) == [f"app/module_escape.py:2:{label}"]


def test_sqlite_construction_guard_allows_non_sqlite_module_runtime_loads():
    source = "import app.models.sources as models\nconsume(models)\n"
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/module_control.py": source}
    ) == []


def test_sqlite_construction_guard_helper_allowlist_is_direct_and_exact():
    allowed = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "KnowledgeStore.source_ids_from_evidence(payload)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/services/sqlite_repository.py": allowed}
    ) == []

    wrong_file = _sqlite_persistence_construction_sites_from_sources(
        {"app/other.py": allowed}
    )
    wrong_method = _sqlite_persistence_construction_sites_from_sources(
        {
            "app/services/sqlite_repository.py": (
                "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
                "KnowledgeStore.unlisted_helper(payload)\n"
            )
        }
    )
    propagated = _sqlite_persistence_construction_sites_from_sources(
        {
            "app/services/sqlite_repository.py": (
                "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
                "helper = KnowledgeStore.source_ids_from_evidence\n"
            )
        }
    )
    assert wrong_file and wrong_method and propagated


def test_sqlite_direct_helper_allowlist_contains_only_live_class_helpers():
    app_root = Path(__file__).resolve().parents[1] / "app"
    for relative, class_name, method_name in SQLITE_DIRECT_HELPER_ALLOWLIST:
        module_name = SQLITE_CONSTRUCTOR_MODULES[class_name]
        module = importlib.import_module(f"app.repositories.sqlite.{module_name}")
        descriptor = inspect.getattr_static(getattr(module, class_name), method_name)
        assert isinstance(descriptor, (staticmethod, classmethod)), (
            f"{class_name}.{method_name} is not a static/class helper"
        )

        tree = ast.parse((app_root.parent / relative).read_text(encoding="utf-8"))
        direct_call_exists = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method_name
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == class_name
            for node in ast.walk(tree)
        )
        assert direct_call_exists, (
            f"stale helper allowlist entry: {(relative, class_name, method_name)}"
        )


def test_sqlite_construction_guard_allows_only_pure_type_annotations():
    pure = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "value: KnowledgeStore\n"
        "items: list[KnowledgeStore]\n"
        "def typed(value: KnowledgeStore) -> KnowledgeStore:\n"
        "    return value\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/annotations.py": pure}
    ) == []

    runtime_annotation = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "value: KnowledgeStore()\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/annotations.py": runtime_annotation}
    )


def test_sqlite_construction_guard_scans_only_annotation_call_subtrees():
    prefix = (
        "from typing import Annotated\n"
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
    )
    pure_type = prefix + "value: Annotated[KnowledgeStore, metadata_factory()]\n"
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/annotations.py": pure_type}
    ) == []

    for annotation in (
        "KnowledgeStore()",
        "Annotated[KnowledgeStore, metadata_factory(KnowledgeStore)]",
    ):
        source = prefix + f"value: {annotation}\n"
        assert _sqlite_persistence_construction_sites_from_sources(
            {"app/annotations.py": source}
        )


@pytest.mark.parametrize(
    "shadow",
    [
        "def escaped(KnowledgeStore):\n    pass",
        "KnowledgeStore = replacement",
        "for KnowledgeStore in values:\n    pass",
        "with manager() as KnowledgeStore:\n    pass",
        "try:\n    run()\nexcept Error as KnowledgeStore:\n    pass",
        "values = [item for KnowledgeStore in items]",
        "match value:\n    case KnowledgeStore:\n        pass",
        "def KnowledgeStore():\n    pass",
        "class KnowledgeStore:\n    pass",
    ],
)
def test_sqlite_construction_guard_rejects_ambiguous_shadow_bindings(shadow):
    source = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        f"{shadow}\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/shadow_escape.py": source}
    )


def test_sqlite_construction_guard_does_not_inherit_class_local_aliases():
    source = (
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "class Outer:\n"
        "    Alias = KnowledgeStore\n"
        "    def method(self):\n"
        "        consume(Alias)\n"
        "    class Nested:\n"
        "        consume(Alias)\n"
    )
    findings = _sqlite_persistence_construction_sites_from_sources(
        {"app/class_alias.py": source}
    )
    assert findings == ["app/class_alias.py:3:KnowledgeStore"]


def test_create_repository_selects_sqlite_from_only_the_active_url(monkeypatch, tmp_path):
    factory = _repository_factory_module()

    sentinel = object()
    monkeypatch.setattr(factory, "SQLiteRepository", lambda _settings: sentinel)
    settings = _settings(
        tmp_path,
        shadow_database_url="postgresql://shadow:secret@db.example/shadow",
    )

    assert factory.create_repository(settings) is sentinel


def test_postgresql_repository_import_is_lazy(monkeypatch, tmp_path):
    module_name = "app.repositories.postgres.repository"
    sys.modules.pop(module_name, None)
    factory = _repository_factory_module()

    assert module_name not in sys.modules
    sqlite_sentinel = object()
    monkeypatch.setattr(factory, "SQLiteRepository", lambda _settings: sqlite_sentinel)
    assert factory.create_repository(_settings(tmp_path)) is sqlite_sentinel
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


def test_sqlite_wrapper_mineru_constructor_monkeypatches_remain_authoritative(
    monkeypatch, tmp_path
):
    from app.services import sqlite_repository

    created: list[tuple[str, Settings]] = []

    class FakeMinerUClient:
        def __init__(self, settings):
            created.append(("local", settings))

    class FakeMinerUCloudClient:
        def __init__(self, settings):
            created.append(("cloud", settings))

    monkeypatch.setattr(sqlite_repository, "MinerUClient", FakeMinerUClient)
    monkeypatch.setattr(sqlite_repository, "MinerUCloudClient", FakeMinerUCloudClient)
    settings = _settings(tmp_path)

    repo = sqlite_repository.SQLiteRepository(settings)

    assert repo.mineru_client.__class__ is FakeMinerUClient
    assert repo.mineru_cloud_client.__class__ is FakeMinerUCloudClient
    assert created == [("local", settings), ("cloud", settings)]


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


def test_real_postgresql_selection_fails_explicitly_without_sqlite_fallback(
    monkeypatch, tmp_path
):
    module_name = "app.repositories.postgres.repository"
    sys.modules.pop(module_name, None)
    sys.modules.pop("app.repositories.postgres", None)
    factory = _repository_factory_module()
    monkeypatch.setattr(
        factory,
        "SQLiteRepository",
        lambda _settings: pytest.fail("PostgreSQL selection fell back to SQLite"),
    )
    settings = _settings(
        tmp_path,
        database_url="postgresql://secret-user:secret-password@db.example/notebook",
    )

    with pytest.raises(factory.RepositoryBackendUnavailableError) as captured:
        factory.create_repository(settings)

    assert str(captured.value) == "PostgreSQL repository backend is not available"
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
