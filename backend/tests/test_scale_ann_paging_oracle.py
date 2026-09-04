"""Oracle: paged ANN feed ≡ the whole-matrix build (batch-3 W4 T-W4-3.3).

The build used to load one whole-notebook matrix per ANN leg and hand it to a
single ``add_items``; it now inserts bounded pages and, for the KG synonym
KNN, re-reads the query set in a SECOND pass because the matrix is gone by
then. Both changes are only admissible if the artifacts are the same ones, so
the reference implementation below is the PRE-CHANGE code copied verbatim and
run against the same database.

``num_threads=1`` throughout: hnswlib's multi-threaded insertion visits
elements in a nondeterministic order, so two runs of the SAME code can build
different graphs. Pinned to one thread, "same rows in the same order" means
"same index", and any difference the test sees is a real one.
"""
from __future__ import annotations

import json

import hnswlib
import numpy as np
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


class _SingleThreadIndex(hnswlib.Index):
    """hnswlib.Index pinned to one thread for insertion AND query."""

    def add_items(self, data, ids=None, num_threads=-1, replace_deleted=False):
        return super().add_items(data, ids, 1, replace_deleted)

    def knn_query(self, data, k=1, num_threads=-1, filter=None):
        return super().knn_query(data, k, 1, filter)


@pytest.fixture(autouse=True)
def _single_threaded_hnsw(monkeypatch):
    monkeypatch.setattr(hnswlib, "Index", _SingleThreadIndex)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _seed(repo, *, objects: int = 40, chunks_per_object: int = 2):
    """A small library with enough rows that a page size of 7 forces several
    pages on every leg (objects, chunks, relations)."""
    notebook = repo.create_notebook(NotebookCreate(name="paging"))
    now = "2026-09-04T00:00:00"
    nodes = []
    edges = []
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("src-1", notebook.id, "paging corpus", "md", "ready", now, now),
        )
        for index in range(objects):
            element_ids = []
            for offset in range(chunks_per_object):
                element_id = f"el-{index}-{offset}"
                element_ids.append(element_id)
                db.execute(
                    "INSERT INTO chunks (id,notebook_id,source_id,text,"
                    "section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        f"chunk-{index}-{offset}", notebook.id, "src-1",
                        f"Mixture of Experts variant {index} note {offset}.",
                        "S", json.dumps([element_id]), now,
                    ),
                )
            nodes.append({
                "local_id": f"n{index}",
                "object_type": "concept",
                "payload": {"name": f"expert router v{index}", "section_path": ""},
                "evidence": [{"element_id": eid} for eid in element_ids],
            })
            if index:
                edges.append({
                    "source_local_id": f"n{index}",
                    "target_local_id": f"n{index - 1}",
                    "edge_type": "depends_on",
                    "evidence": [],
                })
    repo.store_kg(notebook.id, "src-1", nodes, edges)
    repo.rebuild_unified_kg(notebook.id)
    return notebook


# ─────────────────────────────────── the pre-change implementation, verbatim ──

def _reference_leg(builder, notebook_id: str, table: str, id_column: str):
    """``build``'s old per-leg shape: one whole-notebook matrix, one
    ``add_items`` over ``np.arange(n)``. Copied from
    ``scale_index_builder.build`` and its ``_build_ann`` helper as they stood
    before batch-3 W4 T-W4-3.3 (that helper has since been deleted — the paged
    feed left it with no call site — so this copy is now its only record)."""
    ids_raw, matrix_raw = builder.projections.embedding_matrix(
        notebook_id, table, id_column
    )
    labels = list(ids_raw) if ids_raw else []
    vectors = (
        np.asarray(matrix_raw, dtype=np.float32)
        if labels and matrix_raw is not None
        else None
    )
    if vectors is None or vectors.shape[0] == 0:
        return labels, None, vectors
    index = hnswlib.Index(space="cosine", dim=int(vectors.shape[1]))
    index.init_index(
        max_elements=vectors.shape[0],
        ef_construction=builder.settings.hnsw_ef_construction,
        M=16,
        random_seed=42,
    )
    index.add_items(vectors, np.arange(vectors.shape[0]))
    return labels, index, vectors


def _reference_synonyms(builder, labels, vectors, index):
    """The old synonym call: the whole matrix as the query set, reusing the
    prebuilt index (``emb_synonym_edges`` itself is unchanged)."""
    from app.domain.kg.ppr_pairs import emb_synonym_edges

    return emb_synonym_edges(
        labels,
        vectors,
        builder.settings.ppr_emb_synonym_threshold,
        builder.settings.ppr_emb_synonym_topk,
        builder.settings.ppr_emb_synonym_max_entities,
        prebuilt_index=index,
        ef_construction=builder.settings.hnsw_ef_construction,
    )


def _top_k_sets(index, labels, vectors, k):
    found, _ = index.knn_query(vectors, k=min(k, len(labels)))
    return [frozenset(labels[int(label)] for label in row) for row in found]


# ──────────────────────────────────────────────────────────────── the oracle ──

@pytest.mark.parametrize("page_rows", [1, 7, 10_000])
def test_paged_ann_reproduces_the_whole_matrix_index(repo, monkeypatch, page_rows):
    notebook = _seed(repo)
    builder = repo._runtime.scale_builder

    real_pages = builder.projections.embedding_pages
    monkeypatch.setattr(
        builder.projections,
        "embedding_pages",
        lambda nb, table, id_column, *_a, **_k: real_pages(
            nb, table, id_column, page_rows
        ),
    )

    for table, id_column in (
        ("knowledge_embeddings", "object_id"),
        ("chunk_embeddings", "chunk_id"),
        ("relation_embeddings", "relation_id"),
    ):
        want_labels, want_index, want_vectors = _reference_leg(
            builder, notebook.id, table, id_column
        )
        got_labels, got_index, _load_ms, _add_ms = builder._paged_ann(
            notebook.id, table, id_column
        )

        assert got_labels == want_labels, table
        assert (got_index is None) == (want_index is None), table
        if want_index is None:
            continue
        assert want_index.get_current_count() > 0, f"{table} seeded no vectors"
        assert got_index.get_current_count() == want_index.get_current_count()
        assert got_index.dim == want_index.dim
        # top-k SET equality, row by row, over every seeded vector.
        k = 5
        assert _top_k_sets(got_index, got_labels, want_vectors, k) == (
            _top_k_sets(want_index, want_labels, want_vectors, k)
        ), table


@pytest.mark.parametrize("page_rows", [1, 7, 10_000])
def test_second_pass_synonym_edges_match_the_one_matrix_call(
    repo, monkeypatch, page_rows
):
    """The paged query walks the index's OWN stored vectors label-page by
    label-page. A wrong page/label offset is invisible in a one-page run and
    catastrophic in a many-page one — hence page_rows=1."""
    from app.domain.kg.ppr_pairs import emb_synonym_edges_paged

    notebook = _seed(repo)
    builder = repo._runtime.scale_builder
    monkeypatch.setattr(builder.settings, "ppr_emb_synonym_threshold", 0.0)

    labels, index, vectors = _reference_leg(
        builder, notebook.id, "knowledge_embeddings", "object_id"
    )
    assert index is not None and len(labels) > 1
    want = _reference_synonyms(builder, labels, vectors, index)
    assert want, "the seeded corpus must produce synonym edges at all"

    got = emb_synonym_edges_paged(
        labels,
        index,
        builder.settings.ppr_emb_synonym_threshold,
        builder.settings.ppr_emb_synonym_topk,
        page_rows=page_rows,
    )

    # Pairs AND their row-major first-seen order must match exactly. The
    # similarities carry a ~1e-7 wobble: hnswlib's cosine space re-normalizes
    # stored vectors in float32, so ``get_items`` hands back rows that differ
    # from the pre-normalized originals by one rounding step — semantically
    # the same edge set, compared under a tolerance instead of bit equality.
    assert [(a, b) for a, b, _ in got] == [(a, b) for a, b, _ in want]
    assert all(
        abs(g - w) < 1e-5
        for (_, _, g), (_, _, w) in zip(got, want)
    ), "similarities drifted beyond float32 re-normalization rounding"


def test_synonym_edges_ignore_database_state_after_the_index_is_built(repo):
    """codex #676 R1 (P2) closure pin: the second pass must NOT read the
    database — an embedding updated (or deleted) between the passes would
    otherwise query a NEW vector against the OLD one stored in the index,
    minting edges no consistent snapshot supports. The query set comes from
    ``get_items`` on the index itself, so wiping every embedding row after
    the build must change nothing. Mutation anchor: reintroduce a DB read
    into ``emb_synonym_edges_paged`` and this goes red."""
    from app.domain.kg.ppr_pairs import emb_synonym_edges_paged

    notebook = _seed(repo, objects=6, chunks_per_object=1)
    builder = repo._runtime.scale_builder
    labels, index, _vectors = _reference_leg(
        builder, notebook.id, "knowledge_embeddings", "object_id"
    )
    assert index is not None and len(labels) > 2

    before = emb_synonym_edges_paged(
        labels, index, 0.0, builder.settings.ppr_emb_synonym_topk,
        page_rows=2,
    )
    assert before, "the seeded corpus must produce synonym edges at all"

    with repo._write() as db:
        db.execute(
            "DELETE FROM knowledge_embeddings WHERE notebook_id=?",
            (notebook.id,),
        )

    after = emb_synonym_edges_paged(
        labels, index, 0.0, builder.settings.ppr_emb_synonym_topk,
        page_rows=2,
    )
    assert after == before


def test_an_hnswlib_failure_fails_open_and_reports_only_the_exception_class(repo):
    """The other half of the same fix: hnswlib failures still cost only the
    soft synonym edges, and they are reported through ``on_hnsw_error`` so the
    builder can emit a structured event carrying the CLASS NAME and nothing
    else (no message, no ids)."""
    from app.domain.kg.ppr_pairs import emb_synonym_edges_paged

    notebook = _seed(repo, objects=6, chunks_per_object=1)
    builder = repo._runtime.scale_builder
    labels, index, _vectors = _reference_leg(
        builder, notebook.id, "knowledge_embeddings", "object_id"
    )
    assert index is not None

    class _HnswFailure(RuntimeError):
        pass

    class _BrokenIndex:
        def set_ef(self, _ef):
            return None

        def get_items(self, labels):
            return np.zeros((len(labels), 4), dtype=np.float32)

        def knn_query(self, _data, k=1):
            raise _HnswFailure("index corrupted: /var/lib/secret/path")

    seen: list = []
    edges = emb_synonym_edges_paged(
        labels,
        _BrokenIndex(),
        0.0,
        builder.settings.ppr_emb_synonym_topk,
        on_hnsw_error=lambda exc: seen.append(type(exc).__name__),
    )

    assert edges == []
    assert seen == ["_HnswFailure"]


def test_capacity_grows_geometrically_when_the_count_under_estimates(repo, monkeypatch):
    """Double-review fix D. ``hnswlib.resize_index`` reallocates the element
    store and copies everything already inserted, so growing by one page per
    page makes the overflow tail quadratic. Capacity must at least DOUBLE.
    Mutation anchor: restore ``capacity = offset + len(page_ids)`` and the
    resize count goes up with the number of overflowing pages."""
    notebook = _seed(repo, objects=12, chunks_per_object=1)
    builder = repo._runtime.scale_builder
    # A COUNT that under-estimates by a lot forces every page past the first
    # into the resize branch.
    monkeypatch.setattr(
        builder.projections, "embedding_row_count",
        lambda _notebook_id, _table: 1,
    )
    real_pages = builder.projections.embedding_pages
    monkeypatch.setattr(
        builder.projections, "embedding_pages",
        lambda notebook_id, table, id_column, *args, **kw: real_pages(
            notebook_id, table, id_column, 1
        ),
    )
    sizes: list[int] = []
    real_index = hnswlib.Index

    class _ResizeSpy(real_index):
        def resize_index(self, size):
            sizes.append(int(size))
            return super().resize_index(size)

    monkeypatch.setattr(hnswlib, "Index", _ResizeSpy)

    labels, index, _load_ms, _add_ms = builder._paged_ann(
        notebook.id, "knowledge_embeddings", "object_id"
    )

    assert index is not None and len(labels) == 12
    # 12 one-row pages from a capacity of 1: 1 → 2 → 4 → 8 → 16 is FOUR
    # reallocations; a page-at-a-time policy would need eleven.
    assert sizes == [2, 4, 8, 16]
