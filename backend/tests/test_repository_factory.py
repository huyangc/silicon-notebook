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
    """Interpret constructor aliases in Python evaluation order, per lexical scope."""
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

        def argument_names(args):
            names = {
                argument.arg
                for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            }
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
            return names

        def resolve_name(node, aliases):
            if isinstance(node, ast.NamedExpr):
                return resolve_name(node.value, aliases)
            dotted = dotted_name(node)
            head, separator, tail = dotted.partition(".")
            suffix = f".{tail}" if separator else ""
            return aliases.get(head, head) + suffix

        def is_sqlite_reference(value):
            return value == "app" or value.startswith("app.repositories.sqlite")

        def bind_name(name, value, aliases):
            if value and is_sqlite_reference(value):
                aliases[name] = value
            else:
                aliases.pop(name, None)

        def binding_values(target, value, aliases):
            if isinstance(target, ast.Name):
                return [(target.id, resolve_name(value, aliases))]
            if (
                isinstance(target, (ast.Tuple, ast.List))
                and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)
            ):
                return [
                    binding
                    for target_element, value_element in zip(
                        target.elts, value.elts
                    )
                    for binding in binding_values(
                        target_element, value_element, aliases
                    )
                ]
            return [(name, "") for name in bound_names(target)]

        def bind_target(target, value, aliases):
            bindings = binding_values(target, value, aliases)
            for name, resolved in bindings:
                bind_name(name, resolved, aliases)

        def unbind_target(target, aliases):
            for name in bound_names(target):
                aliases.pop(name, None)

        def resolved_import_module(node):
            if not node.level:
                return node.module or ""
            keep = len(package_parts) - (node.level - 1)
            base_parts = package_parts[:max(0, keep)]
            if node.module:
                base_parts.extend(node.module.split("."))
            return ".".join(base_parts)

        def pattern_captures(pattern):
            captures: set[str] = set()
            if isinstance(pattern, ast.MatchAs):
                if pattern.name:
                    captures.add(pattern.name)
                if pattern.pattern:
                    captures.update(pattern_captures(pattern.pattern))
            elif isinstance(pattern, ast.MatchStar):
                if pattern.name:
                    captures.add(pattern.name)
            elif isinstance(pattern, ast.MatchMapping):
                if pattern.rest:
                    captures.add(pattern.rest)
                for nested in pattern.patterns:
                    captures.update(pattern_captures(nested))
            elif isinstance(pattern, ast.MatchSequence):
                for nested in pattern.patterns:
                    captures.update(pattern_captures(nested))
            elif isinstance(pattern, ast.MatchClass):
                for nested in (*pattern.patterns, *pattern.kwd_patterns):
                    captures.update(pattern_captures(nested))
            elif isinstance(pattern, ast.MatchOr):
                for nested in pattern.patterns:
                    captures.update(pattern_captures(nested))
            return captures

        def record_call(node, aliases):
            resolved = resolve_name(node.func, aliases)
            constructor = resolved.rsplit(".", 1)[-1]
            if (
                resolved.startswith("app.repositories.sqlite.")
                and constructor in SQLITE_PERSISTENCE_CONSTRUCTORS
            ):
                offenders.append(f"{relative}:{node.lineno}:{constructor}")

        def scan_comprehension(node, aliases, method_inherited):
            # Python evaluates each iterable before binding that generator's
            # target; its ifs and the final elt/key/value see the bound target.
            first, *remaining = node.generators
            scan_expression(first.iter, aliases, method_inherited)
            local_aliases = dict(aliases)
            unbind_target(first.target, local_aliases)
            for condition in first.ifs:
                scan_expression(condition, local_aliases, method_inherited)
            for generator in remaining:
                scan_expression(generator.iter, local_aliases, method_inherited)
                unbind_target(generator.target, local_aliases)
                for condition in generator.ifs:
                    scan_expression(condition, local_aliases, method_inherited)
            if isinstance(node, ast.DictComp):
                scan_expression(node.key, local_aliases, method_inherited)
                scan_expression(node.value, local_aliases, method_inherited)
            else:
                scan_expression(node.elt, local_aliases, method_inherited)

        def scan_expression(node, aliases, method_inherited=None):
            if node is None:
                return
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                scan_comprehension(node, aliases, method_inherited)
                return
            if isinstance(node, ast.NamedExpr):
                scan_expression(node.value, aliases, method_inherited)
                bind_target(node.target, node.value, aliases)
                return
            if isinstance(node, ast.Lambda):
                for default in (*node.args.defaults, *node.args.kw_defaults):
                    scan_expression(default, aliases, method_inherited)
                body_aliases = dict(aliases)
                for name in argument_names(node.args):
                    body_aliases.pop(name, None)
                scan_expression(node.body, body_aliases)
                return
            if isinstance(node, ast.Call):
                scan_expression(node.func, aliases, method_inherited)
                record_call(node, aliases)
                for argument in node.args:
                    scan_expression(argument, aliases, method_inherited)
                for keyword in node.keywords:
                    scan_expression(keyword.value, aliases, method_inherited)
                return
            for child in ast.iter_child_nodes(node):
                scan_expression(child, aliases, method_inherited)

        def merge_aliases(target, branches):
            if not branches:
                return
            # A post-branch alias is trusted only when every possible branch
            # leaves the exact same canonical constructor identity.
            common = {
                name: value
                for name, value in branches[0].items()
                if all(branch.get(name) == value for branch in branches[1:])
            }
            target.clear()
            target.update(common)

        def scan_function_definition(node, aliases, method_inherited):
            for decorator in node.decorator_list:
                scan_expression(decorator, aliases, method_inherited)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                scan_expression(default, aliases, method_inherited)
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                scan_expression(argument.annotation, aliases, method_inherited)
            if node.args.vararg:
                scan_expression(node.args.vararg.annotation, aliases, method_inherited)
            if node.args.kwarg:
                scan_expression(node.args.kwarg.annotation, aliases, method_inherited)
            scan_expression(node.returns, aliases, method_inherited)
            body_aliases = dict(
                method_inherited if method_inherited is not None else aliases
            )
            for name in argument_names(node.args):
                body_aliases.pop(name, None)
            scan_block(node.body, body_aliases)
            aliases.pop(node.name, None)

        def scan_statement(node, aliases, method_inherited=None):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan_function_definition(node, aliases, method_inherited)
            elif isinstance(node, ast.ClassDef):
                for decorator in node.decorator_list:
                    scan_expression(decorator, aliases, method_inherited)
                for base in node.bases:
                    scan_expression(base, aliases, method_inherited)
                for keyword in node.keywords:
                    scan_expression(keyword.value, aliases, method_inherited)
                outer_aliases = dict(
                    method_inherited if method_inherited is not None else aliases
                )
                # Method bodies close over the surrounding lexical scope;
                # bare names never inherit the class body's local namespace.
                scan_block(node.body, dict(aliases), outer_aliases)
                aliases.pop(node.name, None)
            elif isinstance(node, ast.Import):
                for imported in node.names:
                    local = imported.asname or imported.name.split(".", 1)[0]
                    aliases[local] = imported.name if imported.asname else local
            elif isinstance(node, ast.ImportFrom):
                module = resolved_import_module(node)
                for imported in node.names:
                    if imported.name == "*":
                        if (
                            module == "app.repositories.sqlite"
                            or module.startswith("app.repositories.sqlite.")
                        ):
                            offenders.append(f"{relative}:{node.lineno}:*")
                        continue
                    local = imported.asname or imported.name
                    aliases[local] = ".".join(
                        part for part in (module, imported.name) if part
                    )
            elif isinstance(node, ast.Assign):
                scan_expression(node.value, aliases, method_inherited)
                bindings = [
                    binding_values(target, node.value, aliases)
                    for target in node.targets
                ]
                for target_bindings in bindings:
                    for name, value in target_bindings:
                        bind_name(name, value, aliases)
            elif isinstance(node, ast.AnnAssign):
                scan_expression(node.annotation, aliases, method_inherited)
                if node.value is not None:
                    scan_expression(node.value, aliases, method_inherited)
                    bind_target(node.target, node.value, aliases)
                else:
                    unbind_target(node.target, aliases)
            elif isinstance(node, ast.AugAssign):
                scan_expression(node.target, aliases, method_inherited)
                scan_expression(node.value, aliases, method_inherited)
                unbind_target(node.target, aliases)
            elif isinstance(node, ast.Expr):
                scan_expression(node.value, aliases, method_inherited)
            elif isinstance(node, ast.If):
                scan_expression(node.test, aliases, method_inherited)
                original = dict(aliases)
                body_aliases = dict(original)
                scan_block(node.body, body_aliases, method_inherited)
                else_aliases = dict(original)
                scan_block(node.orelse, else_aliases, method_inherited)
                merge_aliases(aliases, [body_aliases, else_aliases])
            elif isinstance(node, ast.Match):
                scan_expression(node.subject, aliases, method_inherited)
                captures: set[str] = set()
                for case in node.cases:
                    case_aliases = dict(aliases)
                    case_captures = pattern_captures(case.pattern)
                    captures.update(case_captures)
                    for name in case_captures:
                        case_aliases.pop(name, None)
                    scan_expression(case.guard, case_aliases, method_inherited)
                    scan_block(case.body, case_aliases, method_inherited)
                for name in captures:
                    aliases.pop(name, None)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                scan_expression(node.iter, aliases, method_inherited)
                body_aliases = dict(aliases)
                unbind_target(node.target, body_aliases)
                scan_block(node.body, body_aliases, method_inherited)
                scan_block(node.orelse, dict(aliases), method_inherited)
                unbind_target(node.target, aliases)
            elif isinstance(node, ast.While):
                scan_expression(node.test, aliases, method_inherited)
                scan_block(node.body, dict(aliases), method_inherited)
                scan_block(node.orelse, dict(aliases), method_inherited)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                body_aliases = dict(aliases)
                for item in node.items:
                    scan_expression(item.context_expr, body_aliases, method_inherited)
                    if item.optional_vars:
                        unbind_target(item.optional_vars, body_aliases)
                scan_block(node.body, body_aliases, method_inherited)
            elif isinstance(node, ast.Try):
                scan_block(node.body, dict(aliases), method_inherited)
                for handler in node.handlers:
                    handler_aliases = dict(aliases)
                    scan_expression(handler.type, handler_aliases, method_inherited)
                    if handler.name:
                        handler_aliases.pop(handler.name, None)
                    scan_block(handler.body, handler_aliases, method_inherited)
                scan_block(node.orelse, dict(aliases), method_inherited)
                scan_block(node.finalbody, dict(aliases), method_inherited)
            elif isinstance(node, (ast.Return, ast.Raise, ast.Assert)):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.expr):
                        scan_expression(child, aliases, method_inherited)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    unbind_target(target, aliases)
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.expr):
                        scan_expression(child, aliases, method_inherited)

        def scan_block(statements, aliases, method_inherited=None):
            for statement in statements:
                scan_statement(statement, aliases, method_inherited)

        scan_block(tree.body, {})
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


@pytest.mark.parametrize(
    ("scope", "body", "line"),
    [
        (
            "module",
            "from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "DB(settings, root)\n"
            "DB = Fake\n",
            2,
        ),
        (
            "module",
            "DB = Fake\n"
            "from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "DB(settings, root)\n",
            3,
        ),
        (
            "function",
            "def build():\n"
            "    from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "    DB(settings, root)\n"
            "    DB = Fake\n",
            3,
        ),
        (
            "function",
            "def build():\n"
            "    DB = Fake\n"
            "    from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "    DB(settings, root)\n",
            4,
        ),
        (
            "class",
            "class Example:\n"
            "    from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "    DB(settings, root)\n"
            "    DB = Fake\n",
            3,
        ),
        (
            "class",
            "class Example:\n"
            "    DB = Fake\n"
            "    from app.repositories.sqlite.database import SqliteDatabase as DB\n"
            "    DB(settings, root)\n",
            4,
        ),
    ],
)
def test_sqlite_construction_guard_respects_statement_order(scope, body, line):
    assert _sqlite_persistence_construction_sites_from_sources(
        {f"app/{scope}_order.py": body}
    ) == [f"app/{scope}_order.py:{line}:SqliteDatabase"]


def test_sqlite_construction_guard_respects_comprehension_execution_order():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "outer = [item for SqliteDatabase in SqliteDatabase()]\n"
        "shadowed = [SqliteDatabase() for SqliteDatabase in values]\n"
        "ordered = [\n"
        "    SqliteDatabase()\n"
        "    for item in values\n"
        "    if SqliteDatabase()\n"
        "    for SqliteDatabase in SqliteDatabase()\n"
        "    if SqliteDatabase()\n"
        "]\n"
        "after = SqliteDatabase()\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/comprehension_order.py": source}
    ) == [
        "app/comprehension_order.py:2:SqliteDatabase",
        "app/comprehension_order.py:7:SqliteDatabase",
        "app/comprehension_order.py:8:SqliteDatabase",
        "app/comprehension_order.py:11:SqliteDatabase",
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
def test_sqlite_construction_guard_keeps_comprehension_targets_local(expression):
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        f"result = {expression}\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/comprehension_shadow.py": source}
    ) == []


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
def test_sqlite_construction_guard_honors_match_pattern_captures(pattern):
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "match value:\n"
        f"    case {pattern}:\n"
        "        SqliteDatabase(settings, root)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/match_capture.py": source}
    ) == []


def test_sqlite_construction_guard_does_not_treat_match_class_as_a_capture():
    source = (
        "from app.repositories.sqlite.database import SqliteDatabase\n"
        "match value:\n"
        "    case SqliteDatabase():\n"
        "        SqliteDatabase(settings, root)\n"
    )
    assert _sqlite_persistence_construction_sites_from_sources(
        {"app/match_class.py": source}
    ) == ["app/match_class.py:4:SqliteDatabase"]


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
