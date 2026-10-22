"""In-document navigation's retrieval ports on a real store.

``ChunkStorePort.source_outline_rows`` and ``RetrievalPort.section_outline`` /
``section_chunk_similarities`` / ``section_chunks``
(``app.services.section_retrieval``): the bounded outline scan, ceiling first
(out of ceiling reads exactly like a missing source), the chunk-id ownership
check, navigation pages, the relevance ruler, the ``section_read`` marker, the
vector paths (warm notebook matrix borrowed, otherwise only the outline's rows)
and the run-local memos.

The ``check_*`` functions take ``(repo, seed)`` and are shared with the
PostgreSQL twin ``tests/postgres/test_section_outline_ports_pg.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pytest

from app.core.config import Settings
from app.domain.vector_index import encode_vector
from app.services.embedding import FakeEmbedder
from app.services.retrieval import (
    RetrievedChunk,
    _fuse,
    _tokens,
    keyword_basis,
    prefer_stronger_chunk_candidate,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context
from tests.model_testkit import bind_all_embedding_clients

T0 = datetime(2026, 10, 22, tzinfo=timezone.utc)

# (chunk id, section path, text, navigation, vector)
REPORT = (
    ("c-toc", "Contents", "5.7 Loss ..... 12 5.7.3 Package ..... 14", True,
     (0.0, 0.0, 1.0, 0.0)),
    ("c-571", "5.7.2.1 Loss and Crosstalk Mask", "insertion loss mask limits",
     False, (1.0, 0.0, 0.0, 0.0)),
    ("c-572", "5.7.2.1 Loss and Crosstalk Mask", "crosstalk mask table", False,
     (0.6, 0.8, 0.0, 0.0)),
    ("c-573", "", "untitled continuation", False, (0.0, 1.0, 0.0, 0.0)),
    ("c-574", "Report > Notes", "notes about the package", False, None),
    ("c-575", "5.7.2.1 Loss and Crosstalk Mask", "the mask again later", False,
     (0.0, 0.0, 0.0, 1.0)),
)
#: ``REPORT`` with a vector for every chunk: the process cache keeps only a
#: source whose every outline chunk has one.
FULL_REPORT = tuple(
    (cid, path, text, navigation, vector or (0.0, 0.0, 0.0, 1.0))
    for cid, path, text, navigation, vector in REPORT
)
QUERY_VECTORS = {
    "loss question": (1.0, 0.0, 0.0, 0.0),
    "crosstalk question": (0.0, 1.0, 0.0, 0.0),
    "short": (1.0, 0.0, 0.0),
}


class SqliteSeed:
    ph = "?"

    def __init__(self, repo):
        self.repo = repo

    def _write(self):
        return self.repo._write()

    def _ts(self):
        return T0.isoformat()

    def source(self, nb, sid, title="Spec Report", kind="md"):
        p = self.ph
        with self._write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                f"created_at,updated_at) VALUES ({p},{p},{p},{p},'ready',{p},{p})",
                (sid, nb, title, kind, self._ts(), self._ts()),
            )

    def chunk(self, nb, sid, cid, path, text, navigation=False):
        p = self.ph
        with self._write() as db:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                f"element_ids,created_at,navigation) VALUES ({p},{p},{p},{p},{p},"
                f"{self._json()},{p},{p})",
                (cid, nb, sid, text, path, json.dumps([f"el-{cid}"]), self._ts(),
                 1 if navigation else 0),
            )

    def _json(self):
        return "?"

    def vector(self, nb, cid, values):
        p = self.ph
        with self._write() as db:
            db.execute(
                "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) "
                f"VALUES ({p},{p},{p},{p})",
                (cid, nb, encode_vector(values), self._ts()),
            )

    def chunked(self, sid, value="2026-10-22T00:00:00+00:00"):
        """Stamp the source's chunk generation (``sources.chunked_at``)."""
        p = self.ph
        with self._write() as db:
            db.execute(f"UPDATE sources SET chunked_at={p} WHERE id={p}", (value, sid))

    def report(self, nb, sid, rows=REPORT):
        self.source(nb, sid)
        for cid, path, text, navigation, vector in rows:
            self.chunk(nb, sid, cid, path, text, navigation)
            if vector is not None:
                self.vector(nb, cid, vector)


class FixedEmbedder:
    """Query vectors from a table; counts calls (the run memo's evidence)."""

    configured = True

    def __init__(self, table=QUERY_VECTORS):
        self.table = dict(table)
        self.calls: list[str] = []

    def embed_query(self, text):
        self.calls.append(text)
        return list(self.table[text]) if text in self.table else None


class Events:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event):
        self.events.append(dict(event))

    def of(self, kind):
        return [event for event in self.events if event.get("kind") == kind]


def _new_notebook(repo, name):
    from app.models.schemas import NotebookCreate

    return repo.create_notebook(NotebookCreate(name=name)).id


def _wire(repo, monkeypatch):
    candidates = repo.retrieval.candidates
    embedder = FixedEmbedder()
    events = Events()
    monkeypatch.setattr(candidates, "embedder", embedder)
    monkeypatch.setattr(candidates, "event_log", events)
    return candidates, embedder, events


def _scope(visible):
    return {"mode": "include", "source_ids": list(visible),
            "hidden_source_ids": [], "narrowed": True, "owner_id": ""}


# ------------------------------------------------------------ shared checks
def check_outline_rows_document_order(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    rows, truncated = repo.retrieval.section_outline(nb, "src-r")
    assert truncated is False
    assert rows == (
        ("c-toc", "Contents", True),
        ("c-571", "5.7.2.1 Loss and Crosstalk Mask", False),
        ("c-572", "5.7.2.1 Loss and Crosstalk Mask", False),
        ("c-573", "", False),
        ("c-574", "Report > Notes", False),
        ("c-575", "5.7.2.1 Loss and Crosstalk Mask", False),
    )
    with candidates._connect() as db:
        store_rows, store_truncated = candidates.chunks.source_outline_rows(
            db, nb, "src-r", 100)
        assert set(dict(store_rows[0])) == {
            "id", "section_path", "navigation", "source_chunked_at"}
        assert store_truncated is False
        capped, capped_truncated = candidates.chunks.source_outline_rows(
            db, nb, "src-r", 2)
        exact, exact_truncated = candidates.chunks.source_outline_rows(
            db, nb, "src-r", len(REPORT))
        zero = candidates.chunks.source_outline_rows(db, nb, "src-r", 0)
        wrong_owner = candidates.chunks.source_outline_rows(
            db, "nb-absent", "src-r", 100)
    assert [row["id"] for row in capped] == ["c-toc", "c-571"]
    assert capped_truncated is True
    assert len(exact) == len(REPORT) and exact_truncated is False
    assert zero == ([], False)
    assert wrong_owner == ([], False)


def check_outline_scan_bound_and_switches(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    monkeypatch.setattr(candidates.settings, "reasoning_section_outline_max_rows", 3)
    rows, truncated = repo.retrieval.section_outline(nb, "src-r")
    assert [row[0] for row in rows] == ["c-toc", "c-571", "c-572"]
    assert truncated is True
    # The scan bound is also the ownership bound: an id past it is not read.
    assert repo.retrieval.section_chunks(nb, "src-r", ["c-575"], 5, "mask") == []
    monkeypatch.setattr(candidates.settings, "reasoning_section_outline_max_rows", 0)
    assert repo.retrieval.section_outline(nb, "src-r") == ((), False)
    monkeypatch.setattr(candidates.settings, "reasoning_section_outline_max_rows", 4000)
    monkeypatch.setattr(
        candidates.settings, "retrieval_navigation_filter_enabled", False)
    rows, _ = repo.retrieval.section_outline(nb, "src-r")
    assert rows[0] == ("c-toc", "Contents", False)


def check_section_chunks_reads_in_document_order(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    _wire(repo, monkeypatch)
    chunks = repo.retrieval.section_chunks(
        nb, "src-r", ["c-575", "c-571", "c-toc", "c-572"], 12, "loss question")
    assert [chunk.chunk_id for chunk in chunks] == ["c-571", "c-572", "c-575"]
    first = chunks[0]
    assert first.section_read is True and first.exact_lookup is False
    assert first.notebook_id == nb
    assert first.source_id == "src-r"
    assert first.source_title == "Spec Report"
    assert first.section_path == "5.7.2.1 Loss and Crosstalk Mask"
    assert first.text == "insertion loss mask limits"
    assert first.element_ids == ["el-c-571"]
    capped = repo.retrieval.section_chunks(
        nb, "src-r", ["c-575", "c-571", "c-572"], 2, "loss question")
    assert [chunk.chunk_id for chunk in capped] == ["c-571", "c-572"]


def check_ownership_and_ceiling(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    other_nb = _new_notebook(repo, "other")
    seed.report(nb, "src-r")
    seed.source(nb, "src-x", title="Other Doc")
    seed.chunk(nb, "src-x", "x-1", "5.7.2.1 Loss and Crosstalk Mask", "foreign")
    seed.vector(nb, "x-1", (1.0, 0.0, 0.0, 0.0))
    seed.source(other_nb, "src-o")
    seed.chunk(other_nb, "src-o", "o-1", "Intro", "other library")
    _wire(repo, monkeypatch)
    retrieval = repo.retrieval
    chunks = retrieval.section_chunks(
        nb, "src-r", ["c-571", "x-1", "o-1", "missing"], 12, "loss question")
    assert [chunk.chunk_id for chunk in chunks] == ["c-571"]
    sims = retrieval.section_chunk_similarities(
        nb, "src-r", ["c-571", "x-1", "o-1"], ["loss question"])
    assert set(sims) == {"c-571"}
    # The wrong owner reads exactly like a missing source.
    assert retrieval.section_outline(other_nb, "src-r") == ((), False)
    assert retrieval.section_chunks(other_nb, "src-r", ["c-571"], 12, "q") == []

    missing = (
        retrieval.section_outline(nb, "src-absent"),
        retrieval.section_chunk_similarities(
            nb, "src-absent", ["c-571"], ["loss question"]),
        retrieval.section_chunks(nb, "src-absent", ["c-571"], 12, "loss question"),
    )
    assert missing == (((), False), {}, [])
    with source_scope_context(nb, _scope(["src-x"])):
        out_of_ceiling = (
            retrieval.section_outline(nb, "src-r"),
            retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571"], ["loss question"]),
            retrieval.section_chunks(nb, "src-r", ["c-571"], 12, "loss question"),
        )
        inside = retrieval.section_outline(nb, "src-x")
    assert out_of_ceiling == missing
    assert inside == ((("x-1", "5.7.2.1 Loss and Crosstalk Mask", False),), False)
    # A subjectless run admits only the libraries its ceilings name.
    with source_scope_context(
        other_nb, None, None, notebook_source_ceilings={other_nb: ["src-o"]},
        subjectless=True,
    ):
        assert retrieval.section_outline(nb, "src-r") == ((), False)
        assert retrieval.section_outline(other_nb, "src-o")[0] == (
            ("o-1", "Intro", False),)


def check_mounted_library_routing(repo, seed, monkeypatch):
    """A mounted library's chunks live under their OWNER id: read under it,
    stamped with it, and judged by the library dimension at the boundary."""
    active = _new_notebook(repo, "active")
    base = _new_notebook(repo, "base")
    seed.report(base, "src-base")
    _wire(repo, monkeypatch)
    retrieval = repo.retrieval
    assert retrieval.section_outline(active, "src-base") == ((), False)
    with source_scope_context(active, _scope([]), {
        "mode": "include", "notebook_ids": [base], "narrowed": True,
    }):
        rows, _ = retrieval.section_outline(base, "src-base")
        chunks = retrieval.section_chunks(
            base, "src-base", ["c-571"], 12, "loss question")
    assert [row[0] for row in rows][:2] == ["c-toc", "c-571"]
    assert [(chunk.chunk_id, chunk.notebook_id) for chunk in chunks] == [
        ("c-571", base)]
    with source_scope_context(active, _scope([]), {
        "mode": "include", "notebook_ids": [], "narrowed": True,
    }):
        assert retrieval.section_outline(base, "src-base") == ((), False)
        assert retrieval.section_chunks(
            base, "src-base", ["c-571"], 12, "loss question") == []


def check_navigation_drop_and_switch(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    hydrated: list = []
    real_hydrate = candidates.chunks.hydrate_rows

    def spy(db, ids):
        hydrated.append(list(ids))
        return real_hydrate(db, ids)

    monkeypatch.setattr(candidates.chunks, "hydrate_rows", spy)
    chunks = repo.retrieval.section_chunks(
        nb, "src-r", ["c-toc", "c-571"], 1, "loss question")
    # The navigation page takes no read and no slot of the limit.
    assert [chunk.chunk_id for chunk in chunks] == ["c-571"]
    assert hydrated == [["c-571"]]
    monkeypatch.setattr(
        candidates.settings, "retrieval_navigation_filter_enabled", False)
    chunks = repo.retrieval.section_chunks(
        nb, "src-r", ["c-toc", "c-571"], 12, "loss question")
    assert [chunk.chunk_id for chunk in chunks] == ["c-toc", "c-571"]


def check_relevance_ruler(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    _wire(repo, monkeypatch)
    question = "loss question"
    chunks = {
        chunk.chunk_id: chunk
        for chunk in repo.retrieval.section_chunks(
            nb, "src-r", ["c-571", "c-572", "c-574"], 12, question)
    }
    basis = keyword_basis(question)

    def keyword(text):
        return basis.coverage(set(_tokens(text)), text)

    assert chunks["c-571"].relevance == pytest.approx(
        _fuse(keyword("insertion loss mask limits"), 1.0, True))
    assert chunks["c-572"].relevance == pytest.approx(
        _fuse(keyword("crosstalk mask table"), 0.6, True))
    # No vector: keyword only, no floor -- still a defined relevance.
    assert chunks["c-574"].relevance == pytest.approx(
        _fuse(keyword("notes about the package"), 0.0, False))
    for chunk in chunks.values():
        assert chunk.score == chunk.relevance
    # Without an embedder every chunk keeps the keyword half.
    repo.retrieval.candidates.embedder = FixedEmbedder(table={})
    bare = repo.retrieval.section_chunks(nb, "src-r", ["c-571"], 12, question)
    assert bare[0].relevance == pytest.approx(
        _fuse(keyword("insertion loss mask limits"), 0.0, False))


def check_similarity_max_over_queries(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    _wire(repo, monkeypatch)
    sims = repo.retrieval.section_chunk_similarities(
        nb, "src-r", ["c-571", "c-572", "c-573", "c-574", "c-toc"],
        ["loss question", "crosstalk question", "", "unembeddable"])
    assert sims == pytest.approx({
        "c-571": 1.0, "c-572": 0.8, "c-573": 1.0, "c-toc": 0.0,
    })
    assert repo.retrieval.section_chunk_similarities(
        nb, "src-r", ["c-571"], []) == {}


def check_dimension_mismatch(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    _candidates, _embedder, events = _wire(repo, monkeypatch)
    assert repo.retrieval.section_chunk_similarities(
        nb, "src-r", ["c-571"], ["short"]) == {}
    with retrieval_run(run_kind="ask_reasoning"):
        for _ in range(2):
            assert repo.retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571"], ["short"]) == {}
    # Once per call outside a run, once per (library, source, reason) inside.
    assert events.of("section_vectors_unavailable") == [{
        "kind": "section_vectors_unavailable", "notebook_id": nb,
        "source_id": "src-r", "reason": "dim_mismatch",
        "outline_rows": len(REPORT), "query_dim": 3,
    }] * 2
    # A section read without usable vectors keeps its keyword relevance.
    chunks = repo.retrieval.section_chunks(nb, "src-r", ["c-571"], 12, "short")
    assert [chunk.chunk_id for chunk in chunks] == ["c-571"]


def check_vector_paths_agree(repo, seed, monkeypatch):
    """Warm notebook matrix borrowed vs. only the outline's rows read: same
    similarities; the cold path never loads the notebook matrix and the warm
    path never reads vector rows."""
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    seed.source(nb, "src-x", title="Other Doc")
    seed.chunk(nb, "src-x", "x-1", "Intro", "foreign")
    seed.vector(nb, "x-1", (1.0, 0.0, 0.0, 0.0))
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    by_ids: list = []
    real_by_ids = candidates.embeddings.vector_rows_for_ids

    def spy(db, notebook_id, table, id_col, ids):
        by_ids.append((notebook_id, table, id_col, sorted(ids)))
        return real_by_ids(db, notebook_id, table, id_col, ids)

    monkeypatch.setattr(candidates.embeddings, "vector_rows_for_ids", spy)
    ids = ["c-571", "c-572", "c-573"]
    queries = ["loss question", "crosstalk question"]
    cold = repo.retrieval.section_chunk_similarities(nb, "src-r", ids, queries)
    assert by_ids == [(nb, "chunk_embeddings", "chunk_id",
                       sorted(row[0] for row in REPORT))]
    with candidates._connect() as db:
        assert candidates._warm_vector_matrix_version(
            db, nb, "chunk_embeddings") is None
        warm_ids, _mat = candidates._vector_matrix(
            db, nb, "chunk_embeddings", "chunk_id")
    assert "x-1" in warm_ids
    by_ids.clear()
    warm = repo.retrieval.section_chunk_similarities(nb, "src-r", ids, queries)
    assert by_ids == []
    assert warm == pytest.approx(cold)
    assert set(warm) == set(ids)


def check_memo_reuse(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, embedder, _events = _wire(repo, monkeypatch)
    outline_reads: list = []
    vector_reads: list = []
    real_outline = candidates.chunks.source_outline_rows
    real_vectors = candidates.embeddings.vector_rows_for_ids

    def outline_spy(*args):
        outline_reads.append(args[1:3])
        return real_outline(*args)

    def vector_spy(*args):
        vector_reads.append(args[1])
        return real_vectors(*args)

    monkeypatch.setattr(candidates.chunks, "source_outline_rows", outline_spy)
    monkeypatch.setattr(candidates.embeddings, "vector_rows_for_ids", vector_spy)
    retrieval = repo.retrieval
    with retrieval_run(run_kind="ask_reasoning"):
        first = retrieval.section_outline(nb, "src-r")
        assert retrieval.section_outline(nb, "src-r") == first
        retrieval.section_chunk_similarities(nb, "src-r", ["c-571"], ["loss question"])
        retrieval.section_chunk_similarities(
            nb, "src-r", ["c-571"], ["loss question", "crosstalk question"])
        retrieval.section_chunks(nb, "src-r", ["c-571"], 12, "loss question")
    assert outline_reads == [(nb, "src-r")]
    assert vector_reads == [nb]
    assert embedder.calls == ["loss question", "crosstalk question"]
    # Outside a run nothing is retained.
    retrieval.section_outline(nb, "src-r")
    assert len(outline_reads) == 2


def check_ceiling_before_memo(repo, seed, monkeypatch):
    """The memo never answers for authority: a scope narrowed after the
    outline, vectors and rows were memoised in the same run reads nothing."""
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    seed.source(nb, "src-x", title="Other Doc")
    _wire(repo, monkeypatch)
    retrieval = repo.retrieval
    with retrieval_run(run_kind="ask_reasoning"):
        with source_scope_context(nb, _scope(["src-r", "src-x"])):
            assert retrieval.section_outline(nb, "src-r")[0]
            assert retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571"], ["loss question"])
            assert retrieval.section_chunks(
                nb, "src-r", ["c-571"], 12, "loss question")
        with source_scope_context(nb, _scope(["src-x"])):
            assert retrieval.section_outline(nb, "src-r") == ((), False)
            assert retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571"], ["loss question"]) == {}
            assert retrieval.section_chunks(
                nb, "src-r", ["c-571"], 12, "loss question") == []


def check_reference_library_hidden_source(repo, seed, monkeypatch):
    """A reference library with no per-notebook ceiling: ``allows`` admits
    every source of it, but a hidden projection (Memory / Knowhow) is not one
    of its visible sources and is neither outlined nor read."""
    active = _new_notebook(repo, "active")
    base = _new_notebook(repo, "base")
    seed.report(base, "src-base")
    seed.source(base, "src-hidden", title="Someone's Knowhow", kind="knowhow")
    seed.chunk(base, "src-hidden", "h-1", "Private", "private knowhow cell")
    seed.vector(base, "h-1", (1.0, 0.0, 0.0, 0.0))
    _wire(repo, monkeypatch)
    retrieval = repo.retrieval
    base_scope = {"mode": "include", "notebook_ids": [base], "narrowed": True}
    with retrieval_run(run_kind="ask_reasoning"):
        with source_scope_context(active, _scope([]), base_scope):
            from app.services.source_scope import current_source_scope

            scope = current_source_scope()
            assert scope.source_ceiling_for(base) is None
            assert scope.allows(base, "src-hidden")
            assert retrieval.section_outline(base, "src-hidden") == ((), False)
            assert retrieval.section_chunks(
                base, "src-hidden", ["h-1"], 12, "loss question") == []
            assert retrieval.section_chunk_similarities(
                base, "src-hidden", ["h-1"], ["loss question"]) == {}
            assert retrieval.section_outline(base, "src-base")[0]
    # No scope (a direct service caller): the historical unnarrowed answer.
    assert retrieval.section_outline(base, "src-hidden")[0] == (
        ("h-1", "Private", False),)


def check_read_limit_is_clamped_and_batched(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    batches: list = []
    real_hydrate = candidates.chunks.hydrate_rows

    def spy(db, ids):
        batches.append(len(list(ids)))
        return real_hydrate(db, ids)

    monkeypatch.setattr(candidates.chunks, "hydrate_rows", spy)
    monkeypatch.setattr(candidates.settings, "reasoning_section_read_max_chunks", 3)
    monkeypatch.setattr(candidates, "_IN_CHUNK", 2)
    chunks = repo.retrieval.section_chunks(
        nb, "src-r", [row[0] for row in REPORT], 10**6, "loss question")
    assert [chunk.chunk_id for chunk in chunks] == ["c-571", "c-572", "c-573"]
    assert batches == [2, 1]


def check_expected_dim_on_both_vector_paths(repo, seed, monkeypatch):
    """A stray vector of another width is left out by the cold read
    (``expected_dim``) and makes no warm matrix usable in the query's width
    differently: both paths keep exactly the rows of the query's dimension."""
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    seed.chunk(nb, "src-r", "c-odd", "Odd", "a stray three-wide vector")
    seed.vector(nb, "c-odd", (1.0, 0.0, 0.0))
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    ids = ["c-571", "c-572", "c-odd"]
    cold = repo.retrieval.section_chunk_similarities(
        nb, "src-r", ids, ["loss question"])
    assert cold == pytest.approx({"c-571": 1.0, "c-572": 0.6})
    with candidates._connect() as db:
        warm_ids, warm_mat = candidates._vector_matrix(
            db, nb, "chunk_embeddings", "chunk_id")
    if warm_mat.shape[1] == 4:
        warm = repo.retrieval.section_chunk_similarities(
            nb, "src-r", ids, ["loss question"])
        assert warm == pytest.approx(cold)
    else:  # the notebook matrix took the stray width: not borrowed
        assert repo.retrieval.section_chunk_similarities(
            nb, "src-r", ids, ["loss question"]) == pytest.approx(cold)
    assert "c-odd" not in cold


def _count_vector_reads(candidates, monkeypatch) -> list:
    reads: list = []
    real = candidates.embeddings.vector_rows_for_ids

    def spy(db, notebook_id, table, id_col, ids):
        ids = list(ids)
        reads.append(sorted(ids))
        return real(db, notebook_id, table, id_col, ids)

    monkeypatch.setattr(candidates.embeddings, "vector_rows_for_ids", spy)
    return reads


def check_process_cache_reuses_across_runs(repo, seed, monkeypatch):
    """A source's vectors are read once per process, not once per run; a new
    chunk generation (``chunked_at``) reads them again."""
    from app.services.section_retrieval import section_vector_stats

    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r", rows=FULL_REPORT)
    seed.chunked("src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    reads = _count_vector_reads(candidates, monkeypatch)

    def one_run():
        with retrieval_run(run_kind="ask_reasoning"):
            sims = repo.retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571", "c-572"], ["loss question"])
            return sims, section_vector_stats()

    first, first_stats = one_run()
    second, second_stats = one_run()
    assert second == first == pytest.approx({"c-571": 1.0, "c-572": 0.6})
    assert len(reads) == 1
    assert first_stats["vectors_read"] == len(FULL_REPORT)
    assert first_stats["vector_loads"] == 1
    assert first_stats["process_cache_hits"] == 0
    assert second_stats["vectors_read"] == 0 and second_stats["vector_loads"] == 0
    assert second_stats["process_cache_hits"] == 1
    assert candidates.section_vector_cache.stats()["entries"] == 1
    seed.chunked("src-r", "2026-10-23T00:00:00+00:00")
    one_run()
    assert len(reads) == 2


# ------------------------------------------------------------- SQLite only
@pytest.fixture
def backend(tmp_path, monkeypatch):
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    user = repo._runtime.identity.create_user("b00654321", "password-12")
    token = set_request_user(user)
    try:
        yield repo, SqliteSeed(repo)
    finally:
        reset_request_user(token)


CHECKS = (
    check_outline_rows_document_order,
    check_outline_scan_bound_and_switches,
    check_section_chunks_reads_in_document_order,
    check_ownership_and_ceiling,
    check_mounted_library_routing,
    check_navigation_drop_and_switch,
    check_relevance_ruler,
    check_similarity_max_over_queries,
    check_dimension_mismatch,
    check_vector_paths_agree,
    check_memo_reuse,
    check_ceiling_before_memo,
    check_reference_library_hidden_source,
    check_read_limit_is_clamped_and_batched,
    check_expected_dim_on_both_vector_paths,
    check_process_cache_reuses_across_runs,
)


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
def test_sqlite(backend, monkeypatch, check):
    repo, seed = backend
    check(repo, seed, monkeypatch)


def test_sqlite_outline_scan_rides_the_source_index_without_a_sort(backend):
    """The plan of the statement the ADAPTER sends (captured through the
    connection's trace hook, parameters expanded), not of a copy of it."""
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    sent: list[str] = []
    with repo._connect() as db:
        db.set_trace_callback(sent.append)
        try:
            repo.retrieval.candidates.chunks.source_outline_rows(db, nb, "src-r", 5)
        finally:
            db.set_trace_callback(None)
        (statement,) = [sql for sql in sent if "FROM chunks" in sql]
        plan = " | ".join(
            str(row["detail"])
            for row in db.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
        )
    assert "idx_chunks_source" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_warm_borrow_never_loads_when_the_entry_vanishes(backend, monkeypatch):
    """The warm verdict is kept for the run; an entry evicted after it is not
    reloaded by the borrow, and the section falls back to its own rows."""
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    loads: list = []
    real_rows = candidates.embeddings.vector_rows
    monkeypatch.setattr(
        candidates.embeddings, "vector_rows",
        lambda *args: loads.append(args) or real_rows(*args),
    )
    with candidates._connect() as db:
        candidates._vector_matrix(db, nb, "chunk_embeddings", "chunk_id")
        version = candidates._warm_vector_matrix_version(db, nb, "chunk_embeddings")
    assert version is not None and len(loads) == 1
    candidates._vector_cache.invalidate(f"{nb}:matrix:chunk_embeddings")
    assert candidates._borrow_vector_matrix(nb, "chunk_embeddings", version) is None
    with retrieval_run(run_kind="ask_reasoning"):
        sims = repo.retrieval.section_chunk_similarities(
            nb, "src-r", ["c-571"], ["loss question"])
    assert sims == pytest.approx({"c-571": 1.0})
    assert len(loads) == 1


def test_runtime_dim_truncates_both_sides(backend, monkeypatch):
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    monkeypatch.setattr(candidates.settings, "embed_runtime_dim", 2)
    sims = repo.retrieval.section_chunk_similarities(
        nb, "src-r", ["c-571", "c-572"], ["crosstalk question"])
    assert sims == pytest.approx({"c-571": 0.0, "c-572": 0.8})


def test_no_vectors_is_empty_with_one_event_per_run(backend, monkeypatch):
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r", rows=tuple(
        (cid, path, text, navigation, None)
        for cid, path, text, navigation, _vector in REPORT
    ))
    _candidates, _embedder, events = _wire(repo, monkeypatch)
    with retrieval_run(run_kind="ask_reasoning"):
        for _ in range(2):
            assert repo.retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571"], ["loss question"]) == {}
    assert events.of("section_vectors_unavailable") == [{
        "kind": "section_vectors_unavailable", "notebook_id": nb,
        "source_id": "src-r", "reason": "no_chunk_vectors",
        "outline_rows": len(REPORT), "query_dim": 4,
    }]


def test_duplicate_heading_occurrences_stay_separate_rows(backend, monkeypatch):
    """Folding into occurrences is the pure module's job: the port hands back
    every row, a heading that recurs later included, in document order."""
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    _wire(repo, monkeypatch)
    rows, _ = repo.retrieval.section_outline(nb, "src-r")
    mask = "5.7.2.1 Loss and Crosstalk Mask"
    assert [index for index, row in enumerate(rows) if row[1] == mask] == [1, 2, 5]


def test_section_read_marker_survives_duplicate_merge():
    def chunk(**flags):
        return RetrievedChunk(
            chunk_id="c", source_id="s", source_title="", section_path="",
            text="t", relevance=flags.pop("relevance", 0.5), **flags,
        )

    for existing, candidate in (
        (chunk(section_read=True), chunk(relevance=0.9)),
        (chunk(relevance=0.9), chunk(section_read=True)),
    ):
        assert prefer_stronger_chunk_candidate(existing, candidate).section_read
    assert not prefer_stronger_chunk_candidate(chunk(), chunk()).section_read
    assert np.isclose(chunk(section_read=True).relevance, 0.5)
    assert chunk(section_read=True) == chunk()  # provenance, not identity


def test_large_library_never_reads_the_matrix_version(backend, monkeypatch):
    """A library that is not copyable never has its chunk matrix warm-loaded,
    so its sections go straight to their own rows: no version aggregate."""
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    versions: list = []
    real_version = candidates.embeddings.version_row
    monkeypatch.setattr(
        candidates.embeddings, "version_row",
        lambda db, n, t: versions.append((n, t)) or real_version(db, n, t),
    )
    monkeypatch.setattr(
        candidates, "notebook_copy_stats", lambda _nb: {"copyable": False})
    sims = repo.retrieval.section_chunk_similarities(
        nb, "src-r", ["c-571"], ["loss question"])
    assert sims == pytest.approx({"c-571": 1.0})
    assert versions == []


def test_warmth_is_judged_once_per_library_per_run(backend, monkeypatch):
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    seed.source(nb, "src-2", title="Second")
    seed.chunk(nb, "src-2", "d-1", "Intro", "second doc text")
    seed.vector(nb, "d-1", (1.0, 0.0, 0.0, 0.0))
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    versions: list = []
    real_version = candidates.embeddings.version_row
    monkeypatch.setattr(
        candidates.embeddings, "version_row",
        lambda db, n, t: versions.append((n, t)) or real_version(db, n, t),
    )
    with retrieval_run(run_kind="ask_reasoning"):
        repo.retrieval.section_chunk_similarities(
            nb, "src-r", ["c-571"], ["loss question"])
        repo.retrieval.section_chunk_similarities(
            nb, "src-2", ["d-1"], ["loss question"])
        repo.retrieval.section_chunks(nb, "src-r", ["c-572"], 12, "loss question")
    assert versions == [(nb, "chunk_embeddings")]


def test_run_vector_memo_evicts_past_its_byte_budget(backend, monkeypatch):
    from app.services import section_retrieval

    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    seed.report(nb, "src-s", rows=tuple(
        (f"s-{cid}", path, text, navigation, vector)
        for cid, path, text, navigation, vector in REPORT
    ))
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    one_source = 5 * 4 * 4  # five 4-wide float32 vectors
    monkeypatch.setattr(
        section_retrieval, "SECTION_VECTOR_RUN_MEMO_MAX_BYTES", one_source)
    reads: list = []
    real_by_ids = candidates.embeddings.vector_rows_for_ids
    monkeypatch.setattr(
        candidates.embeddings, "vector_rows_for_ids",
        lambda db, n, t, c, ids: reads.append(sorted(ids)[0]) or real_by_ids(
            db, n, t, c, ids),
    )
    with retrieval_run(run_kind="ask_reasoning"):
        for _ in range(2):
            assert repo.retrieval.section_chunk_similarities(
                nb, "src-r", ["c-571"], ["loss question"])
            assert repo.retrieval.section_chunk_similarities(
                nb, "src-s", ["s-c-571"], ["loss question"])
    # Room for one source: each load evicts the other (least recently used).
    assert reads == ["c-571", "s-c-571", "c-571", "s-c-571"]


def _stamped_report(repo, seed, monkeypatch):
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r", rows=FULL_REPORT)
    seed.chunked("src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    return nb, candidates, _count_vector_reads(candidates, monkeypatch)


def _similarities_in_a_run(repo, nb, source_id="src-r", ids=("c-571",)):
    with retrieval_run(run_kind="ask_reasoning"):
        return repo.retrieval.section_chunk_similarities(
            nb, source_id, list(ids), ["loss question"])


def test_process_cache_misses_after_a_rechunk_that_kept_the_generation(
        backend, monkeypatch):
    """The outline fingerprint is part of the key: a source whose chunks
    changed without a new ``chunked_at`` (a projection rewrite) reads again."""
    repo, seed = backend
    nb, _candidates, reads = _stamped_report(repo, seed, monkeypatch)
    _similarities_in_a_run(repo, nb)
    seed.chunk(nb, "src-r", "c-576", "Appendix", "appendix")
    seed.vector(nb, "c-576", (1.0, 0.0, 0.0, 0.0))
    _similarities_in_a_run(repo, nb)
    _similarities_in_a_run(repo, nb)
    assert len(reads) == 2


def test_process_cache_keeps_only_complete_sources(backend, monkeypatch):
    """A source with an outline chunk still missing its vector is not kept
    across runs (it may be mid-embedding)."""
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")  # c-574 has no vector
    seed.chunked("src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    reads = _count_vector_reads(candidates, monkeypatch)
    _similarities_in_a_run(repo, nb)
    _similarities_in_a_run(repo, nb)
    assert len(reads) == 2
    assert candidates.section_vector_cache.stats()["entries"] == 0


def test_process_cache_off_and_without_a_generation(backend, monkeypatch):
    repo, seed = backend
    nb, candidates, reads = _stamped_report(repo, seed, monkeypatch)
    monkeypatch.setattr(
        candidates.settings, "reasoning_section_vector_cache_max_bytes", 0)
    _similarities_in_a_run(repo, nb)
    _similarities_in_a_run(repo, nb)
    assert len(reads) == 2
    monkeypatch.setattr(
        candidates.settings, "reasoning_section_vector_cache_max_bytes", 1 << 20)
    seed.source(nb, "src-u", title="Unstamped")
    seed.chunk(nb, "src-u", "u-1", "Intro", "unstamped")
    seed.vector(nb, "u-1", (1.0, 0.0, 0.0, 0.0))
    _similarities_in_a_run(repo, nb, "src-u", ["u-1"])
    _similarities_in_a_run(repo, nb, "src-u", ["u-1"])
    assert len(reads) == 4


def test_process_cache_evicts_least_recently_used_past_its_budget(
        backend, monkeypatch):
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    full = FULL_REPORT
    seed.report(nb, "src-r", rows=full)
    seed.report(nb, "src-s", rows=tuple(
        (f"s-{cid}", *rest) for cid, *rest in full))
    seed.chunked("src-r")
    seed.chunked("src-s")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    reads = _count_vector_reads(candidates, monkeypatch)
    one_source = len(full) * 4 * 4
    monkeypatch.setattr(
        candidates.settings, "reasoning_section_vector_cache_max_bytes", one_source)
    for source_id, ids in (("src-r", ["c-571"]), ("src-r", ["c-571"]),
                           ("src-s", ["s-c-571"]), ("src-r", ["c-571"])):
        _similarities_in_a_run(repo, nb, source_id, ids)
    assert [read[0] for read in reads] == ["c-571", "s-c-571", "c-571"]
    assert candidates.section_vector_cache.stats() == {
        "entries": 1, "bytes": one_source}


def test_concurrent_first_reads_decode_a_source_once(backend, monkeypatch):
    """Report sections in parallel: one run, four threads, one source -- one
    read (the run memo); four runs at once -- still one read (the process
    cache). Both are single-flight."""
    import contextvars
    import threading
    import time

    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    full = FULL_REPORT
    seed.report(nb, "src-r", rows=full)
    seed.chunked("src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    reads: list = []
    real = candidates.embeddings.vector_rows_for_ids

    def slow(db, notebook_id, table, id_col, ids):
        reads.append(1)
        time.sleep(0.2)
        return real(db, notebook_id, table, id_col, ids)

    monkeypatch.setattr(candidates.embeddings, "vector_rows_for_ids", slow)

    def similarities():
        return repo.retrieval.section_chunk_similarities(
            nb, "src-r", ["c-571"], ["loss question"])

    def race(targets):
        start = threading.Barrier(len(targets))
        results: list = []

        def go(target):
            start.wait()
            results.append(target())

        threads = [threading.Thread(target=go, args=(target,)) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        return results

    monkeypatch.setattr(
        candidates.settings, "reasoning_section_vector_cache_max_bytes", 0)
    with retrieval_run(run_kind="ask_reasoning"):
        repo.retrieval.section_outline(nb, "src-r")
        contexts = [contextvars.copy_context() for _ in range(4)]
        results = race([
            (lambda context=context: context.run(similarities))
            for context in contexts
        ])
    assert len(results) == 4 and reads == [1]

    reads.clear()
    monkeypatch.setattr(
        candidates.settings, "reasoning_section_vector_cache_max_bytes", 1 << 20)

    def in_a_new_run():
        with retrieval_run(run_kind="ask_reasoning"):
            return similarities()

    results = race([in_a_new_run] * 4)
    assert len(results) == 4
    assert all(result == pytest.approx({"c-571": 1.0}) for result in results)
    assert reads == [1]


def test_section_read_scores_against_only_its_own_vectors(backend, monkeypatch):
    """A section read of two chunks reads those two vectors, not the source's."""
    from app.services.section_retrieval import section_vector_stats

    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    candidates, _embedder, _events = _wire(repo, monkeypatch)
    reads = _count_vector_reads(candidates, monkeypatch)
    with retrieval_run(run_kind="ask_reasoning"):
        chunks = repo.retrieval.section_chunks(
            nb, "src-r", [row[0] for row in REPORT], 2, "loss question")
        stats = section_vector_stats()
    assert [chunk.chunk_id for chunk in chunks] == ["c-571", "c-572"]
    assert reads == [["c-571", "c-572"]]
    assert stats["vectors_read"] == 2 and stats["vector_loads"] == 1
    # Once the run holds the whole source, a read reuses it: no new read.
    with retrieval_run(run_kind="ask_reasoning"):
        repo.retrieval.section_chunk_similarities(
            nb, "src-r", ["c-571"], ["loss question"])
        reads.clear()
        repo.retrieval.section_chunks(nb, "src-r", ["c-572"], 2, "loss question")
    assert reads == []
    assert section_vector_stats() == {
        "vectors_read": 0, "vector_ms": 0, "vector_loads": 0,
        "process_cache_hits": 0}


def test_process_cache_never_crosses_databases(tmp_path, monkeypatch):
    """Two databases holding the SAME notebook / source / chunk ids (the e2e
    fixtures do exactly this) never serve each other's vectors, even through
    one shared process cache."""
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )

    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repos = []
    for name, vector in (("a", (1.0, 0.0, 0.0, 0.0)), ("b", (0.0, 1.0, 0.0, 0.0))):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f'{name}.db'}")
        repo = SQLiteRepository(Settings(_env_file=None))
        bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
        user = repo._runtime.identity.create_user("b00654321", "password-12")
        token = set_request_user(user)
        try:
            seed = SqliteSeed(repo)
            with repo._write() as db:
                db.execute(
                    "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                    "created_by,created_at,updated_at) "
                    "VALUES ('nb-same','same','','x','ready',?,?,?)",
                    (user.id, T0.isoformat(), T0.isoformat()),
                )
            seed.source("nb-same", "src-same")
            seed.chunk("nb-same", "src-same", "c-same", "Intro", "same id")
            seed.vector("nb-same", "c-same", vector)
            seed.chunked("src-same")
        finally:
            reset_request_user(token)
        _wire(repo, monkeypatch)
        repos.append(repo)
    shared = repos[0].retrieval.candidates.section_vector_cache
    repos[1].retrieval.candidates.section_vector_cache = shared
    answers = []
    for repo in repos:
        with retrieval_run(run_kind="ask_reasoning"):
            answers.append(repo.retrieval.section_chunk_similarities(
                "nb-same", "src-same", ["c-same"], ["loss question"]))
    assert answers == [pytest.approx({"c-same": 1.0}), pytest.approx({"c-same": 0.0})]
    assert shared.stats()["entries"] == 2
