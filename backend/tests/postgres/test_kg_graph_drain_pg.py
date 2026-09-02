"""batch-3-W1 T-5a PostgreSQL twin: the pre-reset drain's store primitives on
a real PostgreSQL (ctid form-two page, exists-at-offset backlog probe) plus
the end-to-end ``delete_notebook_kg`` equivalence — SQLite-side behaviour pins
live in ``backend/tests/test_kg_graph_drain.py``."""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp
from app.services import knowledge_lifecycle as kl

pytestmark = pytest.mark.xdist_group(name="postgres_kg_graph_drain")


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed_graph(postgres_repository, notebook_id, *, doc_objects, memory_objects=0):
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for source_id, source_type in (("src-doc", "markdown"), ("src-mem", "memory")):
            if source_type == "memory" and not memory_objects:
                continue
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,%s,%s,'extracted','parsed','', '',0,%s,'',%s,%s,'')",
                (source_id, notebook_id, source_id, source_type, source_id, now, now),
            )
        for i in range(doc_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'concept','approved',"
                "%s,%s,%s,%s,%s)",
                (f"ko-doc-{i}", notebook_id, jsonb({}), jsonb([]), "src-doc", now, now),
            )
        for i in range(memory_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'concept','approved',"
                "%s,%s,%s,%s,%s)",
                (f"ko-mem-{i}", notebook_id, jsonb({}), jsonb([]), "src-mem", now, now),
            )


@pytest.mark.postgres_integration
def test_drain_primitives_page_and_probe_on_postgres(postgres_repository):
    """ctid 形二分页 + LIMIT/OFFSET 探针在真 PG 上的行为:探针命名超阈值的表、
    每页有界、排干后探针转 None。"""
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="drain-primitives")
    ).id
    _seed_graph(postgres_repository, notebook_id, doc_objects=7)
    runtime = postgres_repository._runtime
    store = runtime.knowledge

    with runtime.database.connect() as db:
        assert store.graph_drain_backlog(db, notebook_id, 3) == ("knowledge_objects", 2)
        assert store.graph_drain_backlog(db, notebook_id, 7) is None
        # start 游标(评审 F4):从 knowledge_objects 之后起扫,看不到它的积压。
        assert store.graph_drain_backlog(db, notebook_id, 0, 3) is None

    with runtime.database.write() as db:
        assert store.drain_notebook_graph_rows_page(
            db, notebook_id, "knowledge_objects", 3
        )["knowledge_objects"] == 3
    with runtime.database.write() as db:
        assert store.drain_notebook_graph_rows_page(
            db, notebook_id, "knowledge_objects", 3
        )["knowledge_objects"] == 3
    with runtime.database.connect() as db:
        assert store.graph_drain_backlog(db, notebook_id, 0) == ("knowledge_objects", 2)
    with runtime.database.write() as db:
        assert store.drain_notebook_graph_rows_page(
            db, notebook_id, "knowledge_objects", 3
        )["knowledge_objects"] == 1
    with runtime.database.connect() as db:
        assert store.graph_drain_backlog(db, notebook_id, 0) is None
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"] == 0

    with pytest.raises(ValueError, match="unknown graph drain table"):
        with runtime.database.write() as db:
            store.drain_notebook_graph_rows_page(db, notebook_id, "users", 1)


@pytest.mark.postgres_integration
def test_delete_notebook_kg_drains_then_resets_on_postgres(
    postgres_repository, monkeypatch,
):
    """端到端等价:超阈值图走排水后,终局效果与未排水逐字节同契约——用户文档
    对象清空、memory 投影保留、counts 报全量、epoch+1 seq 0。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="drain-e2e")
    ).id
    _seed_graph(postgres_repository, notebook_id, doc_objects=10, memory_objects=2)
    runtime = postgres_repository._runtime

    pages = []
    store = runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, table, limit):
        deleted = original(db, nb, table, limit)
        pages.append((table, deleted))
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    counts = postgres_repository.delete_notebook_kg(notebook_id)

    assert pages, "超阈值的图必须走排水"
    assert all(d.get("knowledge_objects", 0) <= 3 for _t, d in pages)
    assert counts["knowledge_objects"] == 10
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s "
            "AND source_id='src-doc'", (notebook_id,),
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s "
            "AND source_id='src-mem'", (notebook_id,),
        ).fetchone()["c"] == 2
        state = runtime.unified_kg.state_row(db, notebook_id)
    assert state["kg_mutation_seq"] == 0
    assert state["kg_reset_epoch"] >= 1
