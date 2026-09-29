"""笔记本拷贝「不带 Memory」(M2,E5-1)在真 PostgreSQL 上的行为与计划契约。

`tests/test_notebook_share_copy.py` 里 SQLite 那组用例的镜像——**吃同一份世界**
(`tests/copy_memory_cases.py`)、走同一批真路由(`/notebooks/{id}/share`、`/shared/{token}`、
`/shared/{token}/copy`)与同一个 `repo.copy_notebook`:

* 两名成员各有已确认 Memory(元素、元素向量、KG 对象、对象向量、关系、关系向量、事实三表、
  簇成员),整本拷贝与分享链接拷贝的副本里一行都没有;
* 混合端点关系(一端 Memory 对象、一端共享对象)不让拷贝失败;
* 副本被标脏以便重建成簇;`validate_copy` 在两侧同谓词下通过;
* 没有 Memory 的笔记本,快照与 M2 之前的语句原文逐行逐序一致,副本不被标脏;
* 被改动的每条快照语句配 EXPLAIN pin:在「目标笔记本只占各大表一小片」的数据上,新计划对
  `knowledge_relations` / `source_elements` 等大表的扫描形态与 M2 之前的原文一致(没有新增
  的顺序扫描),Memory 谓词是去相关的反连接而不是逐行相关子查询。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from tests import copy_memory_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_copy_memory_exclusion"),
]


@pytest.fixture
def pg_app(postgres_scope, monkeypatch):
    """真应用 + 真 PostgreSQL 仓库(隔离 schema)。返回 (repo, client)。"""
    from app.api import deps
    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    deps.repository.cache_clear()
    repo = deps.repository()
    try:
        yield repo, TestClient(app)
    finally:
        deps.repository.cache_clear()
        get_settings.cache_clear()
        repo.close()


def _seed(repo, nb=cases.NOTEBOOK, *, with_memory=True):
    def insert(table, values):
        values = dict(values)
        if table == "users":
            values.update(
                username=values["id"], password_hash="", password_salt="", password_iterations=0
            )
        columns = list(values)
        params = [
            Jsonb(values[c]) if c in cases.JSON_COLUMNS else values[c] for c in columns
        ]
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('%s' for _ in columns)})",
            params,
        )

    with repo._runtime.database.write() as db:
        cases.seed(insert, nb, with_memory=with_memory)


def _fetch(repo):
    def fetch(sql, params):
        with repo._runtime.database.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    return fetch


def _kg_state(repo, nb):
    rows = _fetch(repo)(
        "SELECT dirty, kg_mutation_seq FROM unified_kg_state WHERE notebook_id=%s", (nb,)
    )
    return rows[0] if rows else None


def _mk_user(repo, uid):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
            (uid, f"{uid}@example.test", uid, cases.NOW, cases.NOW, uid),
        )


def test_deep_copy_of_a_notebook_with_memories_carries_none_of_them(pg_app):
    repo, _client = pg_app
    _seed(repo)
    _mk_user(repo, "user-deep-copy")
    fetch = _fetch(repo)
    assert cases.read_copy(fetch, "%s", cases.NOTEBOOK).counts != cases.expected_copy_counts()

    new = repo.copy_notebook(cases.NOTEBOOK, new_owner_id="user-deep-copy")

    cases.assert_copy_has_no_memory(cases.read_copy(fetch, "%s", new.id))
    repo._runtime.sharing_store.validate_copy(cases.NOTEBOOK, new.id)
    state = _kg_state(repo, new.id)
    assert state is not None and state["dirty"] is not None and int(state["dirty"]) == 1
    source = cases.read_copy(fetch, "%s", cases.NOTEBOOK)
    assert source.counts["sources"] == 4 and source.counts["concept_clusters"] == 7
    assert _kg_state(repo, cases.NOTEBOOK) is None


def test_share_link_copy_route_of_a_notebook_with_memories_carries_none_of_them(pg_app):
    repo, client = pg_app
    _seed(repo)
    shared = client.post(f"/api/notebooks/{cases.NOTEBOOK}/share")
    assert shared.status_code == 200 and shared.json()["copyable"] is True
    token = shared.json()["share_token"]
    preview = client.get(f"/api/shared/{token}")
    assert preview.status_code == 200
    # 预览的节点/边数与拷贝同口径:不含成员 Memory 派生的 8-4 个对象、5-4 条关系
    assert (preview.json()["node_count"], preview.json()["edge_count"]) == (4, 1)
    assert preview.json()["size"]["nodes"] == 4 and preview.json()["size"]["edges"] == 1
    assert (shared.json()["size"]["nodes"], shared.json()["size"]["edges"]) == (4, 1)
    assert repo._runtime.sharing_store.memory_derived_kg_counts(cases.NOTEBOOK) == (4, 4)

    copied = client.post(f"/api/shared/{token}/copy")
    assert copied.status_code == 200, copied.text
    new_id = copied.json()["id"]

    cases.assert_copy_has_no_memory(cases.read_copy(_fetch(repo), "%s", new_id))
    state = _kg_state(repo, new_id)
    assert state is not None and int(state["dirty"]) == 1


def test_copy_of_a_notebook_without_memory_is_unchanged_and_not_marked_dirty(
    pg_app, monkeypatch
):
    """没有 Memory 的笔记本:快照的每一行都与 M2 之前的语句原文一致,有 ORDER BY 的三张表
    连顺序也一致;副本仍然没有 unified_kg_state 行——这条路径字节不变。"""
    from app.repositories.postgres import sharing_store as store_module

    repo, _client = pg_app
    _seed(repo, cases.NOTEBOOK_PLAIN, with_memory=False)
    store = repo._runtime.sharing_store
    now_rows = store.snapshot_copy_rows(cases.NOTEBOOK_PLAIN)
    assert all(now_rows[t] for t in cases.LEGACY_SNAPSHOT_PG), "fixture covers each table"
    assert store_module._MEMORY_EXCLUDED_MARK not in now_rows["notebooks"][0]
    with monkeypatch.context() as patch:
        patch.setattr(
            store_module,
            "_COPY_SNAPSHOT_QUERIES",
            cases.legacy_snapshot_queries(
                store_module._COPY_SNAPSHOT_QUERIES, cases.LEGACY_SNAPSHOT_PG
            ),
        )
        legacy = store.snapshot_copy_rows(cases.NOTEBOOK_PLAIN)
    cases.assert_snapshots_equal(now_rows, legacy, ordered=cases.PG_ORDERED_TABLES)

    _mk_user(repo, "user-plain-copy")
    new = repo.copy_notebook(cases.NOTEBOOK_PLAIN, new_owner_id="user-plain-copy")
    assert _kg_state(repo, new.id) is None
    fetch = _fetch(repo)
    assert cases.read_copy(fetch, "%s", new.id).counts == (
        cases.read_copy(fetch, "%s", cases.NOTEBOOK_PLAIN).counts
    )


def test_memory_notebook_snapshot_is_the_legacy_snapshot_minus_the_memory_rows(
    pg_app, monkeypatch
):
    from app.repositories.postgres import sharing_store as store_module

    repo, _client = pg_app
    _seed(repo)
    store = repo._runtime.sharing_store
    now_rows = store.snapshot_copy_rows(cases.NOTEBOOK)
    with monkeypatch.context() as patch:
        patch.setattr(
            store_module,
            "_COPY_SNAPSHOT_QUERIES",
            cases.legacy_snapshot_queries(
                store_module._COPY_SNAPSHOT_QUERIES, cases.LEGACY_SNAPSHOT_PG
            ),
        )
        legacy = store.snapshot_copy_rows(cases.NOTEBOOK)
    assert len(legacy["sources"]) == 4 and len(legacy["knowledge_relations"]) == 5
    assert {r["id"] for r in now_rows["sources"]} == {"src-doc"}
    assert {r["id"] for r in now_rows["knowledge_relations"]} == {"kr-shared"}
    assert {r["id"] for r in now_rows["knowledge_objects"]} == {
        "ko-shared-1", "ko-shared-2", "ko-shared-3", "ko-shared-4",
    }
    assert {r["canonical_id"] for r in now_rows["concept_clusters"]} == {"K-shared"}
    assert now_rows["notebooks"][0][store_module._MEMORY_EXCLUDED_MARK] is True


def test_validate_copy_compares_like_with_like_and_still_detects_a_short_copy(pg_app):
    """两侧同谓词:含 Memory 的源库与不含 Memory 的副本行数「相等」;副本少一行仍然报错。"""
    repo, _client = pg_app
    _seed(repo)
    _mk_user(repo, "user-validate")
    new = repo.copy_notebook(cases.NOTEBOOK, new_owner_id="user-validate")
    store = repo._runtime.sharing_store
    store.validate_copy(cases.NOTEBOOK, new.id)
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM chunks WHERE notebook_id=%s", (new.id,))
    with pytest.raises(RuntimeError, match="chunks"):
        store.validate_copy(cases.NOTEBOOK, new.id)


def test_share_preview_of_a_notebook_without_memory_counts_every_row(pg_app):
    repo, client = pg_app
    _seed(repo, cases.NOTEBOOK_PLAIN, with_memory=False)
    shared = client.post(f"/api/notebooks/{cases.NOTEBOOK_PLAIN}/share")
    assert shared.status_code == 200
    preview = client.get(f"/api/shared/{shared.json()['share_token']}").json()
    assert (preview["node_count"], preview["edge_count"]) == (4, 1)
    store = repo._runtime.sharing_store
    assert store.memory_derived_kg_counts(cases.NOTEBOOK_PLAIN) == (0, 0)


def test_snapshot_table_set_is_pinned_and_memory_carriers_are_not_in_it():
    from app.repositories.postgres import sharing_store as store_module

    tables = {table for table, _query in store_module._COPY_SNAPSHOT_QUERIES}
    assert tables == cases.SNAPSHOT_TABLES
    assert not cases.MEMORY_CARRIERS_NOT_COPIED & tables


# ------------------------------------------------------------------ EXPLAIN pins
_BIG_TABLES = (
    "knowledge_relations", "source_elements", "chunks", "knowledge_objects",
    "concept_clusters", "element_embeddings", "chunk_embeddings", "knowledge_embeddings",
    "relation_embeddings", "chunk_questions", "sources",
)


def _seed_scale(database) -> None:
    """目标笔记本 nb-big 只占各大表约 1/20;含 Memory 来源(前 40 个)及其派生行。"""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','a@x.test','a','user','active',%s,%s,'u-a','','',0) "
            "ON CONFLICT DO NOTHING",
            (cases.NOW, cases.NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) SELECT CASE WHEN g=0 THEN 'nb-big' ELSE 'nb-'||g END,"
            "'N','','','ready','u-a',%s,%s,'personal' FROM generate_series(0,19) g",
            (cases.NOW, cases.NOW),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,content_md,"
            "created_at,updated_at) SELECT 'mem-'||g,'nb-big','u-a','ask_answer','confirmed',"
            "'t','x',%s,%s FROM generate_series(0,39) g",
            (cases.NOW, cases.NOW),
        )
        # 20 笔记本 × 400 来源;笔记本 g 的第 k 个来源 id = src-g-k;nb-big = 笔记本 0。
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,updated_at) "
            "SELECT 'src-'||n||'-'||k,CASE WHEN n=0 THEN 'nb-big' ELSE 'nb-'||n END,'t',"
            "CASE WHEN n=0 AND k<40 THEN 'memory' ELSE 'document' END,"
            "CASE WHEN n=0 AND k<40 THEN 'mem-'||k END,%s,%s "
            "FROM generate_series(0,19) n, generate_series(0,399) k",
            (cases.NOW, cases.NOW),
        )
        nb = "CASE WHEN n=0 THEN 'nb-big' ELSE 'nb-'||n END"
        each = "generate_series(0,19) n, generate_series(0,3999) g"
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) "
            f"SELECT 'el-'||n||'-'||g,'src-'||n||'-'||(g%%400),'para','p','t',%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO element_embeddings(element_id,source_id,notebook_id,vector,created_at) "
            f"SELECT 'el-'||n||'-'||g,'src-'||n||'-'||(g%%400),{nb},'\\x00'::bytea,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) "
            f"SELECT 'ck-'||n||'-'||g,{nb},'src-'||n||'-'||(40+g%%360),'t','[]'::jsonb,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
            f"SELECT 'ck-'||n||'-'||g,{nb},'\\x00'::bytea,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO chunk_questions(id,chunk_id,notebook_id,source_id,question,vector,created_at) "
            f"SELECT 'cq-'||n||'-'||g,'ck-'||n||'-'||g,{nb},'src-'||n||'-'||(40+g%%360),'q',"
            f"'\\x00'::bytea,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,payload,"
            "evidence,created_at,updated_at) "
            f"SELECT 'ko-'||n||'-'||g,{nb},'concept','approved','src-'||n||'-'||(g%%400),"
            f"'{{}}'::jsonb,'[]'::jsonb,%s,%s FROM {each}",
            (cases.NOW, cases.NOW),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            f"SELECT 'ko-'||n||'-'||g,{nb},'\\x00'::bytea,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) "
            f"SELECT 'kr-'||n||'-'||g,{nb},'src-'||n||'-'||(g%%400),'ko-'||n||'-'||g,"
            f"'ko-'||n||'-'||((g*7+1)%%4000),'r','[]'::jsonb,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO relation_embeddings(relation_id,notebook_id,vector,created_at) "
            f"SELECT 'kr-'||n||'-'||g,{nb},'\\x00'::bytea,%s FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            f"SELECT 'cc-'||n||'-'||g,{nb},'K-'||n||'-'||(g/3),'ko-'||n||'-'||g,'n','concept',%s,0 "
            f"FROM {each}",
            (cases.NOW,),
        )
        db.execute(
            "INSERT INTO knowledge_source_facts(id,notebook_id,source_id,source_generation,"
            "local_object_id,object_type,created_at,updated_at) "
            f"SELECT 'f-'||n||'-'||g,{nb},'src-'||n||'-'||(g%%400),'gen','l'||g,'concept',%s,%s "
            f"FROM generate_series(0,19) n, generate_series(0,1999) g",
            (cases.NOW, cases.NOW),
        )
        for table in _BIG_TABLES + ("knowledge_source_facts", "memory_items"):
            db.execute(f"ANALYZE {table}")
    import psycopg

    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        for table in _BIG_TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


_SCAN = re.compile(r"(Seq Scan|Index Only Scan|Index Scan|Bitmap Heap Scan) (?:using \S+ )?on (\w+)")


def _scans(plan: str) -> dict[str, str]:
    """table -> scan kind of its FIRST occurrence in the plan text. The driving (outer) scan
    is printed before any inner probe of the same table, so this is the scan that reads the
    notebook's rows, not the correlated probes the Memory predicate adds."""
    first: dict[str, str] = {}
    for kind, table in _SCAN.findall(plan):
        first.setdefault(table, kind)
    return first


def _seq_scanned(plan: str) -> set[str]:
    return {table for kind, table in _SCAN.findall(plan) if kind == "Seq Scan"}


def _plan(database, query: str) -> str:
    with database.connect() as db:
        rows = db.execute(f"EXPLAIN (COSTS OFF) {query}", ("nb-big",)).fetchall()
    return "\n".join(str(r["QUERY PLAN"]) for r in rows)


#: 每条快照语句的驱动表(外层按笔记本取行的那张表)。
_DRIVING_TABLE = {
    "source_paper_meta": "source_paper_meta", "source_authors": "source_authors",
    "knowledge_source_facts": "knowledge_source_facts",
    "knowledge_source_fact_elements": "knowledge_source_fact_elements",
    "knowledge_source_fact_backfills": "knowledge_source_fact_backfills",
}


_UNHASHED_SUBPLAN_USE = re.compile(r"(?<!hashed )SubPlan \d+")


def _correlated_subplans(plan: str) -> int:
    """Uses of a per-row (not hashed) SubPlan. The `SubPlan N` definition lines that head an
    uncorrelated subplan's body do not count; a hashed SubPlan is evaluated once."""
    return sum(
        len(_UNHASHED_SUBPLAN_USE.findall(line))
        for line in plan.splitlines()
        if not line.strip().startswith("SubPlan")
    )


def test_changed_snapshot_statements_keep_their_scan_shape_and_use_anti_joins(
    postgres_database,
):
    """新增的 Memory 谓词不许把大表的扫描改成新的顺序扫描:对每条被改动的快照语句,驱动表的
    **扫描种类**与 M2 之前的语句原文在同一份数据(目标笔记本只占各大表约 1/20)上给出的计划
    一致,且任何大表(`knowledge_relations` / `source_elements` 在内)都不许出现新的 Seq Scan;Memory 谓词渲染
    成去相关的反连接,而不是逐行执行的相关子查询(原本就有的 knowhow `hashed SubPlan` 除外)。"""
    from app.repositories.postgres import sharing_store as store_module
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate()
    _seed_scale(postgres_database)
    new_queries = dict(store_module._COPY_SNAPSHOT_QUERIES)
    for table, legacy_sql in cases.LEGACY_SNAPSHOT_PG.items():
        new_plan = _plan(postgres_database, new_queries[table])
        legacy_plan = _plan(postgres_database, legacy_sql)
        driving = _DRIVING_TABLE.get(table, table)
        new_kind = _scans(new_plan).get(driving)
        legacy_kind = _scans(legacy_plan).get(driving)
        assert new_kind == legacy_kind, (
            f"{table}: the scan of {driving} changed from {legacy_kind} to {new_kind}\n"
            f"--- new\n{new_plan}\n--- legacy\n{legacy_plan}"
        )
        assert _correlated_subplans(new_plan) <= _correlated_subplans(legacy_plan), new_plan
        # 任何位置的顺序扫描都不许是新增的(大表)
        assert (_seq_scanned(new_plan) & set(_BIG_TABLES)) <= _seq_scanned(legacy_plan), (
            f"{table}: a new sequential scan\n--- new\n{new_plan}\n--- legacy\n{legacy_plan}"
        )
    # Memory 谓词是「集合只建一次」的形态:反连接或哈希子计划,而不是逐行相关子查询。
    # 关系语句:原有的 knowhow 集合 + Memory 三条臂(自身来源 / 两个端点)= 至少 4 个哈希子计划。
    relations_plan = _plan(postgres_database, new_queries["knowledge_relations"])
    assert relations_plan.count("hashed SubPlan") >= 4, relations_plan
    for table in ("chunks", "knowledge_objects", "concept_clusters", "relation_embeddings"):
        plan = _plan(postgres_database, new_queries[table])
        assert "Anti Join" in plan or "hashed SubPlan" in plan, (table, plan)
