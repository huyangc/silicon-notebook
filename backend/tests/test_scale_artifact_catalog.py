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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients

FIXTURE_SCALE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "repository_v9" / "storage" / "kg_index" / "nb-fixture"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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


def test_open_ann_singleflights_concurrent_cold_load(repo, monkeypatch):
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(6)

    class FakeIndex:
        def __init__(self, *, space, dim):
            assert (space, dim) == ("cosine", 16)

        def load_index(self, path, *, max_elements):
            nonlocal calls
            assert (path, max_elements) == ("kg-ann.bin", 3)
            with calls_lock:
                calls += 1
            # Keep the first load open long enough for all callers to race on
            # the empty handle. Without the per-index lock, each loads a copy.
            time.sleep(0.05)

    monkeypatch.setitem(sys.modules, "hnswlib", SimpleNamespace(Index=FakeIndex))
    index = SimpleNamespace(
        manifest={"dim": 16},
        ann_path="kg-ann.bin",
        ann_labels=["a", "b", "c"],
        ann_handle=None,
    )
    catalog = repo._runtime.scale_catalog

    def open_concurrently():
        start.wait()
        return catalog.open_ann(index, "kg")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(open_concurrently) for _ in range(5)]
        start.wait()
        handles = [future.result() for future in futures]

    assert calls == 1
    assert all(handle is handles[0] for handle in handles)


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


def test_embedding_matrix_reads_keyset_pages_not_one_snapshot(repo, monkeypatch):
    """Guard: the whole-notebook load (object_ids=None) must read bounded keyset
    pages — each an INDEPENDENT `rowid > ?` statement — never one whole-table
    read and never row-by-row. A single statement (.fetchall() the whole table,
    or one long-lived streaming cursor) pins ONE WAL read snapshot for the
    entire multi-million-row build, blocking checkpoints while concurrent writes
    append frames → unbounded `-wal` growth (the P1 codex flagged on PR#340
    round 2). Keyset pages exhaust each statement before the next begins, so the
    snapshot is released between pages. Row-by-row iteration additionally hits
    the instrumented _DiagnosticCursor.__next__ (round 1's per-row sql_scope:
    the global diagnostics lock twice + a writer wake per embedding). Reverting
    to any of those shapes must fail here."""
    import app.repositories.sqlite.index_projection_store as ips
    from app.repositories.sqlite.database import _DiagnosticCursor

    nb = _seeded_notebook(repo)                       # 2 KG objects → 2 vectors
    projections = repo._runtime.index_projections
    object_ids = repo._gather_kg_graph(nb.id)[3]
    assert len(object_ids) >= 2                        # need >1 row to span pages

    monkeypatch.setattr(ips, "_MATRIX_FETCH_BATCH", 1)   # force multi-page reads

    counts = {"paged": 0, "next": 0}
    real_execute = _DiagnosticCursor.execute
    real_next = _DiagnosticCursor.__next__

    def spy_execute(self, sql, parameters=(), /):
        # count only the paged vector reads (rowid keyset), not the COUNT(*)
        if "knowledge_embeddings" in sql and "rowid >" in sql:
            counts["paged"] += 1
        return real_execute(self, sql, parameters)

    monkeypatch.setattr(_DiagnosticCursor, "execute", spy_execute)
    monkeypatch.setattr(_DiagnosticCursor, "__next__",
                        lambda self: (counts.__setitem__("next", counts["next"] + 1)
                                      or real_next(self)))

    ids, matrix = projections.embedding_matrix(
        nb.id, "knowledge_embeddings", "object_id")

    # completeness: correct + fully shaped regardless of page boundaries
    assert set(ids) == set(object_ids)
    assert matrix.shape == (len(object_ids), 16)
    # shape of the read: ≥2 independent keyset pages, never per-row __next__.
    # (whole-table fetchall or a single streaming cursor issue no `rowid >`
    # statement, so both collapse counts["paged"] to 0 and fail here.)
    assert counts["paged"] >= 2              # bounded rowid>? pages; snapshot freed between
    assert counts["next"] == 0               # never the instrumented per-row path


def test_embedding_matrix_dedups_concurrent_replace(repo, monkeypatch):
    """Guard (codex PR#340 round 3): keyset paginates by rowid, but the embedding
    writers use INSERT OR REPLACE — a refresh deletes+reinserts the row at a
    LARGER rowid. If a row is refreshed after the scan passed its old rowid it
    re-surfaces in a later page; without de-duplication build_matrix would carry
    both the stale and the current vector under one id (silent index corruption
    behind an up-to-date manifest). The load must yield each id exactly once.
    Simulate the race: right after the first page is read, REPLACE the object it
    returned — moving its rowid past the rest, exactly what a concurrent re-embed
    does — then assert the id still appears once."""
    from app.repositories.sqlite.database import _DiagnosticCursor
    import app.repositories.sqlite.index_projection_store as ips

    nb = _seeded_notebook(repo)                       # objects a, b → 2 vectors
    projections = repo._runtime.index_projections
    object_ids = repo._gather_kg_graph(nb.id)[3]
    assert len(object_ids) >= 2

    monkeypatch.setattr(ips, "_MATRIX_FETCH_BATCH", 1)   # one row per page

    real_fetchall = _DiagnosticCursor.fetchall
    state = {"fired": False}

    def fetchall_then_replace(self):
        rows = real_fetchall(self)
        # after the FIRST paged vector read, refresh that row on a separate
        # (concurrent) write connection so INSERT OR REPLACE bumps its rowid
        if (not state["fired"] and len(rows) == 1
                and set(rows[0].keys()) >= {"rid", "vid", "vector"}):
            state["fired"] = True
            with repo._write() as w:
                w.execute(
                    "INSERT OR REPLACE INTO knowledge_embeddings "
                    "(object_id, notebook_id, vector, created_at) "
                    "VALUES (?,?,?,?)",
                    (rows[0]["vid"], nb.id, rows[0]["vector"],
                     "2026-01-02T00:00:00"))
        return rows

    monkeypatch.setattr(_DiagnosticCursor, "fetchall", fetchall_then_replace)

    ids, matrix = projections.embedding_matrix(
        nb.id, "knowledge_embeddings", "object_id")

    assert state["fired"]                          # the rowid-move race actually ran
    assert len(ids) == len(set(ids))               # no duplicate id survived
    assert set(ids) == set(object_ids)             # complete, each id exactly once
    assert matrix.shape[0] == len(ids)             # matrix rows align 1:1 with ids
