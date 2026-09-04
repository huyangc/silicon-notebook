from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.ports import BatchMaintenancePort, OfflineMaintenanceBusyError
from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp


pytestmark = pytest.mark.xdist_group(name="postgres_offline_maintenance")


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed_source_and_objects(repository, notebook_id: str) -> None:
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            "VALUES ('src-a',%s,'source','markdown','extracted','parsed',"
            "'a.md','',0,'hash-a','',%s,%s,'textbook')",
            (notebook_id, now, now),
        )
        for object_id in ("ko-a", "ko-z"):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,payload,evidence,source_id,"
                "created_at,updated_at) VALUES (%s,%s,'concept','approved',%s,%s,"
                "'src-a',%s,%s)",
                (
                    object_id,
                    notebook_id,
                    jsonb({"name": object_id}),
                    jsonb([{"source_id": "src-a", "quoted_span": object_id}]),
                    now,
                    now,
                ),
            )
        # Upsert, not a bare INSERT: create_notebook now seeds this row at
        # birth (certified-empty reverse index), so the legacy fixture must
        # overwrite that row rather than collide with it on the primary key.
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,kg_mutation_seq,updated_at,source_index_backfilled) "
            "VALUES (%s,0,0,%s,1) "
            "ON CONFLICT (notebook_id) DO UPDATE SET "
            "dirty=excluded.dirty,kg_mutation_seq=excluded.kg_mutation_seq,"
            "updated_at=excluded.updated_at,"
            "source_index_backfilled=excluded.source_index_backfilled",
            (notebook_id, now),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES ('ko-a','src-a',%s)",
            (notebook_id,),
        )


def test_postgres_adapter_implements_every_batch_maintenance_method():
    declared = {
        name
        for protocol in BatchMaintenancePort.__mro__
        for name, value in protocol.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    implemented = {
        name
        for name in dir(PostgresMaintenanceAdapter)
        if callable(getattr(PostgresMaintenanceAdapter, name, None))
        and not name.startswith("_")
    }
    assert declared <= implemented


@pytest.mark.postgres_integration
def test_offline_lock_uses_non_pool_session_and_reports_contention(
    postgres_repository,
):
    database = postgres_repository._runtime.database
    database._pool.resize(1, 1)

    with postgres_repository.maintenance.offline_maintenance_lock():
        # A pool-backed advisory-lock session would consume the sole slot and
        # make this ordinary maintenance read time out.
        assert postgres_repository.maintenance.all_notebook_ids() == []
        with pytest.raises(OfflineMaintenanceBusyError, match="already running"):
            with postgres_repository.maintenance.offline_maintenance_lock():
                raise AssertionError("unreachable")

    # Explicit unlock (and the close safety net) makes the lock reusable.
    with postgres_repository.maintenance.offline_maintenance_lock():
        pass


@pytest.mark.postgres_integration
def test_new_notebook_status_is_typed_serializable(postgres_repository):
    """codex #601 R2 rebuttal guard: the born `unified_kg_state` row leaves
    `last_rebuild_at` NULL on PostgreSQL, but the status service reads it
    through the PG `state_row`, whose `iso_timestamp` normalizes NULL to "".
    A freshly created notebook must therefore keep `/unified-kg/status`
    serializable through the typed model, byte-equal to the row-absent shape."""
    from app.models.kg import UnifiedKgStatus
    from app.models.schemas import NotebookCreate

    nb = postgres_repository.create_notebook(NotebookCreate(name="fresh-status"))
    status = postgres_repository.unified_kg_status(nb.id)
    model = UnifiedKgStatus(**status)
    assert model.last_rebuild_at == ""
    assert model.dirty is False
    assert (model.objects, model.relations, model.clusters) == (0, 0, 0)


@pytest.mark.postgres_integration
def test_source_index_backfill_is_bounded_restartable_and_marks_only_at_end(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="batch")).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    maintenance = postgres_repository.maintenance

    assert maintenance.clear_source_index(notebook_id) == 2
    with postgres_repository._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources "
            "WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT source_index_backfilled AS done FROM unified_kg_state "
            "WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["done"] == 0

    first = maintenance.backfill_source_index_batch(notebook_id, "", 1)
    second = maintenance.backfill_source_index_batch(notebook_id, first[2], 1)
    done = maintenance.backfill_source_index_batch(notebook_id, second[2], 1)
    assert first[:2] == (1, 1)
    assert second[:2] == (1, 1)
    assert done == (0, 0, second[2])

    maintenance.mark_source_index_backfilled(notebook_id)
    with postgres_repository._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources "
            "WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 2
        assert db.execute(
            "SELECT source_index_backfilled AS done FROM unified_kg_state "
            "WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["done"] == 1


def _seed_chunks_with_elements(repository, notebook_id: str) -> None:
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES ('zz',%s,'src-a','zz','',%s,%s)",
            (notebook_id, jsonb(["e1"]), now),
        )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES ('aa',%s,'src-a','aa','',%s,%s)",
            (notebook_id, jsonb(["e1", "e2"]), now),
        )


@pytest.mark.postgres_integration
def test_chunk_element_backfill_durable_cursor_resumes_and_guards_generation(
    postgres_repository,
):
    """PostgreSQL 侧与 SQLite 的 chunk_elements 回填逐条对等。

    额外钉住两件只有真库能证的:``ORDER BY c.ordinal`` 复刻 SQLite 的 rowid 插入序
    (fixture 故意让 id 序与插入序相反),以及生成列 ``chunk_elements_indexed``
    只在最后一页才翻真。"""
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="durable chunk elements")
    ).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    _seed_chunks_with_elements(postgres_repository, notebook_id)
    maintenance = postgres_repository.maintenance

    started = maintenance.begin_chunk_element_backfill(notebook_id, force=True)
    assert started["status"] == "running"
    assert started["total_chunks"] == 2

    first = maintenance.resume_chunk_element_backfill_batch(notebook_id, batch_size=1)
    assert first["status"] == "running"
    assert first["chunks_scanned"] == 1
    assert maintenance.chunk_elements_indexed(notebook_id) is False

    resumed = maintenance.begin_chunk_element_backfill(notebook_id)
    assert resumed["resumed"] is True
    assert resumed["chunks_scanned"] == 1

    with postgres_repository._runtime.database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=%s",
            (notebook_id,),
        )
    failed = maintenance.resume_chunk_element_backfill_batch(notebook_id, batch_size=1)
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "kg_generation_changed"
    assert maintenance.chunk_elements_indexed(notebook_id) is False

    restarted = maintenance.begin_chunk_element_backfill(notebook_id)
    assert restarted["status"] == "running"
    assert restarted["chunks_scanned"] == 0
    with postgres_repository._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM chunk_elements WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"] == 0

    # Completion is "the cursor came back empty", never a scanned/total
    # comparison (total_chunks is frozen at begin() while writes continue), so
    # the two data pages are always followed by one empty page.
    assert maintenance.resume_chunk_element_backfill_batch(
        notebook_id, batch_size=1
    )["status"] == "running"
    assert maintenance.resume_chunk_element_backfill_batch(
        notebook_id, batch_size=1
    )["status"] == "running"
    assert maintenance.chunk_elements_indexed(notebook_id) is False
    complete = maintenance.resume_chunk_element_backfill_batch(
        notebook_id, batch_size=1
    )
    assert complete["status"] == "complete"
    assert complete["batch_chunks"] == 0
    assert complete["chunks_scanned"] == 2
    assert complete["rows_written"] == 3
    assert maintenance.chunk_elements_indexed(notebook_id) is True

    with postgres_repository._runtime.database.connect() as db:
        terminal = db.execute(
            "SELECT status,completed_at FROM chunk_element_backfills "
            "WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        assert terminal["status"] == "complete"
        assert terminal["completed_at"] is not None
        # Insertion order (ordinal), not id order: 'zz' was inserted first.
        rows = postgres_repository._runtime.chunk_store.chunks_for_element_ids(
            db, notebook_id, ["e1"]
        )
        assert [row["chunk_id"] for row in rows] == ["zz", "aa"]

    skipped = maintenance.begin_chunk_element_backfill(notebook_id)
    assert skipped["already_complete"] is True
    assert skipped["chunks_scanned"] == 2


@pytest.mark.postgres_integration
def test_chunk_elements_follow_the_chunk_write_transaction(postgres_repository):
    """写路径前向维护 + chunks 级联删除,与 SQLite 侧逐条对等。"""
    from app.repositories.ports import ChunkWrite

    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="chunk element writes")
    ).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    store = postgres_repository._runtime.chunk_store
    now = normalize_timestamp(postgres_repository._runtime.seams.now())

    store.replace_source_chunks(
        "src-a",
        notebook_id,
        [ChunkWrite(id="c1", text="a", section_path="", element_ids=["e1", "e1"])],
        created_at=now,
    )
    with postgres_repository._runtime.database.connect() as db:
        assert [
            (row["element_id"], row["chunk_id"])
            for row in db.execute(
                "SELECT element_id,chunk_id FROM chunk_elements WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchall()
        ] == [("e1", "c1")]

    store.replace_source_chunks(
        "src-a",
        notebook_id,
        [ChunkWrite(id="c2", text="b", section_path="", element_ids=["e2"])],
        created_at=now,
    )
    with postgres_repository._runtime.database.connect() as db:
        assert [
            (row["element_id"], row["chunk_id"])
            for row in db.execute(
                "SELECT element_id,chunk_id FROM chunk_elements WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchall()
        ] == [("e2", "c2")]


@pytest.mark.postgres_integration
def test_source_index_backfill_durable_cursor_resumes_and_guards_generation(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="durable source index")
    ).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    maintenance = postgres_repository.maintenance

    started = maintenance.begin_source_index_backfill(notebook_id, force=True)
    assert started["status"] == "running"
    first = maintenance.resume_source_index_backfill_batch(notebook_id, batch_size=1)
    assert first["objects_scanned"] == 1
    assert first["status"] == "running"

    resumed = maintenance.begin_source_index_backfill(notebook_id)
    assert resumed["resumed"] is True
    assert resumed["objects_scanned"] == 1
    with postgres_repository._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"] == 1

    with postgres_repository._runtime.database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1,"
            "source_index_backfilled=0 WHERE notebook_id=%s",
            (notebook_id,),
        )
    failed = maintenance.resume_source_index_backfill_batch(notebook_id, batch_size=1)
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "kg_generation_changed"

    restarted = maintenance.begin_source_index_backfill(notebook_id)
    assert restarted["status"] == "running"
    assert restarted["objects_scanned"] == 0
    assert restarted["resumed"] is False
    page = maintenance.resume_source_index_backfill_batch(notebook_id, batch_size=1)
    assert page["status"] == "running"
    complete = maintenance.resume_source_index_backfill_batch(
        notebook_id, batch_size=1
    )
    assert complete["status"] == "complete"
    assert complete["objects_scanned"] == 2
    with postgres_repository._runtime.database.connect() as db:
        terminal = db.execute(
            "SELECT status,completed_at FROM source_index_backfills "
            "WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        assert terminal["status"] == "complete"
        assert terminal["completed_at"] is not None
        assert db.execute(
            "SELECT source_index_backfilled AS done FROM unified_kg_state "
            "WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["done"] == 1
    skipped = maintenance.begin_source_index_backfill(notebook_id)
    assert skipped["already_complete"] is True
    assert skipped["objects_scanned"] == 2


@pytest.mark.postgres_integration
def test_source_fact_backfill_is_restartable_and_jsonb_safe(postgres_repository):
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="source facts")
    ).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES ('el-a','src-a','paragraph','p','body',%s,%s)",
            (jsonb({}), now),
        )
        db.execute(
            "UPDATE knowledge_objects SET evidence=%s",
            (jsonb([{"source_id": "src-a", "element_id": "el-a"}]),),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-history',%s,'src-a','kg','completed',"
            "'kg objects=2 relations=0',%s,%s)",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-paper',%s,'src-a','paper_meta','running','',"
            "'9999-12-31T23:59:59Z','9999-12-31T23:59:59Z')",
            (notebook_id,),
        )
    maintenance = postgres_repository.maintenance
    maintenance.clear_source_index(notebook_id)
    first_index = maintenance.backfill_source_index_batch(notebook_id, "", 10)
    assert first_index[:2] == (2, 2)
    maintenance.mark_source_index_backfilled(notebook_id)

    first = maintenance.backfill_source_fact_batch(
        notebook_id, "src-a", batch_size=1
    )
    second = maintenance.backfill_source_fact_batch(
        notebook_id, "src-a", batch_size=1
    )
    final = maintenance.backfill_source_fact_batch(
        notebook_id, "src-a", batch_size=1
    )
    assert first["status"] == second["status"] == "running"
    assert final["status"] == "complete"
    assert final["facts_written"] == 2
    with runtime.database.connect() as db:
        facts = db.execute(
            "SELECT payload,evidence FROM knowledge_source_facts "
            "WHERE source_id='src-a' ORDER BY id"
        ).fetchall()
    assert sorted(row["payload"]["name"] for row in facts) == ["ko-a", "ko-z"]
    assert all(row["evidence"][0]["element_id"] == "el-a" for row in facts)


@pytest.mark.postgres_integration
def test_source_fact_backfill_audits_owned_object_missing_from_reverse_index(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="source fact gaps")
    ).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES ('el-a','src-a','paragraph','p','body',%s,%s)",
            (jsonb({}), now),
        )
        db.execute(
            "UPDATE knowledge_objects SET evidence=%s WHERE id='ko-a'",
            (jsonb([{"source_id": "src-a", "element_id": "el-a"}]),),
        )
        db.execute(
            "UPDATE knowledge_objects SET evidence=%s WHERE id='ko-z'",
            (jsonb([]),),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-history',%s,'src-a','kg','completed',"
            "'kg objects=2 relations=0',%s,%s)",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO knowledge_source_facts "
            "(id,notebook_id,source_id,source_generation,local_object_id,"
            "global_object_id,object_type,payload,evidence,projection_version,"
            "created_at,updated_at) VALUES "
            "('ksf-orphan-live',%s,'src-a','run-history',"
            "'legacy:legitimate-live-id','ko-deleted','concept',%s,%s,1,%s,%s)",
            (
                notebook_id,
                jsonb({"name": "orphan live"}),
                jsonb([{"source_id": "src-a", "element_id": "el-a"}]),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_source_fact_elements "
            "(fact_id,notebook_id,source_id,source_generation,element_id,created_at) "
            "VALUES ('ksf-orphan-live',%s,'src-a','run-history','el-a',%s)",
            (notebook_id, now),
        )

    result = postgres_repository.maintenance.backfill_source_fact_batch(
        notebook_id, "src-a", batch_size=10
    )
    assert result["status"] == "incomplete"
    assert result["objects_scanned"] == 2
    assert result["facts_written"] == 2
    assert result["incomplete_objects"] == 1
    with runtime.database.connect() as db:
        state = db.execute(
            "SELECT incomplete_reason,failure_code "
            "FROM knowledge_source_fact_backfills "
            "WHERE source_id='src-a'"
        ).fetchone()
        origins = db.execute(
            "SELECT projection_origin,COUNT(*) AS c FROM knowledge_source_facts "
            "WHERE source_id='src-a' GROUP BY projection_origin "
            "ORDER BY projection_origin"
        ).fetchall()
    assert state["incomplete_reason"] == "missing_evidence"
    assert state["failure_code"] == ""
    assert [(row["projection_origin"], row["c"]) for row in origins] == [
        ("historical", 1),
        ("live", 1),
    ]


@pytest.mark.postgres_integration
def test_node_backfill_releases_read_connection_before_model_work(
    postgres_repository, monkeypatch
):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="embed")).id
    _seed_source_and_objects(postgres_repository, notebook_id)
    runtime = postgres_repository._runtime
    runtime.database._pool.resize(1, 1)
    calls: list[list[str]] = []

    def embed(_notebook_id, items, **_kwargs):
        # Acquiring the only pooled slot here proves discovery did not retain it
        # over the (potentially slow) model call.
        with runtime.database.connect() as db:
            assert db.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        calls.append([str(item["_oid"]) for item in items])
        return len(items)

    monkeypatch.setattr(runtime.source_embedding, "embed_objects_batch", embed)
    assert postgres_repository.maintenance.backfill_node_embeddings(notebook_id) == 2
    assert calls == [["ko-a", "ko-z"]]


@pytest.mark.postgres_integration
def test_document_kg_wipe_preserves_memory_projection_artifacts(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="memory-preserve")
    ).id
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for source_id, source_type in (("src-doc", "markdown"), ("src-mem", "memory")):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,%s,%s,'extracted','parsed','', '',0,%s,'',%s,%s,'')",
                (source_id, notebook_id, source_id, source_type, source_id, now, now),
            )
        for object_id, source_id in (("ko-doc", "src-doc"), ("ko-mem", "src-mem")):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'concept','approved',"
                "%s,%s,%s,%s,%s)",
                (object_id, notebook_id, jsonb({"name": object_id}), jsonb([]), source_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                "VALUES (%s,%s,%s,%s)", (object_id, notebook_id, b"vector", now),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                "VALUES (%s,%s,%s)", (object_id, source_id, notebook_id),
            )
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,evidence,created_at) VALUES (%s,%s,%s,%s,%s,"
                "'relates_to',%s,%s)",
                (f"rel-{source_id}", notebook_id, source_id, object_id, object_id, jsonb([]), now),
            )
            db.execute(
                "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,"
                "error_message,created_at,updated_at) VALUES (%s,%s,%s,'kg','completed','',%s,%s)",
                (f"run-{source_id}", notebook_id, source_id, now, now),
            )

    postgres_repository.delete_notebook_kg(notebook_id)

    with runtime.database.connect() as db:
        for table, id_column, kept, removed in (
            ("knowledge_objects", "id", "ko-mem", "ko-doc"),
            ("knowledge_embeddings", "object_id", "ko-mem", "ko-doc"),
            ("knowledge_object_sources", "object_id", "ko-mem", "ko-doc"),
            ("knowledge_relations", "id", "rel-src-mem", "rel-src-doc"),
            ("extraction_runs", "id", "run-src-mem", "run-src-doc"),
        ):
            assert db.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {id_column}=%s", (kept,)
            ).fetchone()["c"] == 1
            assert db.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {id_column}=%s", (removed,)
            ).fetchone()["c"] == 0


# ── batch-3-W1 PR-2: unified_kg_state seq semantics (design doc Sec 3.3) ──
# PostgreSQL twin of backend/tests/test_kg_repository.py's SQLite-side
# coverage — both backends share the same delete_notebook_graph_rows
# contract (design doc's own parity requirement for this PR).


@pytest.mark.postgres_integration
def test_delete_notebook_kg_resets_unified_kg_state_in_place_and_advances_epoch(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="epoch reset")
    ).id
    runtime = postgres_repository._runtime
    n_obj, n_rel = postgres_repository.store_kg(
        notebook_id, None,
        [{"local_id": "C1", "object_type": "concept", "payload": {"name": "x"}, "evidence": []}],
        [],
    )
    assert (n_obj, n_rel) == (1, 0)

    with runtime.database.connect() as db:
        before = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()
    assert before is not None
    assert int(before["kg_mutation_seq"]) > 0
    assert int(before["kg_reset_epoch"]) == 0

    postgres_repository.delete_notebook_kg(notebook_id)

    with runtime.database.connect() as db:
        after = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()
    assert after is not None, "the row must be RESET in place, never dropped"
    assert int(after["kg_mutation_seq"]) == 0
    assert int(after["cluster_mutation_seq"]) == 0
    assert int(after["community_seq"]) == -1
    assert int(after["canonical_rel_seq"]) == -1
    assert int(after["mention_seq"]) == -1
    assert after["cluster_input_version"] == ""
    assert after["last_rebuild_at"] is None
    assert int(after["object_count"]) == 0
    assert int(after["relation_count"]) == 0
    assert int(after["cluster_count"]) == 0
    assert int(after["kg_reset_epoch"]) == 1, "the ONE writer of kg_reset_epoch must bump it by 1"

    # A second delete (on the now-empty graph) advances the epoch again —
    # only increases, never decreases or resets.
    postgres_repository.delete_notebook_kg(notebook_id)
    with runtime.database.connect() as db:
        twice = db.execute(
            "SELECT kg_reset_epoch FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()
    assert int(twice["kg_reset_epoch"]) == 2


@pytest.mark.postgres_integration
def test_delete_notebook_kg_removes_this_notebooks_kg_analysis_artifacts_ledger(
    postgres_repository,
):
    """design doc Sec 3.2 table #15: the analysis-artifact LEDGER
    (``kg_analysis_artifacts``) is blanket-deleted alongside the graph, not
    reset — see the SQLite twin (test_kg_repository.py) for the full
    seq_behind rationale.

    R1 (P2-1, post-review): the ledger row and its DETAIL tables
    (``kg_community_edges`` / ``kg_source_profiles``) are a documented single
    unit; this now also seeds and asserts on those two detail tables — see
    the SQLite twin for the full rationale."""
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="artifact ledger")
    ).id
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO kg_analysis_artifacts (notebook_id,kind,kg_mutation_seq,payload,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (notebook_id, "cross_board_edges", 5, jsonb({}), now),
        )
        db.execute(
            "INSERT INTO kg_analysis_artifacts (notebook_id,kind,kg_mutation_seq,payload,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (notebook_id, "source_board_profiles", 5, jsonb({}), now),
        )
        db.execute(
            "INSERT INTO kg_community_edges (notebook_id,src_community_id,dst_community_id,weight) "
            "VALUES (%s,%s,%s,%s)",
            (notebook_id, "cid-a", "cid-b", 3),
        )
        db.execute(
            "INSERT INTO kg_source_profiles (notebook_id,source_id) VALUES (%s,%s)",
            (notebook_id, "src-x"),
        )
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_analysis_artifacts WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 2
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_community_edges WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 1
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_source_profiles WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 1

    postgres_repository.delete_notebook_kg(notebook_id)

    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_analysis_artifacts WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_community_edges WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_source_profiles WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 0


@pytest.mark.postgres_integration
def test_delete_notebook_kg_upserts_when_the_state_row_was_already_missing(
    postgres_repository,
):
    """R1 (P0-2, post-review): PostgreSQL twin of test_kg_repository.py's
    SQLite-side coverage. scripts/merge_dbs.py's ``KG_STATE_TABLES``
    deliberately DELETEs a merged notebook's ``unified_kg_state`` row; a bare
    UPDATE against that missing row is a silent no-op (rowcount 0), so
    kg_reset_epoch never advances and every reader's "row is None" fallback
    keeps computing the same (epoch=0, seq=0) key across the delete — even
    though real graph rows were removed in the same transaction."""
    from app.repositories.postgres import knowledge_counts_cache as kcc

    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="missing state row")
    ).id
    runtime = postgres_repository._runtime
    postgres_repository.store_kg(
        notebook_id, None,
        [
            {"local_id": "C1", "object_type": "concept", "payload": {"name": "x"}, "evidence": []},
            {"local_id": "C2", "object_type": "concept", "payload": {"name": "y"}, "evidence": []},
        ],
        [],
    )
    with runtime.database.connect() as db:
        before = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()
    assert before is not None
    assert int(before["kg_mutation_seq"]) > 0

    with runtime.database.write() as db:
        db.execute("DELETE FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,))
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()["c"] == 0

    kcc.invalidate()
    with runtime.database.connect() as db:
        stale_counts = kcc.type_status_counts(db, notebook_id)
    assert sum(stale_counts.values()) == 2

    counts = postgres_repository.delete_notebook_kg(notebook_id)
    assert counts["knowledge_objects"] == 2

    with runtime.database.connect() as db:
        after = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()
    assert after is not None, "the row must be resurrected, not left absent"
    assert int(after["kg_mutation_seq"]) == 0
    assert int(after["kg_reset_epoch"]) == 1, (
        "a missing-row delete is itself a reset event: epoch must start at "
        "1, not the 0 a bare re-insert would default to"
    )

    with runtime.database.connect() as db:
        fresh_counts = kcc.type_status_counts(db, notebook_id)
    assert sum(fresh_counts.values()) == 0, (
        "the memo warmed while the row was absent must NOT keep being "
        "served after delete_notebook_kg"
    )


@pytest.mark.postgres_integration
def test_source_target_pages_use_c_keysets_and_preserve_retry_semantics(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="targets")).id
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for source_id in ("src-B", "src-a", "src-z"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,%s,'markdown','extracted','parsed',%s,'',0,%s,'',"
                "%s,%s,'textbook')",
                (source_id, notebook_id, source_id, f"{source_id}.md", source_id, now, now),
            )
        for source_id, source_type in (
            ("src-hidden-k", "knowhow"),
            ("src-hidden-m", "memory"),
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,%s,%s,'extracted','parsed',%s,'',0,'','',%s,%s,'')",
                (source_id, notebook_id, source_id, source_type, f"{source_id}.md", now, now),
            )
        for source_id in ("src-B", "src-a"):
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (%s,%s,'paragraph','L1','text',%s,%s)",
                (f"el-{source_id}", source_id, jsonb({}), now),
            )
            db.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (%s,%s,%s,'text','',%s,%s)",
                (f"ck-{source_id}", notebook_id, source_id, jsonb([]), now),
            )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,"
            "created_at,updated_at) VALUES ('ko-partial',%s,'concept','approved',"
            "%s,%s,'src-a',%s,%s)",
            (
                notebook_id,
                jsonb({"name": "partial"}),
                jsonb([{"source_id": "src-a"}]),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-partial',%s,'src-a','kg','completed',"
            "'windows_failed=1/2',%s,%s)",
            (notebook_id, now, now),
        )

    maintenance = postgres_repository.maintenance
    assert maintenance.source_has_elements("src-B") is True
    assert maintenance.source_has_elements("src-z") is False
    assert maintenance.source_has_kg("src-a") is True
    assert maintenance.source_has_kg("src-B") is False
    visible_first = maintenance.user_source_ids_page(notebook_id, limit=2)
    visible_second = maintenance.user_source_ids_page(
        notebook_id, after_id=visible_first[-1], limit=2
    )
    assert visible_first == ["src-B", "src-a"]
    assert visible_second == ["src-z"]
    assert maintenance.source_ids_missing_elements_page(
        notebook_id, limit=1
    ) == ["src-z"]
    first_chunk = maintenance.missing_chunk_embedding_page(
        notebook_id, limit=1
    )
    assert [row["id"] for row in first_chunk] == ["ck-src-B"]
    assert [
        row["id"]
        for row in maintenance.missing_chunk_embedding_page(
            notebook_id, after_id=first_chunk[-1]["id"], limit=1
        )
    ] == ["ck-src-a"]
    first_element = maintenance.missing_element_embedding_page(
        notebook_id, limit=1
    )
    assert [row["id"] for row in first_element] == ["el-src-B"]
    assert [
        row["id"]
        for row in maintenance.missing_element_embedding_page(
            notebook_id, after_id=first_element[-1]["id"], limit=1
        )
    ] == ["el-src-a"]
    # only_source_id(审计批4):交互式 backfill 在源的分块锁内按源分页读。作用域下推到
    # SQL,与无界 rows 版的 only_source_id 口径逐一致(两后端同款)。
    assert [
        row["id"]
        for row in maintenance.missing_chunk_embedding_page(
            notebook_id, limit=50, only_source_id="src-a"
        )
    ] == ["ck-src-a"]
    assert [
        row["id"]
        for row in maintenance.missing_element_embedding_page(
            notebook_id, limit=50, only_source_id="src-a"
        )
    ] == ["el-src-a"]
    assert maintenance.missing_element_embedding_page(
        notebook_id, limit=50, only_source_id="src-nonexistent"
    ) == []
    assert {
        row["id"]
        for row in maintenance.missing_element_embedding_page(
            notebook_id, limit=50, only_source_id="src-a"
        )
    } == {
        row["id"]
        for row in maintenance.missing_element_embedding_rows(
            notebook_id, only_source_id="src-a"
        )
    }
    with pytest.raises(ValueError, match="positive"):
        maintenance.missing_chunk_embedding_page(notebook_id, limit=0)

    # 单次发现 + 按 id hydrate(审计批4 评审修订):交互式 backfill 在源的分块锁内**只发现
    # 一次**、正文再按页经主键取回。判据与无界 rows 版逐字一致,只把投影换成 id 并按 id 升序。
    assert maintenance.missing_chunk_embedding_ids(notebook_id) == ["ck-src-B", "ck-src-a"]
    assert maintenance.missing_element_embedding_ids(notebook_id) == ["el-src-B", "el-src-a"]
    for only in ("src-a", "src-B", None):
        assert set(
            maintenance.missing_chunk_embedding_ids(notebook_id, only_source_id=only)
        ) == {
            row["id"]
            for row in maintenance.missing_chunk_embedding_rows(
                notebook_id, only_source_id=only
            )
        }
        assert set(
            maintenance.missing_element_embedding_ids(notebook_id, only_source_id=only)
        ) == {
            row["id"]
            for row in maintenance.missing_element_embedding_rows(
                notebook_id, only_source_id=only
            )
        }
    assert maintenance.missing_element_embedding_ids(
        notebook_id, only_source_id="src-nonexistent"
    ) == []
    assert maintenance.chunk_texts_by_ids([]) == []
    assert maintenance.element_texts_by_ids([]) == []
    hydrated = maintenance.chunk_texts_by_ids(["ck-src-a", "ck-nope"])
    assert [row["id"] for row in hydrated] == ["ck-src-a"]
    assert hydrated[0]["source_id"] == "src-a" and hydrated[0]["text"]
    hydrated_el = maintenance.element_texts_by_ids(["el-src-a", "el-nope"])
    assert [row["id"] for row in hydrated_el] == ["el-src-a"]
    assert hydrated_el[0]["source_id"] == "src-a" and hydrated_el[0]["text"]

    # 体检 H4/H5 memo 键用的租约收窄查询(postgres 镜像):只留属于本 notebook 的 id。
    queries = postgres_repository._runtime.queries
    with postgres_repository._runtime.database.connect() as db:
        assert queries.notebook_source_ids_among(db, notebook_id, set()) == set()
        assert queries.notebook_source_ids_among(
            db, notebook_id, {"src-a", "src-z", "src-absent"}
        ) == {"src-a", "src-z"}
        assert queries.notebook_source_ids_among(
            db, "nb-does-not-exist", {"src-a"}
        ) == set()

    first = maintenance.kg_target_source_rows_page(
        notebook_id, limit=1, retry_partial=False
    )
    second = maintenance.kg_target_source_rows_page(
        notebook_id,
        after_id=first[-1]["source_id"],
        limit=2,
        retry_partial=False,
    )
    assert first == [{"source_id": "src-B", "is_partial": False}]
    assert second == []

    retry = maintenance.kg_target_source_rows_page(
        notebook_id, limit=10, retry_partial=True
    )
    assert retry == [
        {"source_id": "src-B", "is_partial": False},
        {"source_id": "src-a", "is_partial": True},
    ]


def test_statement_timeout_floor_raises_but_never_lowers(postgres_repository):
    """T-W4-4:维护整表 DELETE 的事务内放宽是「取底」——默认 30s 抬到 600s、
    运维已调更高的值保持、0(禁用)保持;且事务结束即复原(不泄漏)。"""
    from app.repositories.postgres.maintenance import (
        _raise_statement_timeout_floor,
    )

    runtime = postgres_repository._runtime

    def current_ms(db) -> int:
        return int(
            db.execute(
                "SELECT setting::bigint AS c FROM pg_settings "
                "WHERE name='statement_timeout'"
            ).fetchone()["c"]
        )

    # Case 1: a value below the floor (the pool default) is raised to 600s.
    with runtime.database.write() as db:
        before = current_ms(db)
        assert 0 < before < 600_000  # pool default (30s) — precondition
        _raise_statement_timeout_floor(db)
        assert current_ms(db) == 600_000
    # Transaction-local: gone after the block (no leak to the next borrower).
    with runtime.database.connect() as db:
        assert current_ms(db) == before

    # Case 2: an operator-raised value above the floor is kept, not lowered.
    with runtime.database.write() as db:
        db.execute("SELECT set_config('statement_timeout','1800000',true)")
        _raise_statement_timeout_floor(db)
        assert current_ms(db) == 1_800_000

    # Case 3: 0 (= timeout disabled) stays disabled.
    with runtime.database.write() as db:
        db.execute("SELECT set_config('statement_timeout','0',true)")
        _raise_statement_timeout_floor(db)
        assert current_ms(db) == 0
