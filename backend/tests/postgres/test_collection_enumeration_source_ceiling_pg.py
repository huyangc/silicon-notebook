"""PR-B(B2/B3/B4)的 PostgreSQL 孪生:真 ``PostgresRepository``、真 store 下推。

SQLite 侧的全量矩阵在 ``tests/test_collection_enumeration_source_ceiling.py``;这里
只钉必须真 PG 才能证的那几件——``count_knowledge(supported_by_source_ids=...)`` 与
``knowledge_object_page_rows(allowed_source_ids=...)`` 在真 psycopg 连接上给出同一个
集合(地图分母 == 清单条数、``complete``)、截断前先按天花板过滤的证据引用、
天花板外的上传不触发 ``concurrent_change``、引用只取天花板内且存活的元素,以及不装
天花板时的旧口径。

⚠ 依赖 store 侧(PR-B·B1)两个关键字参数,以及 ``repository_runtime`` 给
``CollectionCatalogService`` 接上 ``knowledge=seats.knowledge``。
"""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp
from app.services.collection_enumeration import EnumerationBudget
from app.services.source_scope import source_scope_context


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_collection_source_ceiling"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _budget(**overrides):
    base = dict(page_size=25, max_rows=1_000, max_pages=50,
                max_payload_chars=256_000)
    base.update(overrides)
    return EnumerationBudget(**base)


def _seed(repository):
    """sA、sC 勾选,sB 不勾;oA(sA)、oB(sB)、oMix(前三条 sB、第四条 sA)、
    oNone(无证据)。"""
    runtime = repository._runtime
    notebook_id = repository.create_notebook(NotebookCreate(name="ceiling")).id
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for source_id, formulas in (("sA", 2), ("sB", 3), ("sC", 1)):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,%s,'markdown','extracted','extracted','a.md','',0,"
                "%s,'',%s,%s,'')",
                (source_id, notebook_id, f"标题-{source_id}", f"h-{source_id}",
                 now, now),
            )
            for index in range(1, formulas + 1):
                db.execute(
                    "INSERT INTO source_elements "
                    "(id,source_id,element_type,location_label,text,metadata,"
                    "created_at) VALUES (%s,%s,'formula',%s,%s,%s,%s)",
                    (f"el-{source_id}-{index:03d}", source_id, f"p{index}",
                     f"body {source_id} {index}", jsonb({}), now),
                )
        objects = {
            "oA": [("sA", "el-sA-001")],
            "oB": [("sB", "el-sB-001")],
            "oMix": [("sB", "el-sB-001"), ("sB", "el-sB-002"),
                     ("sB", "el-sB-003"), ("sA", "el-sA-002")],
            "oNone": [],
        }
        for object_id, evidence in objects.items():
            evidence_rows = [
                {"source_id": source_id, "element_id": element_id,
                 "element_type": "formula", "location_label": "p1",
                 "quoted_span": "q", "confidence": 1.0}
                for source_id, element_id in evidence
            ]
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,payload,evidence,source_id,"
                "created_at,updated_at) VALUES (%s,%s,'concept','approved',%s,%s,"
                "'',%s,%s)",
                (object_id, notebook_id, jsonb({"name": object_id}),
                 jsonb(evidence_rows), now, now),
            )
            runtime.knowledge.replace_object_sources(
                db, object_id, notebook_id, evidence_rows
            )
        runtime.unified_kg.mark_dirty(db, notebook_id, now)
    repository.collection_catalog.invalidate()
    return notebook_id


def _insert_late_source(repository, notebook_id, source_id):
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,created_at,"
            "updated_at,doc_type) VALUES (%s,%s,'late','markdown',"
            "'extracted','extracted','l.md','',0,%s,'',%s,%s,'')",
            (source_id, notebook_id, f"h-{source_id}", now, now),
        )


def _ticked(notebook_id, source_ids):
    return source_scope_context(notebook_id, {
        "mode": "include", "source_ids": list(source_ids), "narrowed": True,
    })


def test_pg_catalog_is_composed_with_the_knowledge_store(postgres_repository):
    assert (postgres_repository.collection_catalog._knowledge
            is postgres_repository._runtime.knowledge)


def test_pg_enumeration_under_a_ceiling_counts_what_it_lists(postgres_repository):
    repository = postgres_repository
    notebook_id = _seed(repository)
    enumeration = repository.collection_enumeration
    with _ticked(notebook_id, ["sA", "sC"]):
        collection_map = repository.collection_catalog.collection_map(notebook_id)
        kg = enumeration.enumerate_kg_objects(
            notebook_id, "concept", budget=_budget())
        paged = enumeration.enumerate_kg_objects(
            notebook_id, "concept", budget=_budget(page_size=1, max_rows=1))
        resumed = enumeration.enumerate_kg_objects(
            notebook_id, "concept", budget=_budget(page_size=1, max_rows=1),
            cursor=paged.cursor)
        elements = enumeration.enumerate_elements(
            notebook_id, "formula", budget=_budget())
        with pytest.raises(ValueError, match="source is not in scope: 'sB'"):
            enumeration.enumerate_elements(
                notebook_id, "formula", source_id="sB", budget=_budget())
        roster = enumeration.enumerate_sources(notebook_id, budget=_budget())
        citations = repository._runtime.ask_service().evidence_context \
            .collection_item_citations(kg.items, active_notebook_id=notebook_id)

    assert dict(collection_map.kg_objects)["concept"] == 2
    assert collection_map.element_count("formula") == 3
    assert collection_map.sources == 2
    assert sorted(item.object_id for item in kg.items) == ["oA", "oMix"]
    assert (kg.coverage.total, kg.coverage.complete) == (2, True)
    assert paged.cursor is not None
    assert resumed.coverage.returned_total == 2 and resumed.coverage.complete
    mix = next(item for item in kg.items if item.object_id == "oMix")
    assert mix.evidence_element_ids == ("el-sA-002",)
    assert citations["oMix"].element_id == "el-sA-002"
    assert {item.source_id for item in elements.items} == {"sA", "sC"}
    assert (elements.coverage.total, elements.coverage.complete) == (3, True)
    assert [item.source_id for item in roster.items] == ["sA", "sC"]


def test_pg_out_of_ceiling_upload_is_not_a_concurrent_change(
    postgres_repository, monkeypatch,
):
    repository = postgres_repository
    notebook_id = _seed(repository)
    runtime = repository._runtime
    store = runtime.source_store
    original = store.element_page_rows
    state = {"pages": 0}

    def hook(*args, **kwargs):
        rows = original(*args, **kwargs)
        state["pages"] += 1
        if state["pages"] == 1:
            _insert_late_source(repository, notebook_id, "sLate")
        return rows

    monkeypatch.setattr(store, "element_page_rows", hook)
    with _ticked(notebook_id, ["sA", "sC"]):
        result = repository.collection_enumeration.enumerate_elements(
            notebook_id, "formula", budget=_budget(page_size=1))
    assert state["pages"] > 1
    assert result.coverage.complete is True
    assert result.coverage.returned_total == result.coverage.total == 3


def test_pg_without_a_ceiling_keeps_the_historical_numbers(postgres_repository):
    repository = postgres_repository
    notebook_id = _seed(repository)
    collection_map = repository.collection_catalog.collection_map(notebook_id)
    kg = repository.collection_enumeration.enumerate_kg_objects(
        notebook_id, "concept", budget=_budget())
    assert dict(collection_map.kg_objects)["concept"] == 4
    assert collection_map.element_count("formula") == 6
    assert sorted(item.object_id for item in kg.items) == [
        "oA", "oB", "oMix", "oNone"]
    mix = next(item for item in kg.items if item.object_id == "oMix")
    assert mix.evidence_element_ids == ("el-sB-001", "el-sB-002", "el-sB-003")


def test_pg_per_library_freeze_hides_a_later_upload(postgres_repository):
    """E-3 孪生:逐库冻结之后上传的来源不进来源清单,也不进地图的 ``sources:``。"""
    repository = postgres_repository
    notebook_id = _seed(repository)
    with source_scope_context(
        notebook_id, None, None,
        notebook_source_ceilings={notebook_id: ["sA", "sB", "sC"]},
    ):
        _insert_late_source(repository, notebook_id, "sLate")
        collection_map = repository.collection_catalog.collection_map(notebook_id)
        roster = repository.collection_enumeration.enumerate_sources(
            notebook_id, budget=_budget())
    assert collection_map.sources == 3
    assert [item.source_id for item in roster.items] == ["sA", "sB", "sC"]
    assert roster.coverage.complete is True


def test_pg_knowhow_table_count_is_what_the_executor_reaches(postgres_repository):
    """E-5(b) 孪生:只数当前库的表;收窄时为 0。"""
    repository = postgres_repository
    notebook_id = repository.create_notebook(NotebookCreate(name="nb")).id
    base_id = repository.create_notebook(NotebookCreate(name="base")).id
    repository.mark_notebook_base(base_id)
    columns = [{"name": "Topic", "role": "anchor"}]
    repository.create_knowhow_table(notebook_id, "T1", "", columns)
    repository.create_knowhow_table(base_id, "T2", "", columns)
    repository.replace_notebook_bases(
        notebook_id, [base_id], repository.current_user().id)
    catalog = repository.collection_catalog
    assert catalog.collection_map(notebook_id).knowhow_tables == 1
    with _ticked(notebook_id, []):
        assert catalog.collection_map(notebook_id).knowhow_tables == 0
