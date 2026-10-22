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
    json_param: Callable[[Any], Any]
    """How a JSON column value is bound (text on SQLite, jsonb on PostgreSQL)."""


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


def add_knowhow_table(repo, notebook_id: str, title: str, cells: list) -> str:
    """A second projected table (one ``fix`` column per row); returns its hidden source."""
    table_id = repo.create_knowhow_table(
        notebook_id, title, "", [{"name": "fix", "role": "procedure"}],
    )
    column = repo.get_knowhow_table(table_id)["columns"][0]["id"]
    for text in cells:
        repo.add_knowhow_row(table_id, {column: text})
    knowhow_api.build_projector(repo).project_table(table_id)
    return repo.get_knowhow_table(table_id)["hidden_source_id"]


def check_knowhow_fill_discovers_ids_then_reads_page_by_page(
    repo, backend: Backend, monkeypatch
) -> None:
    """The Knowhow fill discovers ids only (one query per table), then reads texts one
    page at a time, and only that page's ids."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    chunks = sorted(knowhow_chunk_ids(repo, backend, world))
    delete_chunk_vectors(repo, backend, chunks)
    monkeypatch.setattr(knowhow_api, "KNOWHOW_FILL_PAGE_ROWS", BATCH)
    mnt = repo.maintenance
    discovered: list = []
    hydrated: list = []
    real_ids, real_texts = mnt.missing_knowhow_chunk_embedding_ids, mnt.chunk_texts_by_ids

    def _ids(notebook_id, *, source_id):
        ids = real_ids(notebook_id, source_id=source_id)
        discovered.append((source_id, ids))
        return ids

    def _texts(ids):
        hydrated.append(list(ids))
        return real_texts(ids)

    monkeypatch.setattr(mnt, "missing_knowhow_chunk_embedding_ids", _ids)
    monkeypatch.setattr(mnt, "chunk_texts_by_ids", _texts)
    knowhow_api.fill_missing_knowhow_chunk_vectors(repo, nb)

    # One discovery to fill, one recount (still under the lock) to report remaining.
    assert discovered == [(world.knowhow_source_id, chunks), (world.knowhow_source_id, [])]
    assert all(isinstance(chunk_id, str) for chunk_id in discovered[0][1])
    assert hydrated == [chunks[i:i + BATCH] for i in range(0, len(chunks), BATCH)]
    assert set(chunks) <= vector_ids(repo, backend, nb)["chunk_embeddings"]


def check_conditional_chunk_vector_write(repo, backend: Backend) -> None:
    """``replace_chunk_vectors_if_text``: a vector lands only on a chunk that still holds
    the embedded text; a changed or missing chunk gets nothing and raises nothing."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    chunk_id = sorted(knowhow_chunk_ids(repo, backend, world))[0]
    delete_chunk_vectors(repo, backend, [chunk_id])
    with repo._runtime.database.connect() as db:
        text = db.execute(
            _sql(backend, "SELECT text FROM chunks WHERE id=?"), (chunk_id,)
        ).fetchone()["text"]
    store = repo._runtime.embedding_store
    vector = FakeEmbedder(dim=DIM).embed_query("x")
    assert store.replace_chunk_vectors_if_text(
        nb, [(chunk_id, text + " (edited)", vector), ("ck-gone", "t", vector)],
        created_at=_NOW,
    ) == 0
    assert chunk_id not in vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert store.replace_chunk_vectors_if_text(
        nb, [(chunk_id, text, vector)], created_at=_NOW,
    ) == 1
    assert chunk_id in vector_ids(repo, backend, nb)["chunk_embeddings"]


def check_blank_chunks_are_never_a_gap(repo, backend: Backend) -> None:
    """A blank chunk is not counted by checkup H4 and not returned by any discovery,
    so the repair cannot leave H4 at 1 forever."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    src = f"src-doc-{nb}"
    mnt = repo.maintenance
    assert f"ck-{src}-blank" not in vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert mnt.count_missing_chunk_vectors(nb) == 0
    assert mnt.missing_chunk_vector_source_ids(nb) == []
    assert mnt.missing_chunk_embedding_ids(nb, only_source_id=src) == []
    assert mnt.missing_chunk_embedding_page(nb) == []


def check_orphan_knowhow_sources_are_not_counted(repo, backend: Backend) -> None:
    """A Knowhow hidden source no table owns (left by a crash between deleting a table
    and its projection) is unreachable: H4 does not count it and nothing embeds it. A
    live table's chunk without a vector is counted."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    with repo._runtime.database.write() as db:
        backend.insert(db, "sources", {
            "id": f"src-orphan-{nb}", "notebook_id": nb, "title": "orphan",
            "source_type": "knowhow", "status": "active", "parse_status": "parsed",
            "created_at": _NOW, "updated_at": _NOW,
        })
        backend.insert(db, "chunks", {
            "id": f"chunk-kh-orphan-{nb}", "notebook_id": nb,
            "source_id": f"src-orphan-{nb}", "text": "orphaned knowhow text",
            "section_path": "", "element_ids": [], "created_at": _NOW,
        })
    mnt = repo.maintenance
    assert mnt.count_missing_chunk_vectors(nb) == 0
    live = sorted(knowhow_chunk_ids(repo, backend, world))[0]
    delete_chunk_vectors(repo, backend, [live])
    assert mnt.count_missing_chunk_vectors(nb) == 1

    status = NotebookReembedService(repo).run(nb, legs=["chunks"])

    assert status.state == "succeeded", status
    assert mnt.count_missing_chunk_vectors(nb) == 0
    assert f"chunk-kh-orphan-{nb}" not in vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert "orphaned knowhow text" not in world.embedders["knowhow_embedding"].texts


def check_one_failing_knowhow_table_does_not_stop_the_others(
    repo, backend: Backend, monkeypatch
) -> None:
    """A table whose fill raises is recorded on the leg; the other tables still fill."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    second = add_knowhow_table(repo, nb, "second", ["knowhow cell: second table"])
    delete_vectors(repo, backend, nb, tables=("chunk_embeddings",))
    mnt = repo.maintenance
    real = mnt.missing_knowhow_chunk_embedding_ids

    def _first_table_fails(notebook_id, *, source_id):
        if source_id == world.knowhow_source_id:
            raise RuntimeError("table read failed")
        return real(notebook_id, source_id=source_id)

    monkeypatch.setattr(mnt, "missing_knowhow_chunk_embedding_ids", _first_table_fails)
    status = NotebookReembedService(repo).run(nb, legs=["chunks"])

    leg = status.progress["chunks"]
    assert (leg.state, leg.detail) == ("failed", "RuntimeError"), status
    have = vector_ids(repo, backend, nb)["chunk_embeddings"]
    with repo._runtime.database.connect() as db:
        second_chunks = {
            str(r["id"]) for r in db.execute(
                _sql(backend, "SELECT id FROM chunks WHERE source_id=?"), (second,)
            ).fetchall()
        }
    assert second_chunks and second_chunks <= have
    assert not (knowhow_chunk_ids(repo, backend, world) & have)


def check_offline_embed_messages(repo, backend: Backend, capsys) -> None:
    """``embed --missing-only`` names an unbound half instead of claiming nothing was
    missing; "nothing missing" only when both halves ran."""
    nb = build_world(repo, backend).notebook_id
    original = repo.configured
    try:
        repo.configured = lambda w: w != "chunk_embedding" and original(w)
        batch_ingest.backfill_chunk_embeddings(repo, nb, missing_only=True)
        out = capsys.readouterr().out
        assert "未绑定 chunk_embedding" in out and "无缺失" not in out
        repo.configured = lambda w: w != "knowhow_embedding" and original(w)
        batch_ingest.backfill_chunk_embeddings(repo, nb, missing_only=True)
        out = capsys.readouterr().out
        assert "未绑定 knowhow_embedding" in out and "无缺失" not in out
    finally:
        repo.configured = original
    batch_ingest.backfill_chunk_embeddings(repo, nb, missing_only=True)
    out = capsys.readouterr().out
    assert "无缺失 chunk 向量" in out and "未绑定" not in out


def check_an_embedder_that_writes_nothing_is_never_done(
    repo, backend: Backend, monkeypatch
) -> None:
    """The object leg counts what it asked for independently of what the embedder
    wrote: an embedder that writes nothing leaves the leg incomplete, never done."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)["knowledge_embeddings"]
    delete_vectors(repo, backend, nb, tables=("knowledge_embeddings",))
    monkeypatch.setattr(
        repo._runtime.source_embedding, "embed_objects_batch",
        lambda nb_, items, **_kwargs: 0,
    )
    status = NotebookReembedService(repo).run(nb, legs=["kg_objects"])
    leg = status.progress["kg_objects"]
    assert (leg.state, leg.embedded, leg.remaining) == ("incomplete", 0, len(before))
    assert world.embedders["knowledge_object_embedding"].calls == []


def _stored_vector(repo, backend: Backend, table: str, column: str, row_id: str):
    from app.domain.vector_index import decode_vector

    with repo._runtime.database.connect() as db:
        row = db.execute(
            _sql(backend, f"SELECT vector FROM {table} WHERE {column}=?"), (row_id,)
        ).fetchone()
    return None if row is None else [float(x) for x in decode_vector(row["vector"])]


def _same_vector(stored, expected) -> bool:
    """Stored vectors are float32; compare at that precision."""
    return stored is not None and len(stored) == len(expected) and all(
        abs(a - float(b)) < 1e-6 for a, b in zip(stored, expected)
    )


def _during_first_call(embedder: RecordingEmbedder, action: Callable[[], None]) -> None:
    inner = embedder.embed_texts

    def _wrapped(texts):
        vectors = inner(texts)
        if len(embedder.calls) == 1:
            action()  # after the model answered, before the vectors are written
        return vectors

    embedder.embed_texts = _wrapped


def check_object_backfill_never_overwrites_a_newer_vector(repo, backend: Backend) -> None:
    """While the object leg is embedding, an edit saves a new payload with its own
    vector, and another writer stores a vector for a second object: both of those
    vectors survive (the backfill writes only where the payload is unchanged and no
    vector exists); every other missing object is still written."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)["knowledge_embeddings"]
    delete_vectors(repo, backend, nb, tables=("knowledge_embeddings",))
    edited, raced = f"ko-{nb}-controller", f"ko-{nb}-firmware"
    pending = f"ko-{nb}-bootloader"  # edited, its own re-embed not landed yet
    newer = FakeEmbedder(dim=DIM).embed_query("edited payload")
    other = FakeEmbedder(dim=DIM).embed_query("another writer")
    store = repo._runtime.embedding_store

    def _edit_and_race() -> None:
        with repo._runtime.database.write() as db:
            for object_id, name in ((edited, "controller (edited)"), (pending, "bootloader v2")):
                db.execute(
                    _sql(backend, "UPDATE knowledge_objects SET payload=? WHERE id=?"),
                    (backend.json_param({"name": name}), object_id),
                )
        store.replace_knowledge_vectors(nb, [(edited, newer), (raced, other)], created_at=_NOW)

    _during_first_call(world.embedders["knowledge_object_embedding"], _edit_and_race)
    status = NotebookReembedService(repo).run(nb, legs=["kg_objects"])

    assert _same_vector(
        _stored_vector(repo, backend, "knowledge_embeddings", "object_id", edited), newer
    )
    assert _same_vector(
        _stored_vector(repo, backend, "knowledge_embeddings", "object_id", raced), other
    )
    # The object edited mid-call gets no vector from its old payload: it stays missing
    # for the edit's own re-embed (or the next run) to fill.
    assert vector_ids(repo, backend, nb)["knowledge_embeddings"] == before - {pending}
    assert status.progress["kg_objects"].embedded == len(before) - 3
    assert status.progress["kg_objects"].remaining == 1  # only the one really missing


def check_a_vector_another_writer_filled_is_not_remaining(repo, backend: Backend) -> None:
    """A row another writer fills while a leg is embedding is skipped by the
    conditional write and is not "remaining": the leg is done, the run succeeds, and
    ``embedded`` counts only what this run wrote -- for objects, relations and Knowhow
    chunks. A genuinely failed batch still leaves the leg incomplete."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    store = repo._runtime.embedding_store
    vec = FakeEmbedder(dim=DIM).embed_query("another writer")
    before = vector_ids(repo, backend, nb)
    delete_vectors(repo, backend, nb, tables=("knowledge_embeddings", "relation_embeddings"))
    knowhow = sorted(knowhow_chunk_ids(repo, backend, world))
    delete_chunk_vectors(repo, backend, knowhow)
    obj, rel, chunk = f"ko-{nb}-firmware", f"rel-{nb}-firmware-controller", knowhow[0]

    def _projection_re_embeds_chunk() -> None:
        with repo._runtime.database.write() as db:
            db.execute(
                _sql(backend, "UPDATE chunks SET text=? WHERE id=?"),
                ("knowhow cell: rewritten by a projection", chunk),
            )
        store.replace_chunk_vectors(nb, [(chunk, vec)], created_at=_NOW)

    _during_first_call(
        world.embedders["knowledge_object_embedding"],
        lambda: store.replace_knowledge_vectors(nb, [(obj, vec)], created_at=_NOW),
    )
    _during_first_call(
        world.embedders["relation_embedding"],
        lambda: store.replace_relation_vectors(nb, [(rel, vec)], created_at=_NOW),
    )
    _during_first_call(world.embedders["knowhow_embedding"], _projection_re_embeds_chunk)
    status = NotebookReembedService(repo).run(nb, legs=["chunks", "kg_objects", "kg_relations"])

    assert status.state == "succeeded", status
    for leg, total in (
        ("kg_objects", len(before["knowledge_embeddings"])),
        ("kg_relations", len(before["relation_embeddings"])),
        ("chunks", len(knowhow)),
    ):
        progress = status.progress[leg]
        assert (progress.state, progress.remaining) == ("done", 0), (leg, progress)
        assert progress.embedded == total - 1, (leg, progress)

    # A batch that really fails is still reported.
    delete_vectors(repo, backend, nb, tables=("knowledge_embeddings",))
    reset_calls(world)
    world.embedders["knowledge_object_embedding"].fail_calls = {1}
    failed = NotebookReembedService(repo).run(nb, legs=["kg_objects"])
    leg = failed.progress["kg_objects"]
    assert leg.state == "incomplete" and leg.remaining > 0, failed
    assert failed.state == "failed"


def check_relation_backfill_never_overwrites_or_mismatches(repo, backend: Backend) -> None:
    """While the relation leg is embedding, another writer stores a vector for one
    relation and a rebuild changes another relation's evidence: the stored vector
    survives, the changed relation gets no vector from its old text, and the rest
    are written."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)["relation_embeddings"]
    delete_vectors(repo, backend, nb, tables=("relation_embeddings",))
    raced = f"rel-{nb}-firmware-controller"
    changed = f"rel-{nb}-bootloader-firmware"
    other = FakeEmbedder(dim=DIM).embed_query("another writer")
    store = repo._runtime.embedding_store

    def _race() -> None:
        store.replace_relation_vectors(nb, [(raced, other)], created_at=_NOW)
        with repo._runtime.database.write() as db:
            db.execute(
                _sql(backend, "UPDATE knowledge_relations SET evidence=? WHERE id=?"),
                (backend.json_param([{"quoted_span": "rebuilt"}]), changed),
            )

    _during_first_call(world.embedders["relation_embedding"], _race)
    NotebookReembedService(repo).run(nb, legs=["kg_relations"])

    assert _same_vector(
        _stored_vector(repo, backend, "relation_embeddings", "relation_id", raced), other
    )
    have = vector_ids(repo, backend, nb)["relation_embeddings"]
    assert changed not in have
    assert have == before - {changed}


def _set_pipeline_pending(repo, backend: Backend, notebook_id: str) -> None:
    with repo._runtime.database.write() as db:
        db.execute(
            _sql(backend, "UPDATE notebooks SET indexing_pipeline_job_id=? WHERE id=?"),
            ("pending:generation", notebook_id),
        )


def check_pipeline_switch_while_waiting_for_a_table_lock(
    repo, backend: Backend, monkeypatch
) -> None:
    """A pipeline switch that lands while the Knowhow fill waits for a table's lock
    aborts the pass once the lock is held: nothing is written, the run fails, the
    guard is released."""
    from contextlib import contextmanager

    world = build_world(repo, backend)
    nb = world.notebook_id
    chunks = knowhow_chunk_ids(repo, backend, world)
    delete_chunk_vectors(repo, backend, chunks)
    database = repo._runtime.database
    real_lock = database.table_projection_lock

    @contextmanager
    def _switch_while_waiting(table_id):
        _set_pipeline_pending(repo, backend, nb)  # lands while this one waits
        with real_lock(table_id):
            yield

    monkeypatch.setattr(database, "table_projection_lock", _switch_while_waiting)
    with pytest.raises(IndexingPipelineUnavailableError):
        NotebookReembedService(repo).run(nb, legs=["chunks"])

    assert world.embedders["knowhow_embedding"].calls == []
    assert not (chunks & vector_ids(repo, backend, nb)["chunk_embeddings"])
    assert nb not in notebook_reembed._active_runs
    status = [r for r in notebook_reembed._runs.values() if r.notebook_id == nb][-1]
    assert (status.state, status.error) == ("failed", "IndexingPipelineUnavailableError")


def check_pipeline_switch_between_the_halves(repo, backend: Backend, monkeypatch) -> None:
    """A switch that lands after the document half aborts the pass before the Knowhow
    half writes anything; the document vectors written before it stay."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    chunks = knowhow_chunk_ids(repo, backend, world)
    delete_vectors(repo, backend, nb, tables=("chunk_embeddings",))
    mnt = repo.maintenance
    real = mnt.embed_chunks_batch

    def _switch_after(notebook_id, items):
        written = real(notebook_id, items)
        _set_pipeline_pending(repo, backend, nb)
        return written

    monkeypatch.setattr(mnt, "embed_chunks_batch", _switch_after)
    tables_listed: list = []
    real_list = repo.list_knowhow_tables
    monkeypatch.setattr(
        repo, "list_knowhow_tables",
        lambda notebook_id: tables_listed.append(notebook_id) or real_list(notebook_id),
    )
    with pytest.raises(IndexingPipelineUnavailableError):
        NotebookReembedService(repo).run(nb, legs=["chunks"])
    assert tables_listed == []  # refused before the Knowhow half even started

    have = vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert {f"ck-src-doc-{nb}-{i:03d}" for i in range(DOC_CHUNKS)} <= have
    assert not (chunks & have)
    assert world.embedders["knowhow_embedding"].calls == []
    assert nb not in notebook_reembed._active_runs
