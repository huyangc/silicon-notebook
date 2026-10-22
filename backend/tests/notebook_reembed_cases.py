"""Notebook re-embed job behaviour (``app.services.notebook_reembed``) -- **both backends
run the same checks on the same world**: ``tests/test_notebook_reembed.py`` (SQLite) and
``tests/postgres/test_notebook_reembed_pg.py`` (PostgreSQL) each supply only an ``insert``
adapter, a placeholder style and a repository.

The world has every kind of vector: a document source with chunks and elements, three
document knowledge objects with two relations, and one Knowhow table (its projection adds
a hidden source with its own chunks, objects and edges). Each embedding workload is bound
to its own recording fake, so a check can tell which workload embedded what.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Callable

from app.models.schemas import NotebookCreate
from app.services import notebook_reembed
from app.services.embedding import FakeEmbedder
from app.services.knowhow import api as knowhow_api
from app.services.model_work import ModelProviderError
from app.services.notebook_reembed import LEGS, NotebookReembedService
from tests.model_testkit import bind_embedding_client

DIM = 16
BATCH = 2
ENV = {"EMBED_DIM": str(DIM), "EMBED_BATCH_SIZE": str(BATCH)}
"""Settings every world needs (set before the repository is built)."""

_NOW = "2026-01-01T00:00:00+00:00"
DOC_CHUNKS = 5
DOC_ELEMENTS = 5
KNOWHOW_TEXT = "knowhow cell: reboot the controller before reflashing"

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
        names = ("controller", "firmware", "bootloader")
        for name in names:
            backend.insert(db, "knowledge_objects", {
                "id": f"ko-{nb}-{name}", "notebook_id": nb, "object_type": "concept",
                "status": "approved", "payload": {"name": name}, "evidence": [],
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
        ],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    repo.add_knowhow_row(
        table_id, {columns["symptom"]: "controller hangs", columns["fix"]: KNOWHOW_TEXT},
    )
    knowhow_api.build_projector(repo).project_table(table_id)
    hidden = repo.get_knowhow_table(table_id)["hidden_source_id"]
    world = World(nb, table_id, hidden, embedders)
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


# --------------------------------------------------------------------------- checks
def check_every_vector_comes_back_once(repo, backend: Backend) -> None:
    """Delete all four vector tables, run every leg: every vector is back, each workload
    is called once per batch (not once per row), the Knowhow chunks are embedded by
    ``knowhow_embedding`` and never by ``chunk_embedding``, and a second run makes no
    model call at all."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    before = vector_ids(repo, backend, nb)
    knowhow_chunks = knowhow_chunk_ids(repo, backend, world)
    assert knowhow_chunks and knowhow_chunks <= before["chunk_embeddings"]
    assert all(before.values()), before
    delete_vectors(repo, backend, nb)

    status = NotebookReembedService(repo).run(nb, legs=LEGS)

    assert status.state == "succeeded", status
    assert vector_ids(repo, backend, nb) == before
    for leg in LEGS:
        assert status.progress[leg].state == "done", status
        assert status.progress[leg].remaining == 0, status
    doc_chunks = len(before["chunk_embeddings"]) - len(knowhow_chunks)
    assert status.progress["chunks"].embedded == len(before["chunk_embeddings"])
    assert status.progress["elements"].embedded == len(before["element_embeddings"])
    assert status.progress["kg_objects"].embedded == len(before["knowledge_embeddings"])
    assert status.progress["kg_relations"].embedded == len(before["relation_embeddings"])
    e = world.embedders
    assert len(e["chunk_embedding"].calls) == _calls_for(doc_chunks)
    assert len(e["source_element_embedding"].calls) == _calls_for(
        len(before["element_embeddings"]))
    assert len(e["knowledge_object_embedding"].calls) == _calls_for(
        len(before["knowledge_embeddings"]))
    assert len(e["relation_embedding"].calls) == _calls_for(
        len(before["relation_embeddings"]))
    assert len(e["chunk_embedding"].calls) < doc_chunks  # batched, not one call per row
    assert KNOWHOW_TEXT in e["knowhow_embedding"].texts
    assert all("knowhow cell" not in text for text in e["chunk_embedding"].texts)

    reset_calls(world)
    again = NotebookReembedService(repo).run(nb, legs=LEGS)
    assert again.state == "succeeded", again
    assert {w: len(x.calls) for w, x in e.items()} == {w: 0 for w in e}
    assert all(again.progress[leg].embedded == 0 for leg in LEGS)


def check_backfill_job_keeps_knowhow_chunks_on_their_workload(repo, backend: Backend) -> None:
    """The ``backfill-vectors`` job (chunks + elements) once embedded a Knowhow table's
    chunks with ``chunk_embedding``; now the table is reprojected and its chunk comes
    back through ``knowhow_embedding``."""
    from app.api import source_routes

    world = build_world(repo, backend)
    nb = world.notebook_id
    knowhow_chunks = knowhow_chunk_ids(repo, backend, world)
    with repo._runtime.database.write() as db:
        for chunk_id in knowhow_chunks:
            db.execute(_sql(backend, "DELETE FROM chunk_embeddings WHERE chunk_id=?"), (chunk_id,))

    source_routes._backfill_vectors_job(repo, nb)

    assert knowhow_chunks <= vector_ids(repo, backend, nb)["chunk_embeddings"]
    assert world.embedders["chunk_embedding"].calls == []
    assert KNOWHOW_TEXT in world.embedders["knowhow_embedding"].texts
    assert nb not in notebook_reembed._inflight


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
    raising."""
    world = build_world(repo, backend)
    nb = world.notebook_id
    original = repo.configured
    repo.configured = lambda workload: (
        workload != "knowledge_object_embedding" and original(workload)
    )
    try:
        delete_vectors(repo, backend, nb, tables=("knowledge_embeddings",))
        status = NotebookReembedService(repo).run(nb, legs=["kg_objects", "elements"])
    finally:
        repo.configured = original
    assert status.state == "failed", status
    assert status.progress["kg_objects"] == notebook_reembed.LegProgress(
        state="skipped", detail="not_configured"
    )
    assert status.progress["elements"].state == "done"
    assert status.error == "kg_objects: skipped/not_configured"
    assert world.embedders["knowledge_object_embedding"].calls == []
    assert vector_ids(repo, backend, nb)["knowledge_embeddings"] == set()
