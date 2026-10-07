# backend/tests/test_memory_chunk_guard.py
"""E4-1: the chunk store's write methods and the chunking service refuse Memory.

Passages (chunks) are a SHARED index -- every member of a notebook retrieves
from them -- while Memory is private per user and is projected into a hidden
synthetic source (``sources.source_type = 'memory'``). Until this guard the
privacy of the passage channel rested on an ingestion convention ("Memory
sources are never chunked") with nothing enforcing it.

What this file pins is exactly two things, each by its own tests (a mutation
that removes one layer must turn only that layer's tests red). The other
writers of the ``chunks`` table are pinned elsewhere:
``test_memory_chunk_write_guard.py`` enumerates every write site; the KG build
publish, the Knowhow transfer insert and the sync import refuse Memory sources
(behaviour tests in ``test_memory_chunk_write_refusals.py``); the mirror /
existing-rows-only paths are on the guard's reviewed list with their reason; and
the notebook copy path (``insert_copy_rows("chunks")``) refuses nothing: which
chunk rows it copies is decided by the ``chunks`` query of the copy statement set
used for a notebook that holds a Memory source (``_COPY_SNAPSHOT_QUERIES`` when it
is the only set; ``_MEMORY_COPY_SNAPSHOT_QUERIES`` where ``_copy_queries`` chooses
per copy, task E5-1), and the guard reports the path as guarded exactly when that
query carries a ``memory_sql`` Memory-derived predicate on both backends. This
file pins:

- service: ``SourceChunkingService.build_chunks_for_source`` returns without
  writing anything, and emits a content-free ``memory_chunk_write_refused``
  event, when the source is a Memory source;
- store (last line of defence): ``ChunkStore.replace_source_chunks`` and
  ``ChunkStore.insert_rows`` raise ``ValueError("memory sources are not
  chunked")`` before touching any row, on both backends.

The scenario functions below take a repository object and are backend-neutral;
this file drives them against SQLite and
``tests/postgres/test_memory_chunk_guard_pg.py`` drives the same functions
against PostgreSQL (the PG lane only collects ``tests/postgres``).
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from app.services import sqlite_repository
from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients

REFUSAL = "memory sources are not chunked"
NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = sqlite_repository.SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


# ----------------------------------------------------------------- helpers
def _sql(repo, statement: str) -> str:
    """Write statements with ``?``; PostgreSQL wants ``%s``."""
    if repo._runtime.chunk_store.database.__class__.__name__.startswith("Postgres"):
        return statement.replace("?", "%s")
    return statement


def _one(repo, statement: str, params=()):
    with repo._runtime.chunk_store.database.connect() as db:
        return db.execute(_sql(repo, statement), params).fetchone()


def _count(repo, table: str, source_id: str) -> int:
    column = "source_id"
    if table == "chunk_embeddings":
        return int(
            _one(
                repo,
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE source_id=?)",
                (source_id,),
            )["n"]
        )
    return int(
        _one(repo, f"SELECT COUNT(*) AS n FROM {table} WHERE {column}=?", (source_id,))["n"]
    )


def _chunked_at(repo, source_id: str):
    return _one(repo, "SELECT chunked_at AS c FROM sources WHERE id=?", (source_id,))["c"]


def seed_source(repo, source_type: str, texts=("alpha paragraph", "beta paragraph")):
    """A sources row of the given type with elements; returns (notebook_id, source_id)."""
    notebook_id = repo.create_notebook(NotebookCreate(name="guard")).id
    source_id = f"src-{uuid.uuid4().hex[:8]}"
    sources = repo._runtime.source_store
    sources.insert_source(
        source_id=source_id,
        notebook_id=notebook_id,
        title="S",
        source_type=source_type,
        status="extracted",
        parse_status="extracted",
        file_name="s.md",
        file_path="/tmp/s.md",
        file_size=0,
        file_hash="h",
        summary="",
        doc_type="",
    )
    with repo._runtime.chunk_store.database.write() as db:
        sources.replace_elements(
            db,
            source_id,
            [
                SourceElementWrite(f"el-{source_id}-{i}", "paragraph", f"p{i}", text, {})
                for i, text in enumerate(texts, 1)
            ],
            created_at=NOW,
        )
    return notebook_id, source_id


def _writes(source_id: str, n: int = 2) -> list[ChunkWrite]:
    return [
        ChunkWrite(
            id=f"ck-{source_id}-{i}",
            text=f"chunk text {i}",
            section_path="",
            element_ids=(f"el-{source_id}-1",),
        )
        for i in range(n)
    ]


def plant_memory_chunks(repo, notebook_id: str, source_id: str, n: int = 2) -> None:
    """Simulate a notebook that ALREADY holds chunks of a Memory source (it
    should not exist -- the guard forbids it -- so the rows go in directly)."""
    with repo._runtime.chunk_store.database.write() as db:
        for i in range(n):
            db.execute(
                _sql(
                    repo,
                    "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                    "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                ),
                (f"ck-planted-{source_id}-{i}", notebook_id, source_id,
                 f"planted {i}", "", "[]", NOW),
            )


class _Events:
    def __init__(self):
        self.items: list[dict] = []

    def emit(self, event, **_kw):
        self.items.append(dict(event))


def _spy_service(repo):
    """Recording event log + dirty seat on the live chunking service."""
    service = repo._runtime.source_chunking
    events = _Events()
    dirty: list[str] = []
    service.event_log = events
    service.mark_unified_dirty = dirty.append
    return service, events, dirty


# --------------------------------------------------------------- scenarios
def scenario_store_replace_refuses_memory(repo) -> None:
    chunks = repo._runtime.chunk_store
    notebook_id, source_id = seed_source(repo, "memory")
    with pytest.raises(ValueError, match=REFUSAL):
        chunks.replace_source_chunks(
            source_id, notebook_id, _writes(source_id), created_at=NOW, mark_chunked_at=NOW
        )
    # An empty replacement is a wipe-and-rewrite of the same write path: refused too.
    with pytest.raises(ValueError, match=REFUSAL):
        chunks.replace_source_chunks(source_id, notebook_id, [], created_at=NOW)
    assert _count(repo, "chunks", source_id) == 0
    assert _chunked_at(repo, source_id) is None


def scenario_store_insert_rows_refuses_memory(repo) -> None:
    chunks = repo._runtime.chunk_store
    notebook_id, source_id = seed_source(repo, "memory")
    with pytest.raises(ValueError, match=REFUSAL):
        with chunks.database.write() as db:
            chunks.insert_rows(db, notebook_id, source_id, _writes(source_id), created_at=NOW)
    assert _count(repo, "chunks", source_id) == 0
    assert int(
        _one(repo, "SELECT COUNT(*) AS n FROM chunk_elements WHERE notebook_id=?",
             (notebook_id,))["n"]
    ) == 0


def scenario_store_refusal_leaves_planted_rows_untouched(repo) -> None:
    """A notebook that already holds Memory chunks: a refused write neither
    adds to nor wipes them. Removing pre-existing chunks under a Memory source
    that still exists is the job of the schema migration that ships in this same
    PR; the delete paths only remove them when the source itself is deleted
    (foreign-key cascade)."""
    chunks = repo._runtime.chunk_store
    notebook_id, source_id = seed_source(repo, "memory")
    plant_memory_chunks(repo, notebook_id, source_id, 2)
    with pytest.raises(ValueError, match=REFUSAL):
        chunks.replace_source_chunks(source_id, notebook_id, _writes(source_id), created_at=NOW)
    with pytest.raises(ValueError, match=REFUSAL):
        with chunks.database.write() as db:
            chunks.insert_rows(db, notebook_id, source_id, _writes(source_id), created_at=NOW)
    assert _count(repo, "chunks", source_id) == 2


def scenario_store_still_writes_ordinary_and_knowhow(repo) -> None:
    chunks = repo._runtime.chunk_store
    for source_type in ("document", "knowhow"):
        notebook_id, source_id = seed_source(repo, source_type)
        chunks.replace_source_chunks(
            source_id, notebook_id, _writes(source_id, 2), created_at=NOW, mark_chunked_at=NOW
        )
        assert _count(repo, "chunks", source_id) == 2
        assert _chunked_at(repo, source_id) is not None
        extra = [ChunkWrite("ck-extra-" + source_id, "extra text", "", (f"el-{source_id}-2",))]
        with chunks.database.write() as db:
            chunks.insert_rows(db, notebook_id, source_id, extra, created_at=NOW)
        rows = _one(
            repo,
            "SELECT text AS t, section_path AS s FROM chunks WHERE id=?",
            ("ck-extra-" + source_id,),
        )
        assert (rows["t"], rows["s"]) == ("extra text", "")
        assert _count(repo, "chunks", source_id) == 3


def scenario_store_probe_is_once_per_source_per_call(repo) -> None:
    """The guard is one probe per write call, not one per chunk row."""
    store = repo._runtime.chunk_store
    cls = type(store)
    saved = cls.__dict__["_refuse_memory_source"]  # the staticmethod object itself
    original = cls._refuse_memory_source
    probes: list[str] = []

    def counting(connection, source_id):
        probes.append(source_id)
        return original(connection, source_id)

    cls._refuse_memory_source = staticmethod(counting)
    try:
        notebook_id, source_id = seed_source(repo, "document")
        store.replace_source_chunks(source_id, notebook_id, _writes(source_id, 1), created_at=NOW)
        assert probes == [source_id]
        probes.clear()
        store.replace_source_chunks(source_id, notebook_id, _writes(source_id, 60), created_at=NOW)
        assert probes == [source_id]
        probes.clear()
        with store.database.write() as db:
            store.insert_rows(
                db, notebook_id, source_id,
                [ChunkWrite(f"ck-more-{i}", "t", "", (f"el-{source_id}-1",)) for i in range(60)],
                created_at=NOW,
            )
        assert probes == [source_id]
    finally:
        cls._refuse_memory_source = saved


class _RecordingConnection:
    """Delegating proxy over a write connection that records every statement it
    runs (``execute`` / ``executemany``, also through a ``cursor()``), with
    sqlite's ``in_transaction`` at that moment. Anything a store issues on some
    OTHER connection never shows up in its log."""

    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def _record(self, statement):
        self._log.append((str(statement), getattr(self._inner, "in_transaction", None)))

    def execute(self, statement, *args, **kwargs):
        self._record(statement)
        return self._inner.execute(statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        self._record(statement)
        return self._inner.executemany(statement, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._inner.cursor(*args, **kwargs), self._record)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RecordingCursor:
    def __init__(self, inner, record):
        self._inner = inner
        self._record = record

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def execute(self, statement, *args, **kwargs):
        self._record(statement)
        return self._inner.execute(statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        self._record(statement)
        return self._inner.executemany(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _is_probe(statement: str) -> bool:
    return "FROM sources" in statement and "source_type" in statement


def _assert_probe_precedes_write(log, write_marker: str) -> None:
    probe = [i for i, (stmt, _tx) in enumerate(log) if _is_probe(stmt)]
    write = [i for i, (stmt, _tx) in enumerate(log) if write_marker in stmt]
    assert len(probe) == 1, log  # issued on the write's own connection, once
    assert write and probe[0] < write[0], log
    in_tx = log[probe[0]][1]
    if in_tx is not None:  # sqlite: inside the open transaction, not autocommit
        assert in_tx is True, log


def scenario_store_probe_shares_the_writes_connection_and_transaction(repo) -> None:
    """The probe runs on the same connection object -- and, on SQLite, inside the
    already-open transaction -- as the DELETE/INSERT it guards."""
    store = repo._runtime.chunk_store
    notebook_id, source_id = seed_source(repo, "document")
    log: list = []
    original_write = store.database.write

    from contextlib import contextmanager

    @contextmanager
    def recording_write(*args, **kwargs):
        with original_write(*args, **kwargs) as raw:
            yield _RecordingConnection(raw, log)

    store.database.write = recording_write
    try:
        store.replace_source_chunks(source_id, notebook_id, _writes(source_id), created_at=NOW)
    finally:
        store.database.write = original_write
    _assert_probe_precedes_write(log, "DELETE FROM chunks")
    if log[0][1] is not None:
        assert log[0][0].upper().startswith("BEGIN"), log  # sqlite opens the tx first

    # insert_rows: the transaction belongs to the caller; the probe must run on
    # the caller's connection.
    log.clear()
    with original_write() as raw:
        if hasattr(raw, "in_transaction"):
            store.database.begin_immediate(raw)
        store.insert_rows(
            _RecordingConnection(raw, log), notebook_id, source_id,
            [ChunkWrite("ck-shared-conn", "t", "", (f"el-{source_id}-1",))], created_at=NOW,
        )
    _assert_probe_precedes_write(log, "INSERT INTO chunks")


def scenario_store_guard_matches_memory_exactly(repo) -> None:
    """Only the exact type 'memory' is refused: 'memo', 'memory_x' and a
    different letter case are ordinary source types and chunk normally (pins the
    predicate against LIKE 'mem%' and case-insensitive matching)."""
    chunks = repo._runtime.chunk_store
    service = repo._runtime.source_chunking
    for source_type in ("memo", "memory_x", "Memory", "MEMORY"):
        notebook_id, source_id = seed_source(repo, source_type)
        chunks.replace_source_chunks(
            source_id, notebook_id, _writes(source_id, 2), created_at=NOW, mark_chunked_at=NOW
        )
        assert _count(repo, "chunks", source_id) == 2, source_type
        with chunks.database.write() as db:
            chunks.insert_rows(
                db, notebook_id, source_id,
                [ChunkWrite("ck-x-" + source_id, "t", "", (f"el-{source_id}-1",))], created_at=NOW,
            )
        assert _count(repo, "chunks", source_id) == 3, source_type
        _nb, other_id = seed_source(repo, source_type, texts=("gamma paragraph",))
        service.build_chunks_for_source(other_id)
        assert _count(repo, "chunks", other_id) >= 1, source_type
        assert _chunked_at(repo, other_id) is not None, source_type


def scenario_memory_confirmation_ingests_without_chunks(repo, monkeypatch) -> None:
    """The real Memory projection path (ingest_memory_source) never tries to
    chunk, so the store guard never fires on it."""
    notebook_id = repo.create_notebook(NotebookCreate(name="nb")).id
    ingestion = repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "run_extraction", lambda sid: None)
    source_id = ingestion.ingest_memory_source(notebook_id, "memory-1", "标题", "正文")
    assert source_id is not None
    assert repo._runtime.source_store.get_source(source_id).parse_status == "extracted"
    assert _count(repo, "chunks", source_id) == 0


def scenario_service_skips_memory_and_emits(repo) -> None:
    service, events, dirty = _spy_service(repo)
    notebook_id, source_id = seed_source(repo, "memory", texts=("private secret sentence",))
    warning = service.build_chunks_for_source(source_id)
    assert warning == ""
    assert _count(repo, "chunks", source_id) == 0
    assert _chunked_at(repo, source_id) is None
    assert dirty == []  # nothing changed, nothing to invalidate
    assert len(events.items) == 1
    event = events.items[0]
    assert event == {
        "kind": "memory_chunk_write_refused",
        "notebook_id": notebook_id,
        "source_id": source_id,
        "stage": "chunking",
        "status": "refused",
        "reason": "memory_source",
    }
    assert "private secret sentence" not in json.dumps(event)


def scenario_service_entrypoints_write_nothing(repo) -> None:
    """facade `_build_chunks_for_source` / `_chunk_and_embed_source` and the
    maintenance adapter's `chunk_and_embed_source` all reach the service guard."""
    _service, events, _dirty = _spy_service(repo)
    _nb, source_id = seed_source(repo, "memory")
    repo._build_chunks_for_source(source_id)
    repo._chunk_and_embed_source(source_id)
    repo.maintenance.chunk_and_embed_source(source_id)
    assert _count(repo, "chunks", source_id) == 0
    assert _count(repo, "chunk_embeddings", source_id) == 0
    assert _chunked_at(repo, source_id) is None
    assert len(events.items) == 3


def scenario_service_leaves_planted_rows_untouched(repo) -> None:
    service, _events, _dirty = _spy_service(repo)
    notebook_id, source_id = seed_source(repo, "memory")
    plant_memory_chunks(repo, notebook_id, source_id, 2)
    assert service.build_chunks_for_source(source_id) == ""
    assert _count(repo, "chunks", source_id) == 2


def scenario_service_still_chunks_ordinary_source(repo) -> None:
    service, events, dirty = _spy_service(repo)
    notebook_id, source_id = seed_source(repo, "document", texts=("first paragraph", "second"))
    assert service.build_chunks_for_source(source_id) == ""
    assert _count(repo, "chunks", source_id) >= 1
    assert _chunked_at(repo, source_id) is not None
    assert dirty == [notebook_id]
    assert events.items == []  # the refusal event is for Memory sources only
    stored = _one(repo, "SELECT text AS t FROM chunks WHERE source_id=?", (source_id,))["t"]
    assert "first paragraph" in stored and "second" in stored


# ------------------------------------------------------------------ SQLite
def test_store_replace_refuses_memory(repo):
    scenario_store_replace_refuses_memory(repo)


def test_store_insert_rows_refuses_memory(repo):
    scenario_store_insert_rows_refuses_memory(repo)


def test_store_refusal_leaves_planted_rows_untouched(repo):
    scenario_store_refusal_leaves_planted_rows_untouched(repo)


def test_store_still_writes_ordinary_and_knowhow(repo):
    scenario_store_still_writes_ordinary_and_knowhow(repo)


def test_store_probe_is_once_per_source_per_call(repo):
    scenario_store_probe_is_once_per_source_per_call(repo)


def test_service_skips_memory_and_emits(repo):
    scenario_service_skips_memory_and_emits(repo)


def test_service_entrypoints_write_nothing(repo):
    scenario_service_entrypoints_write_nothing(repo)


def test_service_leaves_planted_rows_untouched(repo):
    scenario_service_leaves_planted_rows_untouched(repo)


def test_service_still_chunks_ordinary_source(repo):
    scenario_service_still_chunks_ordinary_source(repo)


def test_store_probe_shares_the_writes_connection_and_transaction(repo):
    scenario_store_probe_shares_the_writes_connection_and_transaction(repo)


def test_store_guard_matches_memory_exactly(repo):
    scenario_store_guard_matches_memory_exactly(repo)


def test_memory_confirmation_still_ingests_without_chunks(repo, monkeypatch):
    scenario_memory_confirmation_ingests_without_chunks(repo, monkeypatch)
