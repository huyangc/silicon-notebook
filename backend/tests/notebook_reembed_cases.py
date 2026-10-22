"""Notebook re-embed job behaviour (``app.services.notebook_reembed``) -- **both backends
run the same checks on the same world**: ``tests/test_notebook_reembed.py`` (SQLite) and
``tests/postgres/test_notebook_reembed_pg.py`` (PostgreSQL) each supply only an ``insert``
adapter, a placeholder style, the maintenance module and a repository.

The world has every kind of vector: a document source with chunks and elements (plus one
blank chunk no model can embed), three document knowledge objects (plus one without
payload text) with two relations, and one Knowhow table of two rows by three columns (its
projection adds a hidden source with six chunks, objects and edges). Each embedding
workload is bound to its own recording fake, so a check can tell which workload embedded
what and in how many calls.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Callable

import pytest

from app.domain.indexing_pipeline import IndexingPipelineUnavailableError
from app.models.schemas import NotebookCreate
from app.services import batch_ingest, notebook_reembed
from app.services.embedding import FakeEmbedder
from app.services.knowhow import api as knowhow_api
from app.services.model_work import ModelProviderError
from app.services.notebook_reembed import (
    LEGS,
    SOURCE_VECTOR_LEGS,
    LegProgress,
    NotebookReembedService,
)
from tests.model_testkit import bind_embedding_client

DIM = 16
BATCH = 2
ENV = {"EMBED_DIM": str(DIM), "EMBED_BATCH_SIZE": str(BATCH)}
"""Settings every world needs (set before the repository is built)."""

_NOW = "2026-01-01T00:00:00+00:00"
DOC_CHUNKS = 5
DOC_ELEMENTS = 5
KNOWHOW_ROWS = (
    ("controller hangs", "knowhow cell: reboot the controller before reflashing", "jtag"),
    ("flash fails", "knowhow cell: erase the boot sector first", "programmer"),
)
KNOWHOW_CHUNKS = 6
KNOWHOW_TEXT = KNOWHOW_ROWS[0][1]

WORKLOADS = (
    "chunk_embedding",
    "source_element_embedding",
    "knowledge_object_embedding",
    "relation_embedding",
    "knowhow_embedding",
)
VECTOR_TABLES = {
    "chunk_embeddings": "chunk_id",
    "element_embeddings": "element_id",
    "knowledge_embeddings": "object_id",
    "relation_embeddings": "relation_id",
}


@dataclasses.dataclass(frozen=True)
class Backend:
    name: str
    insert: Callable[[Any, str, dict], None]
    placeholder: str
    """``?`` (SQLite) or ``%s`` (PostgreSQL)."""
    maintenance_module: Any
    relation_page_attr: str
    """The module constant that sizes a relation-backfill discovery page."""


@dataclasses.dataclass(frozen=True)
class World:
    notebook_id: str
    table_id: str
    knowhow_source_id: str
    embedders: dict


class RecordingEmbedder:
    """One workload's fake: records every call's texts; ``fail_calls`` makes those call
    numbers (1-based) raise a provider error."""

    configured = True

    def __init__(self) -> None:
        self._fake = FakeEmbedder(dim=DIM)
        self.calls: list[list[str]] = []
        self.fail_calls: set[int] = set()
        self.dim = DIM

    def embed_texts(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        if len(self.calls) in self.fail_calls:
            raise ModelProviderError("boom", code="provider_unavailable")
        return self._fake.embed_texts(texts)

    def embed_query(self, text):
        return self._fake.embed_query(text)

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


def bind_embedders(repo, workloads=WORKLOADS) -> dict:
    embedders = {workload: RecordingEmbedder() for workload in workloads}
    for workload, embedder in embedders.items():
        bind_embedding_client(repo, embedder, workload_ids=[workload])
    return embedders


def reset_calls(world: World) -> None:
    for embedder in world.embedders.values():
        embedder.calls.clear()
        embedder.fail_calls.clear()


def _sql(backend: Backend, statement: str) -> str:
    return statement.replace("?", backend.placeholder)


def build_world(repo, backend: Backend, *, notebook_id: "str | None" = None) -> World:
    """The world with every vector filled once (the Knowhow chunks by the projection,
    the rest by a full run), then every recorder cleared. ``notebook_id`` seeds an
    existing (for example API-created) notebook instead of a new one."""
    embedders = bind_embedders(repo)
    nb = notebook_id or repo.create_notebook(NotebookCreate(name="reembed")).id
    src = f"src-doc-{nb}"
    with repo._runtime.database.write() as db:
        backend.insert(db, "sources", {
            "id": src, "notebook_id": nb, "title": "manual", "source_type": "document",
            "status": "extracted", "parse_status": "extracted",
            "created_at": _NOW, "updated_at": _NOW,
        })
        for i in range(DOC_ELEMENTS):
            backend.insert(db, "source_elements", {
                "id": f"el-{src}-{i:03d}", "source_id": src, "element_type": "paragraph",
                "location_label": f"p{i}", "text": f"element text {i}", "metadata": {},
                "created_at": _NOW,
            })
        for i in range(DOC_CHUNKS):
            backend.insert(db, "chunks", {
                "id": f"ck-{src}-{i:03d}", "notebook_id": nb, "source_id": src,
                "text": f"chunk text {i}", "section_path": "", "element_ids": [],
                "created_at": _NOW,
            })
        backend.insert(db, "chunks", {  # blank: never embeddable, never "missing"
            "id": f"ck-{src}-blank", "notebook_id": nb, "source_id": src,
            "text": "   ", "section_path": "", "element_ids": [], "created_at": _NOW,
        })
        for name in ("controller", "firmware", "bootloader"):
            backend.insert(db, "knowledge_objects", {
                "id": f"ko-{nb}-{name}", "notebook_id": nb, "object_type": "concept",
                "status": "approved", "payload": {"name": name}, "evidence": [],
                "source_id": src, "created_at": _NOW, "updated_at": _NOW,
            })
        backend.insert(db, "knowledge_objects", {  # no payload text: never embeddable
            "id": f"ko-{nb}-blank", "notebook_id": nb, "object_type": "concept",
            "status": "approved", "payload": {}, "evidence": [],
            "source_id": src, "created_at": _NOW, "updated_at": _NOW,
        })
        for left, right in (("firmware", "controller"), ("bootloader", "firmware")):
            backend.insert(db, "knowledge_relations", {
                "id": f"rel-{nb}-{left}-{right}", "notebook_id": nb, "source_id": src,
                "source_object_id": f"ko-{nb}-{left}",
                "target_object_id": f"ko-{nb}-{right}", "edge_type": "part_of",
                "evidence": [], "created_at": _NOW,
            })
    table_id = repo.create_knowhow_table(
        nb, "troubleshooting", "", [
            {"name": "symptom", "role": "anchor"},
            {"name": "fix", "role": "procedure"},
            {"name": "tool", "role": "entity"},
        ],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    for symptom, fix, tool in KNOWHOW_ROWS:
        repo.add_knowhow_row(table_id, {
            columns["symptom"]: symptom, columns["fix"]: fix, columns["tool"]: tool,
        })
    knowhow_api.build_projector(repo).project_table(table_id)
    hidden = repo.get_knowhow_table(table_id)["hidden_source_id"]
    world = World(nb, table_id, hidden, embedders)
    assert len(knowhow_chunk_ids(repo, backend, world)) == KNOWHOW_CHUNKS
    status = NotebookReembedService(repo).run(nb, legs=LEGS)
    assert status.state == "succeeded", status
    reset_calls(world)
    return world


def vector_ids(repo, backend: Backend, notebook_id: str) -> dict[str, set[str]]:
    with repo._runtime.database.connect() as db:
        return {
            table: {
                str(row[column]) for row in db.execute(
                    _sql(backend, f"SELECT {column} FROM {table} WHERE notebook_id=?"),
                    (notebook_id,),
                ).fetchall()
            }
            for table, column in VECTOR_TABLES.items()
        }


def delete_vectors(repo, backend: Backend, notebook_id: str, tables=VECTOR_TABLES) -> None:
    with repo._runtime.database.write() as db:
        for table in tables:
            db.execute(_sql(backend, f"DELETE FROM {table} WHERE notebook_id=?"), (notebook_id,))


def delete_chunk_vectors(repo, backend: Backend, chunk_ids) -> None:
    with repo._runtime.database.write() as db:
        for chunk_id in chunk_ids:
            db.execute(_sql(backend, "DELETE FROM chunk_embeddings WHERE chunk_id=?"), (chunk_id,))


def knowhow_chunk_ids(repo, backend: Backend, world: World) -> set[str]:
    with repo._runtime.database.connect() as db:
        return {
            str(row["id"]) for row in db.execute(
                _sql(backend, "SELECT id FROM chunks WHERE source_id=?"),
                (world.knowhow_source_id,),
            ).fetchall()
        }


def kg_mutation_seq(repo, backend: Backend, notebook_id: str) -> int:
    with repo._runtime.database.connect() as db:
        row = db.execute(
            _sql(backend, "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?"),
            (notebook_id,),
        ).fetchone()
    return int(row["kg_mutation_seq"]) if row is not None else 0


def _calls_for(rows: int) -> int:
    """Model calls for ``rows`` texts when every call carries one full batch."""
    return math.ceil(rows / BATCH)


def claim_backfill(notebook_id: str) -> None:
    """Take the shared guard the way the route does before its job runs."""
    assert NotebookReembedService.claim(notebook_id, SOURCE_VECTOR_LEGS).started


def _assert_batched(embedder: RecordingEmbedder, rows: int) -> None:
    assert len(embedder.calls) == _calls_for(rows), embedder.calls
    assert all(len(call) <= BATCH for call in embedder.calls)


# --------------------------------------------------------------------------- checks
def check_every_vector_comes_back_once(repo, backend: Backend) -> None:
    """Delete all four vector tables, run every leg: every vector is back, each workload
    is called once per batch (not once per row), the Knowhow chunks are embedded by
    ``knowhow_embedding`` and never by ``chunk_embedding``, rows nothing can embed (a
    blank chunk, an object without text) are never reported missing, and a second run
    makes no model call at all."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)
    knowhow_chunks = knowhow_chunk_ids(repo, backend, world)
    assert knowhow_chunks <= before["chunk_embeddings"]
    assert all(before.values()), before
    assert f"ko-{nb}-blank" not in before["knowledge_embeddings"]
    delete_vectors(repo, backend, nb)

    status = NotebookReembedService(repo).run(nb, legs=LEGS)

    assert status.state == "succeeded", status
    assert vector_ids(repo, backend, nb) == before
    for leg in LEGS:
        assert status.progress[leg].state == "done", status
        assert status.progress[leg].remaining == 0, status
    assert status.progress["chunks"].embedded == len(before["chunk_embeddings"])
    assert status.progress["elements"].embedded == len(before["element_embeddings"])
    assert status.progress["kg_objects"].embedded == len(before["knowledge_embeddings"])
    assert status.progress["kg_relations"].embedded == len(before["relation_embeddings"])
    e = world.embedders
    _assert_batched(e["chunk_embedding"], DOC_CHUNKS)
    _assert_batched(e["knowhow_embedding"], KNOWHOW_CHUNKS)
    _assert_batched(e["source_element_embedding"], len(before["element_embeddings"]))
    _assert_batched(e["knowledge_object_embedding"], len(before["knowledge_embeddings"]))
    _assert_batched(e["relation_embedding"], len(before["relation_embeddings"]))
    assert KNOWHOW_TEXT in e["knowhow_embedding"].texts
    assert all("knowhow cell" not in text for text in e["chunk_embedding"].texts)

    reset_calls(world)
    again = NotebookReembedService(repo).run(nb, legs=LEGS)
    assert again.state == "succeeded", again
    assert {w: len(x.calls) for w, x in e.items()} == {w: 0 for w in e}
    assert all(again.progress[leg].embedded == 0 for leg in LEGS)


def check_backfill_job_fills_knowhow_without_touching_the_graph(repo, backend: Backend) -> None:
    """The ``backfill-vectors`` job (chunks + elements) fills a Knowhow table's chunks
    through ``knowhow_embedding`` in batches, and only that: no reprojection, so the
    table's relation vectors and ``kg_mutation_seq`` stay exactly as they were."""
    from app.api import source_routes

    world = build_world(repo, backend)
    nb = world.notebook_id
    knowhow_chunks = knowhow_chunk_ids(repo, backend, world)
    delete_chunk_vectors(repo, backend, knowhow_chunks)
    relations = vector_ids(repo, backend, nb)["relation_embeddings"]
    assert any(rid.startswith("kr-kh-") for rid in relations)  # the table's own edges
    seq = kg_mutation_seq(repo, backend, nb)

    claim_backfill(nb)
    source_routes._backfill_vectors_job(repo, nb)

    assert knowhow_chunks <= vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert vector_ids(repo, backend, nb)["relation_embeddings"] == relations
    assert kg_mutation_seq(repo, backend, nb) == seq
    assert world.embedders["chunk_embedding"].calls == []
    _assert_batched(world.embedders["knowhow_embedding"], KNOWHOW_CHUNKS)
    assert nb not in notebook_reembed._active_runs


def check_knowhow_fill_skips_a_chunk_rewritten_mid_call(repo, backend: Backend) -> None:
    """A Knowhow chunk whose text changes while the model is embedding it (a cell edit
    landing mid-call) is not given its old text's vector; the next run embeds the new
    text."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    knowhow_chunks = sorted(knowhow_chunk_ids(repo, backend, world))
    delete_chunk_vectors(repo, backend, knowhow_chunks)
    edited = knowhow_chunks[0]
    embedder = world.embedders["knowhow_embedding"]
    inner = embedder.embed_texts

    def _edit_during_first_call(texts):
        if not embedder.calls:
            with repo._runtime.database.write() as db:
                db.execute(
                    _sql(backend, "UPDATE chunks SET text=? WHERE id=?"),
                    ("knowhow cell: edited while embedding", edited),
                )
        return inner(texts)

    embedder.embed_texts = _edit_during_first_call
    status = NotebookReembedService(repo).run(nb, legs=["chunks"])

    have = vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert edited not in have
    assert set(knowhow_chunks) - {edited} <= have
    assert status.progress["chunks"].remaining == 1
    embedder.embed_texts = inner
    reset_calls(world)
    NotebookReembedService(repo).run(nb, legs=["chunks"])
    assert edited in vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert embedder.texts == ["knowhow cell: edited while embedding"]


def check_offline_missing_only_embed_keeps_knowhow_on_its_workload(repo, backend: Backend) -> None:
    """``embed --missing-only`` (``batch_ingest.backfill_chunk_embeddings``): document
    chunks through ``chunk_embedding``, Knowhow chunks through ``knowhow_embedding``,
    batched, nothing else touched."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)
    relations = before["relation_embeddings"]
    seq = kg_mutation_seq(repo, backend, nb)
    delete_vectors(repo, backend, nb, tables=("chunk_embeddings",))

    batch_ingest.backfill_chunk_embeddings(repo, nb, missing_only=True)

    after = vector_ids(repo, backend, nb)
    assert after["chunk_embeddings"] == before["chunk_embeddings"]
    assert after["relation_embeddings"] == relations
    assert kg_mutation_seq(repo, backend, nb) == seq
    assert all("knowhow cell" not in text for text in world.embedders["chunk_embedding"].texts)
    _assert_batched(world.embedders["knowhow_embedding"], KNOWHOW_CHUNKS)


def check_object_vectors_mark_the_graph_dirty(repo, backend: Backend) -> None:
    """Writing object vectors advances ``kg_mutation_seq`` exactly once per pass; a pass
    that wrote none leaves it alone."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    delete_vectors(repo, backend, nb, tables=("knowledge_embeddings",))
    seq = kg_mutation_seq(repo, backend, nb)

    status = NotebookReembedService(repo).run(nb, legs=["kg_objects"])

    assert status.progress["kg_objects"].embedded > 0, status
    assert kg_mutation_seq(repo, backend, nb) == seq + 1
    NotebookReembedService(repo).run(nb, legs=["kg_objects"])
    assert kg_mutation_seq(repo, backend, nb) == seq + 1


def check_model_failure_is_visible_and_resumes(repo, backend: Backend) -> None:
    """A failing model batch fails the run visibly (the leg is ``incomplete`` with the
    missing count), keeps what was written, raises nothing, and the next run embeds only
    the rest."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)
    delete_vectors(repo, backend, nb, tables=("element_embeddings", "relation_embeddings"))
    elements = world.embedders["source_element_embedding"]
    relations = world.embedders["relation_embedding"]
    elements.fail_calls = {2}
    relations.fail_calls = {1}

    status = NotebookReembedService(repo).run(nb, legs=["elements", "kg_relations"])

    assert status.state == "failed", status
    leg = status.progress["elements"]
    assert (leg.state, leg.embedded, leg.remaining) == ("incomplete", DOC_ELEMENTS - BATCH, BATCH)
    rel = status.progress["kg_relations"]
    assert rel.state == "incomplete" and rel.remaining > 0, status
    assert "elements: incomplete" in status.error
    kept = vector_ids(repo, backend, nb)
    assert len(kept["element_embeddings"]) == DOC_ELEMENTS - BATCH
    still_missing = before["element_embeddings"] - kept["element_embeddings"]

    reset_calls(world)
    resumed = NotebookReembedService(repo).run(nb, legs=["elements", "kg_relations"])

    assert resumed.state == "succeeded", resumed
    assert vector_ids(repo, backend, nb) == before
    # Only the rows the failed batch left behind are embedded again.
    assert sorted(world.embedders["source_element_embedding"].texts) == sorted(
        f"element text {int(element_id.rsplit('-', 1)[1])}" for element_id in still_missing
    )


def check_unbound_workload_is_visible(repo, backend: Backend) -> None:
    """A leg whose workload is unbound does not run, says so, and fails the run without
    raising; a chunk leg with only its Knowhow half unbound runs the document half and
    names the missing workload."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    original = repo.configured
    unbound = {"knowledge_object_embedding", "knowhow_embedding"}
    repo.configured = lambda workload: workload not in unbound and original(workload)
    try:
        delete_vectors(repo, backend, nb, tables=("knowledge_embeddings", "chunk_embeddings"))
        status = NotebookReembedService(repo).run(nb, legs=["kg_objects", "chunks"])
    finally:
        repo.configured = original
    assert status.state == "failed", status
    assert status.progress["kg_objects"] == LegProgress(state="skipped", detail="not_configured")
    chunks = status.progress["chunks"]
    assert (chunks.state, chunks.detail) == ("incomplete", "not_configured:knowhow_embedding")
    assert chunks.embedded == DOC_CHUNKS
    assert "kg_objects: skipped/not_configured" in status.error
    assert world.embedders["knowledge_object_embedding"].calls == []
    assert world.embedders["knowhow_embedding"].calls == []
    assert vector_ids(repo, backend, nb)["knowledge_embeddings"] == set()


def check_a_failed_discovery_fails_its_leg(repo, backend: Backend, monkeypatch) -> None:
    """A per-source discovery query that raises fails the leg (never ``done``) and the
    run, without stopping the job."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    delete_vectors(repo, backend, nb, tables=("element_embeddings",))

    def _timeout(*_args, **_kwargs):
        raise TimeoutError("statement timeout")

    monkeypatch.setattr(repo.maintenance, "missing_element_embedding_ids", _timeout)
    status = NotebookReembedService(repo).run(nb, legs=["elements"])

    assert status.state == "failed", status
    assert status.progress["elements"].state == "failed"
    assert status.progress["elements"].detail == "TimeoutError"
    assert nb not in notebook_reembed._active_runs


def check_a_pipeline_switch_aborts_the_pass(repo, backend: Backend, monkeypatch) -> None:
    """The gate re-checked at a source lock raises out of the pass: the run fails with
    it, the later legs never start, the guard is released."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    delete_vectors(repo, backend, nb, tables=("element_embeddings", "knowledge_embeddings"))
    calls = []
    real_gate = repo.require_indexing_pipeline_write

    def _gate(notebook_id):
        calls.append(notebook_id)
        if len(calls) >= 2:  # the leg's own check passes, the source lock's fails
            raise IndexingPipelineUnavailableError("pending:generation")
        return real_gate(notebook_id)

    monkeypatch.setattr(repo, "require_indexing_pipeline_write", _gate)
    with pytest.raises(IndexingPipelineUnavailableError):
        NotebookReembedService(repo).run(nb, legs=["elements", "kg_objects"])

    assert world.embedders["knowledge_object_embedding"].calls == []
    assert nb not in notebook_reembed._active_runs
    runs = [r for r in notebook_reembed._runs.values() if r.notebook_id == nb]
    status = runs[-1].snapshot()
    assert (status.state, status.error) == ("failed", "IndexingPipelineUnavailableError")
    assert status.progress["elements"].state == "failed"

    # A KG leg checks the gate itself before it starts.
    delete_vectors(repo, backend, nb, tables=("relation_embeddings",))
    monkeypatch.setattr(
        repo, "require_indexing_pipeline_write",
        lambda _nb: (_ for _ in ()).throw(IndexingPipelineUnavailableError("pending")),
    )
    with pytest.raises(IndexingPipelineUnavailableError):
        NotebookReembedService(repo).run(nb, legs=["kg_relations"])
    assert world.embedders["relation_embedding"].calls == []


def check_relation_backfill_pages(repo, backend: Backend, monkeypatch) -> None:
    """Relations are discovered a page at a time and hydrated by those ids only."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)["relation_embeddings"]
    assert len(before) >= 3
    delete_vectors(repo, backend, nb, tables=("relation_embeddings",))
    monkeypatch.setattr(backend.maintenance_module, backend.relation_page_attr, 1)
    retrieval = repo._runtime.retrieval_component
    hydrated: list = []
    real = retrieval.relations_with_names

    def _spy(notebook_id, relation_ids=None):
        hydrated.append(relation_ids)
        return real(notebook_id, relation_ids)

    monkeypatch.setattr(retrieval, "relations_with_names", _spy)
    status = NotebookReembedService(repo).run(nb, legs=["kg_relations"])

    assert status.state == "succeeded", status
    assert vector_ids(repo, backend, nb)["relation_embeddings"] == before
    assert len(hydrated) >= len(before)
    assert all(ids is not None and len(ids) <= 1 for ids in hydrated)


def check_object_leg_keeps_its_count_when_it_fails(repo, backend: Backend, monkeypatch) -> None:
    """Object progress is reported while the leg runs, and a leg that raises after
    writing still shows what it wrote."""
    nb = build_world(repo, backend).notebook_id
    delete_vectors(repo, backend, nb, tables=("knowledge_embeddings",))
    real = repo.maintenance.backfill_node_embeddings

    def _then_fail(notebook_id, *args, **kwargs):
        real(notebook_id, *args, **kwargs)
        raise RuntimeError("connection lost")

    monkeypatch.setattr(repo.maintenance, "backfill_node_embeddings", _then_fail)
    status = NotebookReembedService(repo).run(nb, legs=["kg_objects"])

    leg = status.progress["kg_objects"]
    assert (leg.state, leg.detail) == ("failed", "RuntimeError")
    assert leg.embedded == len(vector_ids(repo, backend, nb)["knowledge_embeddings"]) > 0
