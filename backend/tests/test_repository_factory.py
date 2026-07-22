from __future__ import annotations

import ast
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


def test_sqlite_bundle_factory_is_the_only_persistence_construction_root():
    app_root = Path(__file__).resolve().parents[1] / "app"
    bundle_path = app_root / "repositories" / "sqlite" / "bundle.py"
    offenders: list[str] = []

    for path in app_root.rglob("*.py"):
        if path == bundle_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in SQLITE_PERSISTENCE_CONSTRUCTORS
            ):
                offenders.append(
                    f"{path.relative_to(app_root.parent)}:{node.lineno}:{node.func.id}"
                )

    assert offenders == []


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
