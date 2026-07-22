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


def _sqlite_persistence_construction_sites_from_sources(
    sources: dict[str, str],
) -> list[str]:
    offenders: list[str] = []
    for relative, source in sources.items():
        tree = ast.parse(source, filename=relative)
        module_parts = list(Path(relative).with_suffix("").parts)
        package_parts = (
            module_parts if module_parts[-1:] == ["__init__"] else module_parts[:-1]
        )
        if package_parts[-1:] == ["__init__"]:
            package_parts.pop()
        def dotted_name(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                parent = dotted_name(node.value)
                return f"{parent}.{node.attr}" if parent else node.attr
            return ""

        scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

        def scope_nodes(scope):
            roots = [scope.body] if isinstance(scope, ast.Lambda) else scope.body
            pending = list(reversed(roots))
            while pending:
                node = pending.pop()
                yield node
                if isinstance(node, scope_types):
                    body_nodes = (
                        {id(node.body)}
                        if isinstance(node, ast.Lambda)
                        else {id(statement) for statement in node.body}
                    )
                    pending.extend(
                        reversed(
                            [
                                child
                                for child in ast.iter_child_nodes(node)
                                if id(child) not in body_nodes
                            ]
                        )
                    )
                    continue
                pending.extend(reversed(list(ast.iter_child_nodes(node))))

        def bound_names(target):
            if isinstance(target, ast.Name):
                return {target.id}
            if isinstance(target, (ast.Tuple, ast.List)):
                return {
                    name
                    for element in target.elts
                    for name in bound_names(element)
                }
            if isinstance(target, ast.Starred):
                return bound_names(target.value)
            return set()

        def argument_names(scope):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return set()
            args = scope.args
            names = {
                argument.arg
                for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            }
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
            return names

        def scan_scope(scope, inherited_modules, inherited_symbols):
            nodes = list(scope_nodes(scope))
            local_names = argument_names(scope)
            explicit_outer_names: set[str] = set()
            assignments: list[tuple[str, ast.expr]] = []

            def add_assignment(target, value):
                if isinstance(target, ast.Name):
                    local_names.add(target.id)
                    assignments.append((target.id, value))
                    return
                if (
                    isinstance(target, (ast.Tuple, ast.List))
                    and isinstance(value, (ast.Tuple, ast.List))
                    and len(target.elts) == len(value.elts)
                ):
                    for target_element, value_element in zip(target.elts, value.elts):
                        add_assignment(target_element, value_element)
                    return
                local_names.update(bound_names(target))

            for node in nodes:
                if isinstance(node, (ast.Global, ast.Nonlocal)):
                    explicit_outer_names.update(node.names)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        add_assignment(target, node.value)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    add_assignment(node.target, node.value)
                elif isinstance(node, ast.NamedExpr):
                    add_assignment(node.target, node.value)
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    local_names.update(bound_names(node.target))
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars:
                            local_names.update(bound_names(item.optional_vars))
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    local_names.add(node.name)
                elif isinstance(node, scope_types) and not isinstance(node, ast.Lambda):
                    local_names.add(node.name)
                elif isinstance(node, ast.Import):
                    local_names.update(
                        alias.asname or alias.name.split(".", 1)[0]
                        for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom):
                    local_names.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name != "*"
                    )

            local_names.difference_update(explicit_outer_names)
            module_aliases = {
                name: target
                for name, target in inherited_modules.items()
                if name not in local_names
            }
            symbol_aliases = {
                name: target
                for name, target in inherited_symbols.items()
                if name not in local_names
            }

            def resolve_name(node):
                dotted = dotted_name(node)
                head, separator, tail = dotted.partition(".")
                suffix = f".{tail}" if separator else ""
                if head in symbol_aliases:
                    return symbol_aliases[head] + suffix
                if head in module_aliases:
                    return module_aliases[head] + suffix
                return dotted

            for node in nodes:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        local = alias.asname or alias.name.split(".", 1)[0]
                        module_aliases[local] = (
                            alias.name if alias.asname else local
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        keep = len(package_parts) - (node.level - 1)
                        base_parts = package_parts[:max(0, keep)]
                        if node.module:
                            base_parts.extend(node.module.split("."))
                        resolved_module = ".".join(base_parts)
                    else:
                        resolved_module = node.module or ""
                    for alias in node.names:
                        if alias.name == "*" and (
                            resolved_module == "app.repositories.sqlite"
                            or resolved_module.startswith(
                                "app.repositories.sqlite."
                            )
                        ):
                            offenders.append(f"{relative}:{node.lineno}:*")
                            continue
                        local = alias.asname or alias.name
                        target = ".".join(
                            part
                            for part in (resolved_module, alias.name)
                            if part
                        )
                        if resolved_module == "app.repositories.sqlite":
                            module_aliases[local] = target
                        else:
                            symbol_aliases[local] = target

            for target, _value in assignments:
                module_aliases.pop(target, None)
                symbol_aliases.pop(target, None)
            changed = True
            while changed:
                changed = False
                for target, value in assignments:
                    resolved = resolve_name(value)
                    constructor = resolved.rsplit(".", 1)[-1]
                    if (
                        resolved.startswith("app.repositories.sqlite.")
                        and constructor in SQLITE_PERSISTENCE_CONSTRUCTORS
                        and symbol_aliases.get(target) != resolved
                    ):
                        symbol_aliases[target] = resolved
                        changed = True

            for node in nodes:
                if not isinstance(node, ast.Call):
                    continue
                resolved = resolve_name(node.func)
                constructor = resolved.rsplit(".", 1)[-1]
                if (
                    resolved.startswith("app.repositories.sqlite.")
                    and constructor in SQLITE_PERSISTENCE_CONSTRUCTORS
                ):
                    offenders.append(f"{relative}:{node.lineno}:{constructor}")

            child_modules = module_aliases
            child_symbols = symbol_aliases
            for node in nodes:
                if not isinstance(node, scope_types):
                    continue
                if isinstance(scope, ast.ClassDef):
                    scan_scope(node, inherited_modules, inherited_symbols)
                else:
                    scan_scope(node, child_modules, child_symbols)

        scan_scope(tree, {}, {})
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
        "helper = KnowledgeStore.source_ids_from_evidence\n"
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
        "from app.repositories.sqlite.knowledge_store import KnowledgeStore\n"
        "helper = KnowledgeStore.source_ids_from_evidence\n"
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


def test_sqlite_construction_guard_keeps_aliases_in_their_lexical_scope():
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
    ) == ["app/scoped.py:4:SqliteDatabase"]


def test_sqlite_construction_guard_scans_definition_expressions_in_parent_scope():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "def escaped(default=SqliteDatabase(settings, root)):\n"
        "    return default\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/defaults.py": source}
    ) == ["app/defaults.py:2:SqliteDatabase"]


def test_sqlite_construction_guard_scans_class_methods_once_without_class_aliases():
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
    ) == ["app/class_scope.py:5:SqliteDatabase"]


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
