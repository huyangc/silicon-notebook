"""Task 15 — knowledge lifecycle / unified-KG orchestration extraction.

The facade's KG lifecycle surface (delete/build/rebuild, store/relink,
cluster writes, incremental fusion, unified reads, streamed rebuild and the
canonical/mention/community derived layers) delegates to ONE
KnowledgeLifecycleService instance owned by the runtime; the minimal
port-based KnowledgeGovernanceService owns resolve_notebook_conflicts so
build can call it without a facade callback. Gate-4's temporary KG source
hooks are replaced by direct service/coordinator dependencies, and the two
new service modules never import the facade.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

SERVICE_MODULES = (
    "app/services/knowledge_lifecycle.py",
    "app/services/knowledge_governance.py",
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'lc.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_facade_lifecycle_delegates_to_same_service(repo, monkeypatch):
    sentinel = {"dirty": False}
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle,
        "unified_kg_status",
        lambda notebook_id: sentinel,
    )
    assert repo.unified_kg_status("nb-x") is sentinel


def test_facade_lifecycle_methods_delegate_to_the_runtime_instance(repo, monkeypatch):
    lifecycle = repo._runtime.knowledge_lifecycle
    sentinel = object()
    # Index-mutating facade calls now run the notebook pipeline write gate
    # before their lifecycle delegate.  This characterization owns only the
    # delegate target, so keep admission orthogonal (the indexing-pipeline API
    # tests exercise the real gate and its 409 semantics).
    monkeypatch.setattr(
        repo, "require_indexing_pipeline_write", lambda _notebook_id: None
    )
    for name, call in (
        ("delete_notebook_kg", lambda: repo.delete_notebook_kg("nb")),
        ("build_notebook_kg", lambda: repo.build_notebook_kg("nb")),
        ("rebuild_notebook_kg", lambda: repo.rebuild_notebook_kg("nb")),
        ("store_kg", lambda: repo.store_kg("nb", None, [], [])),
        ("relink_notebook_kg", lambda: repo.relink_notebook_kg("nb")),
        ("incremental_fuse_source", lambda: repo.incremental_fuse_source("nb", "s")),
        ("write_clusters", lambda: repo.write_clusters("nb", [])),
        ("append_clusters", lambda: repo.append_clusters("nb", [])),
        ("unified_graph", lambda: repo.unified_graph("nb")),
        ("kg_neighbors", lambda: repo.kg_neighbors("nb", "K-x")),
        ("rebuild_unified_kg", lambda: repo.rebuild_unified_kg("nb")),
        ("rebuild_canonical_relations", lambda: repo.rebuild_canonical_relations("nb")),
        ("rebuild_mention_bridge", lambda: repo.rebuild_mention_bridge("nb")),
        ("rebuild_communities", lambda: repo.rebuild_communities("nb")),
        ("list_communities", lambda: repo.list_communities("nb")),
        ("summarize_communities", lambda: repo.summarize_communities("nb")),
        ("get_community_reports", lambda: repo.get_community_reports("nb")),
    ):
        monkeypatch.setattr(lifecycle, name, lambda *a, **k: sentinel)
        assert call() is sentinel, name


def test_resolve_notebook_conflicts_delegates_to_governance_service(repo, monkeypatch):
    sentinel = {"detected": 0}
    monkeypatch.setattr(
        repo._runtime.knowledge_governance,
        "resolve_notebook_conflicts",
        lambda notebook_id: sentinel,
    )
    assert repo.resolve_notebook_conflicts("nb-x") is sentinel


def test_kg_building_set_and_lock_are_lifecycle_owned_shared_objects(repo):
    lifecycle = repo._runtime.knowledge_lifecycle
    # One membership set: catalog reads it (get_notebook backfill), the
    # lifecycle build/rebuild paths mutate it, the facade attribute aliases it.
    assert repo._kg_building is lifecycle.kg_building
    assert lifecycle.kg_building is repo._runtime.catalog.kg_building
    assert repo._kg_building_lock is lifecycle.kg_building_lock


def test_lifecycle_holds_the_facade_cache_objects_by_identity(repo):
    lifecycle = repo._runtime.knowledge_lifecycle
    assert lifecycle.unified_cache is repo._unified_cache
    assert lifecycle.scale_artifacts is repo._runtime.scale_artifacts
    assert lifecycle.scale_artifacts.viz_building is repo._viz_building


def test_source_ingestion_uses_direct_service_dependencies(repo):
    ingestion = repo._runtime.source_ingestion
    assert ingestion.knowledge_lifecycle is repo._runtime.knowledge_lifecycle
    assert ingestion.kg_mutations is repo._runtime.kg_mutations
    # The Gate-4 temporary KG callbacks are gone from the ingestion service.
    for stale in ("store_kg", "incremental_fuse_source", "invalidate_unified_cache"):
        assert not hasattr(ingestion, stale), stale


def test_build_calls_governance_conflict_resolution_without_a_facade_callback(repo):
    lifecycle = repo._runtime.knowledge_lifecycle
    assert lifecycle.governance is repo._runtime.knowledge_governance


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_lifecycle_modules_never_import_the_facade():
    for rel in SERVICE_MODULES:
        modules = _imports(BACKEND / rel)
        assert not any(
            m.startswith("app.services.sqlite_repository") for m in modules
        ), rel
        assert not any(
            m.startswith("app.services.repository_runtime") for m in modules
        ), rel
