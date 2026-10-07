# backend/tests/test_memory_chunk_write_refusals.py
"""E4-1b: the remaining chunk write paths refuse a Memory source.

E4-1 made the chunk store's two write methods and the chunking service refuse a
Memory source. Three more paths write the shared ``chunks`` table and get the
same refusal here, each pinned by a behaviour test:

- the KG build publish (``kg_build_job_store.publish_indexing_pipeline_success``,
  atomic publish of the staged chunks): one statement scoped to the notebook
  (its Memory sources, by ``idx_sources_nb_hidden_type``) intersected with the
  snapshot the publish already loaded; it raises
  ``IndexingPipelineMemorySourceError`` before the first live mutation, on both
  backends, and the job ends ``failed`` with its own classified reason on both
  publish paths (with and without a KG build);
- the Knowhow table transfer insert (``knowhow_transfer_store.insert_transfer``):
  every chunk row must name the payload's own source, which is probed by primary
  key; a refusal rolls the whole transfer back;
- the sync import (``app.migration.sync.import_``): a package that carries
  passages of a Memory source, or a source id whose type differs between the
  package and the target with one of the two types Memory, is refused for the
  WHOLE run before any table is applied (the target stays byte-identical, a dry
  run refuses too) with a fixed sentence that says what is wrong and what to do,
  the number of offending sources and the first ``_MEMORY_ID_LIMIT`` (20) of
  their ids in sorted order, the rest as "(and N more)". The apply pass repeats
  both checks per batch as a backstop. Every statement of the importer that reads
  ``sources`` is either the one statement ``_source_rows_statement`` builds (one
  per batch of ids, never one per row) or a listed non-Memory read
  (``_StatementCounter``).

The scenario functions take repository objects and are backend-neutral; this
file drives them against SQLite and
``tests/postgres/test_memory_chunk_write_refusals_pg.py`` drives the same
functions against PostgreSQL (the PG lane only collects ``tests/postgres``).
The static side (every write site of ``chunks`` is either refused or on a
reviewed list) is ``test_memory_chunk_write_guard.py``; the probes' plans are
pinned in ``test_memory_chunk_write_sqlite_plans.py`` and
``tests/postgres/test_memory_chunk_write_explain_pins.py``.
"""
from __future__ import annotations

import json
import re
import types
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.knowhow_transfer import CHUNK_SOURCE_MISMATCH as TRANSFER_REASON_MISMATCH
from app.domain.knowhow_transfer import MEMORY_SOURCE as TRANSFER_REASON_MEMORY
from app.domain.knowhow_transfer import KnowhowTransferRefused
from app.migration.sync import database as sync_database
from app.migration.sync.export import export_notebooks
from app.migration.sync.import_ import SyncImportError, import_package
from app.models.schemas import NotebookCreate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_chat_client
from tests.test_indexing_pipeline_chunking import _PipelineHost

REFUSAL = "memory sources are not chunked"
TRANSFER_MISMATCH = "transfer chunk rows must belong to the transferred source"
NOW = "2026-01-01T00:00:00+00:00"
ALICE = ("user-a-alice", "user-b-alice", "alice")


# ----------------------------------------------------------------- helpers
def _is_postgres(repo) -> bool:
    return repo._runtime.chunk_store.database.__class__.__name__.startswith("Postgres")


def _sql(repo, statement: str) -> str:
    return statement.replace("?", "%s") if _is_postgres(repo) else statement


def _rows(repo, statement: str, params=()):
    with repo._runtime.chunk_store.database.connect() as db:
        return db.execute(_sql(repo, statement), params).fetchall()


def _n(repo, statement: str, params=()) -> int:
    return int(_rows(repo, statement, params)[0]["n"])


def _write(repo, statement: str, params=()) -> None:
    with repo._runtime.chunk_store.database.write() as db:
        db.execute(_sql(repo, statement), params)


def seed_source(repo, notebook_id: str, source_type: str, *, chunks: int = 0,
                title: str = "S", elements: bool = True) -> str:
    """A source of the given type in ``notebook_id`` with two elements and, when
    asked, ``chunks`` chunk rows written through the (guarded) store while the
    source is still an ordinary one."""
    source_id = f"src-{uuid.uuid4().hex[:8]}"
    sources = repo._runtime.source_store
    sources.insert_source(
        source_id=source_id, notebook_id=notebook_id, title=title,
        source_type=source_type, status="extracted", parse_status="extracted",
        file_name="s.md", file_path="/tmp/s.md", file_size=0, file_hash="h",
        summary="", doc_type="",
    )
    if elements:
        with repo._runtime.chunk_store.database.write() as db:
            sources.replace_elements(
                db, source_id,
                [SourceElementWrite(f"el-{source_id}-{i}", "paragraph", f"p{i}",
                                    f"paragraph {i} of {source_id}", {})
                 for i in (1, 2)],
                created_at=NOW,
            )
    if chunks:
        repo._runtime.chunk_store.replace_source_chunks(
            source_id, notebook_id,
            [ChunkWrite(id=f"ck-{source_id}-{i}", text=f"text {i}", section_path="",
                        element_ids=(f"el-{source_id}-1",))
             for i in range(chunks)],
            created_at=NOW,
        )
    return source_id


def retype(repo, source_id: str, source_type: str) -> None:
    """Direct SQL type change. Production has no writer of this column after
    insert (that is exactly what the static guard pins); tests use it to plant
    the states the refusals are for."""
    _write(repo, "UPDATE sources SET source_type=? WHERE id=?", (source_type, source_id))


# ============================================================ KG publish
def _stage_chunks(repo, notebook_id: str):
    """Stage a whole-notebook rebuild's chunks the way the pipeline does; returns
    ``(job_id, (pipeline_id, version, generation))``."""
    begun = repo._runtime.indexing_pipeline.begin(notebook_id, "test.pipeline")
    identity = (
        str(begun["_pipeline_id"]), str(begun["_pipeline_version"]),
        str(begun["_pipeline_generation"]),
    )
    job = repo._runtime.knowledge_lifecycle.prepare_notebook_kg_job(
        notebook_id, "rebuild", allow_without_model=True
    )
    assert repo._runtime.notebook_store.attach_indexing_pipeline_job(
        notebook_id, identity[2], job["id"]
    )
    repo._runtime.indexing_pipeline.rebuild(
        notebook_id, job_id=job["id"], pipeline_id=identity[0],
        pipeline_version=identity[1], pipeline_generation=identity[2],
    )
    return str(job["id"]), identity


def _stage_for_publish(repo, notebook_id: str):
    """``_stage_chunks`` plus the no-KG completion ``publish_indexing_pipeline_success``
    needs."""
    job_id, identity = _stage_chunks(repo, notebook_id)
    assert repo._runtime.kg_build_jobs.complete_indexing_pipeline_stage_without_kg(job_id)
    return job_id, identity


class _ProbeClient:
    """Answers the KG build's start-up probe; the run below extracts nothing."""

    configured = True
    model = "test-kg"

    def chat_json(self, messages, _schema_hint, **_kwargs):
        return '{"ok":true}' if messages[0]["content"].startswith('Return {"ok":true}') else '{"objects":[]}'


def _job(repo, job_id: str) -> dict:
    return repo._runtime.kg_build_jobs.get(job_id)


def _stage_rows(repo, job_id: str) -> int:
    return _n(repo, "SELECT COUNT(*) AS n FROM indexing_pipeline_stages WHERE job_id=?", (job_id,))


def scenario_publish_refuses_memory_source(repo, drift_visible_predicate) -> None:
    """The snapshot compare inside the publish already keeps a Memory source out
    (it is not a visible type). ``drift_visible_predicate`` makes the publish's
    own visible-source predicate match every type, i.e. simulates that
    predicate drifting; the publish's probe must then still refuse, before the
    first live mutation, leave the notebook's published chunks alone, and end the
    job with its own classified reason on BOTH publish paths."""
    from app.domain.indexing_pipeline import IndexingPipelineMemorySourceError
    from app.services.knowledge_lifecycle import (
        INDEXING_PIPELINE_MEMORY_SOURCE_CODE,
        INDEXING_PIPELINE_MEMORY_SOURCE_MESSAGE,
    )

    jobs = repo._runtime.kg_build_jobs
    lifecycle = repo._runtime.knowledge_lifecycle
    # control: ordinary sources publish normally even though a Memory source lives
    # in the SAME notebook (it is not in the snapshot) and in another notebook
    other_nb = repo.create_notebook(NotebookCreate(name="other")).id
    seed_source(repo, other_nb, "memory")
    control_nb = repo.create_notebook(NotebookCreate(name="control")).id
    control = seed_source(repo, control_nb, "document")
    seed_source(repo, control_nb, "memory")
    job_id, identity = _stage_for_publish(repo, control_nb)
    assert jobs.publish_indexing_pipeline_success(job_id, control_nb, *identity)
    assert _n(repo, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (control,)) >= 1

    # 1. the store: refused before the first live mutation
    notebook_id = repo.create_notebook(NotebookCreate(name="drift")).id
    doc = seed_source(repo, notebook_id, "document", chunks=1)
    planted = _n(repo, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (doc,))
    job_id, identity = _stage_for_publish(repo, notebook_id)
    retype(repo, doc, "memory")  # stage taken while it was ordinary, then it is Memory
    drift_visible_predicate()
    with pytest.raises(IndexingPipelineMemorySourceError, match=REFUSAL):
        jobs.publish_indexing_pipeline_success(job_id, notebook_id, *identity)
    # the pre-existing chunk row survives, nothing new was written, the pipeline
    # state is unmoved
    assert _n(repo, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (doc,)) == planted
    state = repo._runtime.notebook_store.indexing_pipeline_state(notebook_id)
    assert state["published_pipeline_id"] != "test.pipeline"

    # 2. the no-KG path: the exception no longer escapes without closing the job
    nb2 = repo.create_notebook(NotebookCreate(name="drift-no-kg")).id
    doc2 = seed_source(repo, nb2, "document", chunks=1)
    job_id, identity = _stage_chunks(repo, nb2)
    retype(repo, doc2, "memory")
    with pytest.raises(IndexingPipelineMemorySourceError):
        lifecycle.finish_indexing_pipeline_job(
            nb2, job_id, succeeded=True, pipeline_identity=identity
        )
    job = _job(repo, job_id)
    assert (job["status"], job["error_code"], job["error_message"]) == (
        "failed", INDEXING_PIPELINE_MEMORY_SOURCE_CODE, INDEXING_PIPELINE_MEMORY_SOURCE_MESSAGE
    )
    assert _stage_rows(repo, job_id) == 0
    assert nb2 not in lifecycle.kg_building
    assert _n(repo, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (doc2,)) == 1

    # 3. the KG path: an element-less source is staged with an empty KG payload by
    #    the run itself; the refusal comes at the publish and ends the job classified
    nb3 = repo.create_notebook(NotebookCreate(name="drift-kg")).id
    doc3 = seed_source(repo, nb3, "document", elements=False)
    bind_chat_client(repo, "kg_extract", _ProbeClient())
    job_id, identity = _stage_chunks(repo, nb3)
    real_drained = lifecycle._ingestion_drained_for_publish

    def drained_then_drift(notebook):
        retype(repo, doc3, "memory")
        return real_drained(notebook)

    lifecycle._ingestion_drained_for_publish = drained_then_drift
    try:
        with pytest.raises(IndexingPipelineMemorySourceError):
            lifecycle._run_notebook_kg_job(
                nb3, job_id, "rebuild", indexing_pipeline_identity=identity
            )
    finally:
        lifecycle._ingestion_drained_for_publish = real_drained
    job = _job(repo, job_id)
    assert (job["status"], job["error_code"], job["error_message"]) == (
        "failed", INDEXING_PIPELINE_MEMORY_SOURCE_CODE, INDEXING_PIPELINE_MEMORY_SOURCE_MESSAGE
    )
    assert _stage_rows(repo, job_id) == 0
    assert nb3 not in lifecycle.kg_building


# ========================================================= Knowhow transfer
def _transfer_payload(repo, notebook_id: str, source_type: str, tag: str) -> dict:
    """A minimal Knowhow copy payload: a real table snapshot re-keyed, a hidden
    source of ``source_type`` and one chunk of it."""
    table_id = repo.create_knowhow_table(
        notebook_id, f"t-{tag}", "", [{"name": "Topic", "role": "anchor"}],
        created_by="user-local",
    )
    columns = repo.get_knowhow_table(table_id)["columns"]
    repo.add_knowhow_row(table_id, {c["id"]: "v" for c in columns}, actor="user-local")
    snap = repo._runtime.knowhow_transfer_store.snapshot_table(table_id)
    new_table = f"khtbl-{tag}"
    column_map = {c["id"]: f"khcol-{tag}-{i}" for i, c in enumerate(snap["columns"])}
    row_map = {r["id"]: f"khrow-{tag}-{i}" for i, r in enumerate(snap["rows"])}
    source_id = f"src-transfer-{tag}"
    return {
        "table": {**snap["table"], "id": new_table, "title": f"copy {tag}",
                  "hidden_source_id": None},
        "columns": [{**c, "id": column_map[c["id"]], "table_id": new_table}
                    for c in snap["columns"]],
        "rows": [{**r, "id": row_map[r["id"]], "table_id": new_table}
                 for r in snap["rows"]],
        "cells": [{**c, "id": f"copy-{tag}-{c['id']}", "row_id": row_map[c["row_id"]],
                   "column_id": column_map[c["column_id"]]} for c in snap["cells"]],
        "cell_code": [],
        "assets": [],
        "source": {"id": source_id, "notebook_id": notebook_id, "title": "projection",
                   "source_type": source_type, "status": "ready",
                   "parse_status": "parsed", "created_at": NOW, "updated_at": NOW},
        "elements": [],
        "chunks": [{"id": f"ck-transfer-{tag}", "notebook_id": notebook_id,
                    "source_id": source_id, "text": "visible", "section_path": "",
                    # a snapshot spells the JSON column natively per backend
                    "element_ids": ["el-x"] if _is_postgres(repo) else '["el-x"]',
                    "created_at": NOW}],
        "chunk_embeddings": [],
    }, {"columns": len(snap["columns"]), "rows": len(snap["rows"]),
        "cells": len(snap["cells"]), "cell_code": 0}


def scenario_transfer_refuses_memory_source(repo) -> None:
    store = repo._runtime.knowhow_transfer_store
    notebook_id = repo.create_notebook(NotebookCreate(name="transfer")).id

    def refused(tag: str, source_type: str, mutate=None, match=REFUSAL) -> None:
        payload, counts = _transfer_payload(repo, notebook_id, source_type, tag)
        if mutate is not None:
            mutate(payload)
        # a classified refusal (the route answers it with a user error, not a 500)
        with pytest.raises(KnowhowTransferRefused, match=match) as caught:
            store.insert_transfer(payload, counts)
        assert caught.value.reason == (
            TRANSFER_REASON_MEMORY if match == REFUSAL else TRANSFER_REASON_MISMATCH
        )
        # the whole transfer rolled back: no table, no hidden source, no chunk
        assert _n(repo, "SELECT COUNT(*) AS n FROM knowhow_tables WHERE id=?",
                  (payload["table"]["id"],)) == 0
        assert _n(repo, "SELECT COUNT(*) AS n FROM sources WHERE id=?",
                  (f"src-transfer-{tag}",)) == 0
        assert _n(repo, "SELECT COUNT(*) AS n FROM chunks WHERE id LIKE ?",
                  (f"ck-transfer-{tag}%",)) == 0

    # control: a Knowhow-typed hidden source transfers as before
    payload, counts = _transfer_payload(repo, notebook_id, "knowhow", "ok")
    store.insert_transfer(payload, counts)
    assert _n(repo, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?",
              (payload["source"]["id"],)) == 1

    # the transferred source is a Memory source
    refused("bad", "memory")
    # every chunk row must name the payload's own source: a second row that names a
    # Memory source (the first one is fine) is refused, not just the first row probed
    memory_source = seed_source(repo, notebook_id, "memory")

    def add_foreign_row(payload):
        payload["chunks"].append({**payload["chunks"][0], "id": "ck-transfer-mix-2",
                                  "source_id": memory_source})

    refused("mix", "knowhow", add_foreign_row, TRANSFER_MISMATCH)

    def only_foreign_row(payload):
        payload["chunks"][0]["source_id"] = memory_source

    refused("alien", "knowhow", only_foreign_row, TRANSFER_MISMATCH)

    def drop_source(payload):
        payload["source"] = None

    refused("orphan", "knowhow", drop_source, TRANSFER_MISMATCH)


# ============================================================== sync import
# The importer's reads of ``sources`` that are NOT a Memory probe, by exact text
# (an ``IN (?,...)`` list written as ``IN (?)``): the check that a mirrored Memory
# source is not deleted from under its memory item (``_protected_memory_sources``).
_OTHER_SOURCES_READS = frozenset({
    "SELECT id, memory_id FROM sources WHERE notebook_id IN (?) AND source_type = 'memory'",
})


def _one_mark(statement: str) -> str:
    return re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?)", " ".join(statement.split()))


class _StatementCounter:
    """Watches EVERY statement the importer runs through its backend while an
    import (or a pre-flight) is running: ``_Source.sql`` is the one translation
    every ``fetch`` / ``stream`` / ``conn.execute(backend.sql(...))`` of the
    importer passes its text through. Each statement that reads ``sources`` must be
    EXACTLY the one production builder's output for its own id count
    (``import_._source_rows_statement(n)``, the statement the EXPLAIN pins run),
    which is counted, or one of the importer's other reads of ``sources``
    (``_OTHER_SOURCES_READS``). Anything else lands in ``foreign``: a probe
    rewritten into another shape (a whole-table scan of the Memory sources, a
    probe with a type predicate) cannot hide from the count."""

    def __init__(self, monkeypatch):
        from app.migration.sync.import_ import _source_rows_statement

        self.count = 0
        self.foreign: list[str] = []
        self.active = False
        original = sync_database._Source.sql
        counter = self

        def sql(this, statement):
            if counter.active and re.search(r"\bFROM\s+sources\b", statement, re.I):
                if statement == _source_rows_statement(statement.count("?")):
                    counter.count += 1
                elif _one_mark(statement) not in _OTHER_SOURCES_READS:
                    counter.foreign.append(statement)
            return original(this, statement)

        monkeypatch.setattr(sync_database._Source, "sql", sql)

    def watching(self, run):
        """``run`` with the counter switched on for its duration (the exporter
        shares the backend class; its statements are not the importer's)."""

        def watched(*args, **kwargs):
            self.active = True
            try:
                return run(*args, **kwargs)
            finally:
                self.active = False

        return watched


def _prepare_source_notebook(src, owner_id: str) -> dict:
    notebook_id = src.create_notebook(NotebookCreate(name="mirrored")).id
    _write(src, "UPDATE notebooks SET created_by=? WHERE id=?", (owner_id, notebook_id))
    return {
        "notebook": notebook_id,
        # A: many chunk rows -- the probe must not scale with them
        "a": seed_source(src, notebook_id, "document", chunks=25, title="A"),
        "b": seed_source(src, notebook_id, "document", chunks=3, title="B"),
        "c": seed_source(src, notebook_id, "document", chunks=1, title="C"),
        # a type that merely starts like the Memory one: the refusal is on the exact type
        "d": seed_source(src, notebook_id, "memo", chunks=2, title="D"),
    }


def dump_database(repo) -> dict[str, list[str]]:
    """Every row of every base table, for a byte-for-byte before/after comparison."""
    if _is_postgres(repo):
        names = _rows(
            repo,
            "SELECT table_name AS n FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE'",
        )
    else:
        names = _rows(
            repo,
            "SELECT name AS n FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'",
        )
    out = {}
    for name in sorted(row["n"] for row in names):
        rows = _rows(repo, f'SELECT * FROM "{name}"')
        out[name] = sorted(repr(sorted(dict(row).items())) for row in rows)
    return out


def _refused_with(exc, *needles: str) -> None:
    text = str(exc.value)
    for needle in needles:
        assert needle in text, (needle, text)


def scenario_import_refuses_memory_sources(
    src, dst, export, do_import, owner_id: str, monkeypatch
) -> None:
    """The import's two Memory refusals end the whole run BEFORE any table is
    applied: the target is byte-identical to what it was, a dry run refuses too,
    and the message says what is wrong, what to do, and how many / which sources."""
    import app.migration.sync.import_ as import_module

    ids = _prepare_source_notebook(src, owner_id)
    a, b, c, d = ids["a"], ids["b"], ids["c"], ids["d"]
    a_chunks = _n(src, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (a,))
    counter = _StatementCounter(monkeypatch)
    do_import = counter.watching(do_import)

    # 1. passages of a Memory source in the package (legacy state at the source):
    #    refused, nothing applied, on a target that has nothing yet
    retype(src, b, "memory")
    before = dump_database(dst)
    package = export()
    for kwargs in ({}, {"dry_run": True}):
        counter.count = 0
        with pytest.raises(SyncImportError) as refused:
            do_import(package, **kwargs)
        _refused_with(
            refused, "1 Memory source(s)", b, "Upgrade the source environment",
            "export a FULL package again", "cannot be resumed",
        )
        assert dump_database(dst) == before
        # one statement for the package's source ids; no per-row probe
        assert counter.count == 1, counter.count
        assert not counter.foreign, counter.foreign

    # 2. the source library is cleaned (what the E4-5 migration does); the next
    #    package imports. Statements: the pre-flight's one + the apply pass's two
    #    (sources batch, chunks batch), however many rows the tables carry
    _write(src, "DELETE FROM chunks WHERE source_id=?", (b,))
    counter.count = 0
    clean = do_import(export())
    assert counter.count == 3, counter.count
    assert not counter.foreign, counter.foreign
    assert clean.tables["chunks"].skipped == 0
    assert _n(dst, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (a,)) == a_chunks
    assert _n(dst, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (b,)) == 0
    assert _n(dst, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (d,)) == 2  # 'memo' is no Memory

    # 3. the same id typed differently in the two environments, both directions:
    #    refused before anything is applied, the target byte-identical
    before = dump_database(dst)
    retype(src, a, "memory")       # package: memory, target: document
    for kwargs in ({}, {"dry_run": True}):
        with pytest.raises(SyncImportError) as refused:
            do_import(export(), **kwargs)
        _refused_with(
            refused, "1 source id(s)", "different type", a, "package: memory, target: document",
            "the same source id has different types in the two environments",
            "请先核对两边的这些来源、让它们的类型一致，再重新同步。",
        )
        assert dump_database(dst) == before
    retype(src, a, "document")
    retype(src, b, "document")     # package: document, target: memory
    with pytest.raises(SyncImportError) as refused:
        do_import(export())
    _refused_with(refused, b, "package: document, target: memory")
    assert dump_database(dst) == before
    retype(src, a, "memory")       # both flips at once: one refusal counting both
    with pytest.raises(SyncImportError) as refused:
        do_import(export())
    _refused_with(refused, "2 source id(s)", a, b)
    assert dump_database(dst) == before

    # 4. with the flips undone an ordinary edit imports as usual
    retype(src, a, "document")
    retype(src, b, "memory")
    _write(src, "UPDATE sources SET title=? WHERE id=?", ("C renamed", c))
    ok = do_import(export())
    assert ok.tables["sources"].updated == 4
    assert _rows(dst, "SELECT title AS t FROM sources WHERE id=?", (c,))[0]["t"] == "C renamed"

    # 5. the per-batch backstop inside the apply pass (for whatever changed after
    #    the pre-flight read): switch the pre-flight off and the apply pass refuses
    monkeypatch.setattr(import_module, "_preflight_memory_sources", lambda *args: None)
    retype(src, a, "memory")
    counter.count = 0
    with pytest.raises(SyncImportError) as refused:
        do_import(export())
    _refused_with(refused, "1 source id(s)", a, "package: memory, target: document")
    assert counter.count == 1, counter.count
    assert _rows(dst, "SELECT source_type AS t FROM sources WHERE id=?", (a,))[0]["t"] == "document"
    retype(src, a, "document")
    _write(
        src,
        "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        # an id that sorts after every other chunk row: the Memory source's row is
        # not the first of its batch, so the backstop must look at every row
        ("zz-legacy", ids["notebook"], b, "legacy", "", "[]", NOW),
    )
    with pytest.raises(SyncImportError) as refused:
        do_import(export())
    _refused_with(refused, "1 Memory source(s)", b, "cannot be resumed")
    assert _n(dst, "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (b,)) == 0
    # every probe of the whole scenario was the one production statement
    assert not counter.foreign, counter.foreign


def scenario_import_refuses_passages_of_a_target_side_memory_source(
    dst, tmp_path, run_preflight, monkeypatch
) -> None:
    """A window package can carry passage rows for a source it carries no source
    row for; the pre-flight then asks the target for that source's type, with the
    one production probe (one statement for all those ids). The message is bounded:
    the count, then the first ``_MEMORY_ID_LIMIT`` ids in sorted order and
    "(and N more)"."""
    from app.migration.sync.import_ import _MEMORY_ID_LIMIT

    notebook_id = dst.create_notebook(NotebookCreate(name="window")).id
    memory_sources = [
        seed_source(dst, notebook_id, "memory", elements=False)
        for _ in range(_MEMORY_ID_LIMIT + 5)
    ]
    plain_source = seed_source(dst, notebook_id, "document", elements=False)
    context = types.SimpleNamespace(package_dir=tmp_path / "pkg")
    (context.package_dir / "rows").mkdir(parents=True)
    # the package names the Memory sources in reverse order: the message's order is
    # its own
    (context.package_dir / "rows" / "chunks.jsonl").write_text(
        json.dumps({"id": "ck-w", "source_id": plain_source}) + "\n"
        + "".join(
            json.dumps({"id": f"ck-{i}", "source_id": source_id}) + "\n"
            for i, source_id in enumerate(sorted(memory_sources, reverse=True))
        )
    )
    counter = _StatementCounter(monkeypatch)
    with pytest.raises(SyncImportError) as refused:
        counter.watching(run_preflight)(context)
    text = str(refused.value)
    ordered = sorted(memory_sources)
    _refused_with(
        refused, f"{len(memory_sources)} Memory source(s)",
        "Source ids: " + ", ".join(ordered[:_MEMORY_ID_LIMIT]) + " (and 5 more)",
    )
    for source_id in (*ordered[_MEMORY_ID_LIMIT:], plain_source):
        assert source_id not in text, (source_id, text)
    # one statement for the ids the package carries no source row for, and it is the
    # production probe: a whole-table read of the Memory sources would be foreign
    assert counter.count == 1, counter.count
    assert not counter.foreign, counter.foreign


# ------------------------------------------------------------ SQLite lane
@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    r = SQLiteRepository(Settings(_env_file=None), indexing_pipeline_host=_PipelineHost())
    try:
        yield r
    finally:
        r.close()


def test_publish_refuses_memory_source(repo, monkeypatch):
    from app.repositories.sqlite import kg_build_job_store

    scenario_publish_refuses_memory_source(
        repo,
        lambda: monkeypatch.setattr(
            kg_build_job_store, "VISIBLE_SOURCE_TYPES_PREDICATE", "1=1"
        ),
    )


def test_transfer_refuses_memory_source(repo):
    scenario_transfer_refuses_memory_source(repo)


def _lane(root: Path, name: str):
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite:///{root / f'{name}.db'}",
        storage_dir=str(root / "storage"),
    )
    return settings, SQLiteRepository(settings)


def _add_user(repo, user_id: str, username: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES(?,?,?,?,'active',?,?,?)",
            (user_id, f"{user_id}@example.invalid", username.title(), "user",
             username, NOW, NOW),
        )


def test_import_refuses_memory_sources(tmp_path, monkeypatch):
    src_settings, src = _lane(tmp_path / "a", "a")
    dst_settings, dst = _lane(tmp_path / "b", "b")
    try:
        _add_user(src, ALICE[0], ALICE[2])
        _add_user(dst, ALICE[1], ALICE[2])
        exports = iter(range(100))

        def export():
            notebook = _rows(src, "SELECT id FROM notebooks WHERE created_by=?", (ALICE[0],))
            report = export_notebooks(
                src_settings, target_env="prod", out_dir=tmp_path / f"out{next(exports)}",
                notebook_ids=[notebook[0]["id"]], source_env="dev",
            )
            return report.package_dir

        scenario_import_refuses_memory_sources(
            src, dst, export,
            lambda pkg, **kw: import_package(dst_settings, pkg, **kw),
            ALICE[0], monkeypatch,
        )
    finally:
        src.close()
        dst.close()


def test_type_conflict_message_is_bounded_and_sorted():
    """The type-conflict refusal names the count, then the first
    ``_MEMORY_ID_LIMIT`` conflicts sorted by source id, then "(and N more)"."""
    from app.migration.sync.import_ import (
        _MEMORY_ID_LIMIT,
        _memory_type_conflicts,
        _memory_type_message,
    )

    ids = [f"src-{i:02d}" for i in range(_MEMORY_ID_LIMIT + 5)]
    package = {sid: "memory" for sid in reversed(ids)}
    target = {sid: "document" for sid in reversed(ids)}
    conflicts = _memory_type_conflicts(package, target)
    assert [sid for sid, _package, _target in conflicts] == ids
    message = _memory_type_message(conflicts)
    shown = ", ".join(f"{sid} (package: memory, target: document)" for sid in ids[:_MEMORY_ID_LIMIT])
    assert f"{len(ids)} source id(s)" in message, message
    assert f"Source ids: {shown} (and 5 more)" in message, message
    for sid in ids[_MEMORY_ID_LIMIT:]:
        assert sid not in message, message


def test_import_refuses_passages_of_a_target_side_memory_source(repo, tmp_path, monkeypatch):
    from pathlib import Path as _Path

    import app.migration.sync.import_ as import_module

    settings = Settings(_env_file=None)

    def check(context):
        backend = import_module._Backend(settings, _Path(__file__).resolve().parents[2])
        try:
            with backend.read() as conn:
                import_module._preflight_memory_sources(backend, conn, context)
        finally:
            backend.close()

    scenario_import_refuses_passages_of_a_target_side_memory_source(
        repo, tmp_path, check, monkeypatch
    )
