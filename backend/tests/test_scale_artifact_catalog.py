"""Task 18 — runtime-owned scale artifact read adapters.

The scale/viz artifact READ side moves behind the runtime: IndexProjectionStore
owns the version/count/graph/vector DB snapshots, ScaleArtifactStore owns the
on-disk manifest/artifact layout, and ScaleArtifactCatalog applies the
exact/allow_stale version semantics plus the lazy ANN open. The catalog holds
NO builder — loading an existing artifact can never schedule a rebuild (the
base-offline-ANN / active-brute cost-separation invariant), and the facade's
`_scale_index` / `_open_scale_ann` delegates route through these SAME objects.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository

FIXTURE_SCALE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "repository_v9" / "storage" / "kg_index" / "nb-fixture"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


@dataclass(frozen=True)
class CopiedScaleFixture:
    notebook_id: str
    manifest_path: Path


@pytest.fixture
def copied_scale_fixture(repo):
    """An EXISTING on-disk scale artifact — the frozen v9 fixture files copied
    under a fresh notebook id. Its manifest.version can never match this
    database's version, which is exactly the allow_stale serving case: the
    artifact must be served as-is, never rebuilt because it was read."""
    nb = repo.create_notebook(NotebookCreate(name="carrier"))
    dst = Path(repo.settings.storage_dir) / "kg_index" / nb.id
    shutil.copytree(FIXTURE_SCALE_DIR, dst)
    return CopiedScaleFixture(notebook_id=nb.id, manifest_path=dst / "manifest.json")


def _seeded_notebook(repo, name="idx"):
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "bandgap reference", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b",
         "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_existing_scale_artifact_loads_without_rebuild(repo, copied_scale_fixture):
    before = copied_scale_fixture.manifest_path.stat().st_mtime_ns
    catalog = repo._runtime.scale_catalog
    assert not hasattr(catalog, "builder")
    loaded = repo._scale_index(
        copied_scale_fixture.notebook_id, allow_stale=True
    )
    assert loaded is not None
    assert copied_scale_fixture.manifest_path.stat().st_mtime_ns == before


def test_runtime_owns_the_interim_adapters(repo):
    """The runtime exposes the Task-18 interim objects; Task 20 composes these
    SAME objects into scale_artifacts without recreating them."""
    runtime = repo._runtime
    assert runtime.index_projections is not None
    assert runtime.scale_artifact_store is not None
    assert runtime.scale_catalog is not None
    assert runtime.scale_catalog.artifacts is runtime.scale_artifact_store


def test_exact_load_matches_version_and_stale_reuses_disk_identity(repo):
    nb = _seeded_notebook(repo)
    repo.build_scale_index(nb.id)
    exact = repo._scale_index(nb.id)
    assert exact is not None                      # manifest.version == current
    # a KG write drifts the DB version away from the on-disk manifest
    repo.store_kg(nb.id, None, [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "PTAT current", "section_path": ""}, "evidence": []},
    ], [])
    assert repo._scale_index(nb.id) is None       # exact caller unchanged
    stale_a = repo._scale_index(nb.id, allow_stale=True)
    stale_b = repo._scale_index(nb.id, allow_stale=True)
    assert stale_a is not None and stale_a is stale_b   # disk-identity reuse


def test_version_facts_compose_the_frozen_version_list(repo):
    """version_signal + version_facts carve today's probe/cold reads apart
    without changing the version list format (facts + settings tail)."""
    nb = repo.create_notebook(NotebookCreate(name="v"))
    projections = repo._runtime.index_projections
    seq, cseq, tail = projections.version_signal(nb.id)
    assert isinstance(seq, int) and isinstance(cseq, int)
    assert repo._scale_index_version(nb.id) == projections.version_facts(nb.id) + list(tail)


def test_open_ann_memoizes_handle_and_fails_open(repo):
    nb = _seeded_notebook(repo)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    catalog = repo._runtime.scale_catalog
    handle = catalog.open_ann(idx, "kg")
    assert handle is not None
    assert catalog.open_ann(idx, "kg") is handle          # memoized on the instance
    assert catalog.open_ann(idx, "chunk") is None         # no chunk artifact → fail-open


def test_graph_rows_matches_the_facade_gather(repo):
    nb = _seeded_notebook(repo)
    projections = repo._runtime.index_projections
    rows = projections.graph_rows(nb.id, None)
    assert (
        rows.node_ids, rows.edges, rows.chunk_ids,
        rows.kg_node_ids, rows.membership_counts,
    ) == repo._gather_kg_graph(nb.id)
    empty = projections.graph_rows(nb.id, [])
    assert (empty.node_ids, empty.edges, empty.chunk_ids,
            empty.kg_node_ids, empty.membership_counts) == ([], [], [], [], {})


def test_embedding_matrix_unscoped_scoped_and_empty(repo):
    nb = _seeded_notebook(repo)
    projections = repo._runtime.index_projections
    object_ids = repo._gather_kg_graph(nb.id)[3]
    ids, matrix = projections.embedding_matrix(
        nb.id, "knowledge_embeddings", "object_id")
    assert set(ids) == set(object_ids)
    assert matrix.shape == (len(object_ids), 16)
    scoped_ids, scoped = projections.embedding_matrix(
        nb.id, "knowledge_embeddings", "object_id", object_ids=object_ids[:1])
    assert scoped_ids == object_ids[:1]
    assert scoped.shape == (1, 16)
    assert projections.embedding_matrix(
        nb.id, "knowledge_embeddings", "object_id", object_ids=[]) == ([], [])
