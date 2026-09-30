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


def _insert_rows(repo, rows):
    with repo._runtime.database.write() as db:
        for row in rows:
            values = dict(row.values)
            if row.table == "users":
                values.update(
                    username=values["id"], password_hash="", password_salt="",
                    password_iterations=0,
                )
            columns = list(values)
            db.execute(
                f"INSERT INTO {row.table} ({','.join(columns)}) "
                f"VALUES ({','.join('%s' for _ in columns)})",
                [Jsonb(values[c]) if c in cases.JSON_COLUMNS else values[c] for c in columns],
            )


def test_deep_copy_of_a_notebook_with_memories_carries_none_of_them(pg_app):
    repo, _client = pg_app
    _seed(repo)
    _mk_user(repo, "user-deep-copy")
    fetch = _fetch(repo)
    assert cases.read_copy(fetch, "%s", cases.NOTEBOOK).counts != cases.expected_copy_counts()

    new = repo.copy_notebook(cases.NOTEBOOK, new_owner_id="user-deep-copy")

    view = cases.read_copy(fetch, "%s", new.id)
    cases.assert_copy_has_no_memory(view)
    cases.assert_kept_rows_lost_only_the_lent_evidence(view)
    repo._runtime.sharing_store.validate_copy(cases.NOTEBOOK, new.id)
    state = _kg_state(repo, new.id)
    assert state is not None and int(state["dirty"]) == 1
    source = cases.read_copy(fetch, "%s", cases.NOTEBOOK)
    assert source.counts["sources"] == 4 and source.counts["concept_clusters"] == 10
    assert _kg_state(repo, cases.NOTEBOOK) is None


def test_share_link_copy_route_of_a_notebook_with_memories_carries_none_of_them(pg_app):
    repo, client = pg_app
    _seed(repo)
    shared = client.post(f"/api/notebooks/{cases.NOTEBOOK}/share")
    assert shared.status_code == 200 and shared.json()["copyable"] is True
    token = shared.json()["share_token"]
    preview = client.get(f"/api/shared/{token}")
    assert preview.status_code == 200
    cases.assert_share_sizes_exclude_memory(shared.json(), preview.json())
    assert repo._runtime.sharing_store.memory_derived_kg_counts(cases.NOTEBOOK) == (3, 4, 4)
    state_view = client.get(f"/api/notebooks/{cases.NOTEBOOK}/share")
    assert state_view.status_code == 200 and state_view.json()["size"] == preview.json()["size"]

    copied = client.post(f"/api/shared/{token}/copy")
    assert copied.status_code == 200, copied.text
    new_id = copied.json()["id"]

    view = cases.read_copy(_fetch(repo), "%s", new_id)
    cases.assert_copy_has_no_memory(view)
    cases.assert_kept_rows_lost_only_the_lent_evidence(view)
    state = _kg_state(repo, new_id)
    assert state is not None and int(state["dirty"]) == 1
    listing = client.get(f"/api/notebooks/{new_id}/knowledge?type=concept")
    assert listing.status_code == 200 and listing.json()["items"], listing.text
    assert cases.MARK not in listing.text


def test_copy_of_a_clean_notebook_without_memory_runs_the_pre_m2_statements_verbatim(
    pg_app, monkeypatch
):
    """没有 Memory、也不脏的笔记本:快照、上限计数与 validate_copy 用 M2 之前的语句原文;把
    Memory 那套语句换成执行即报错的文本,拷贝照样成功;副本没有 unified_kg_state 行。"""
    from app.repositories.postgres import sharing_store as store_module

    current = dict(store_module._COPY_SNAPSHOT_QUERIES)
    for table, legacy in cases.LEGACY_SNAPSHOT_PG.items():
        assert current[table] == legacy, table
    assert dict(store_module._COPY_VALIDATED_TABLES) == cases.LEGACY_VALIDATED_PG

    repo, _client = pg_app
    _seed(repo, cases.NOTEBOOK_PLAIN, with_memory=False)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at) "
            "VALUES (%s, 0, 3, %s)", (cases.NOTEBOOK_PLAIN, cases.NOW),
        )
    monkeypatch.setattr(
        store_module, "_MEMORY_COPY_SNAPSHOT_QUERIES",
        cases.poisoned(store_module._MEMORY_COPY_SNAPSHOT_QUERIES),
    )
    monkeypatch.setattr(
        store_module, "_MEMORY_COPY_VALIDATED_TABLES",
        cases.poisoned(store_module._MEMORY_COPY_VALIDATED_TABLES),
    )
    store = repo._runtime.sharing_store
    assert store.snapshot_copy_within_limits(cases.NOTEBOOK_PLAIN)
    assert store_module._COPY_DIRTY_MARK not in (
        store.snapshot_copy_rows(cases.NOTEBOOK_PLAIN)["notebooks"][0]
    )
    _mk_user(repo, "user-plain-copy")
    new = repo.copy_notebook(cases.NOTEBOOK_PLAIN, new_owner_id="user-plain-copy")
    assert _kg_state(repo, new.id) is None
    fetch = _fetch(repo)
    assert cases.read_copy(fetch, "%s", new.id).counts == (
        cases.read_copy(fetch, "%s", cases.NOTEBOOK_PLAIN).counts
    )


def test_copy_of_a_dirty_notebook_without_memory_starts_dirty(pg_app):
    repo, _client = pg_app
    _seed(repo, cases.NOTEBOOK_PLAIN, with_memory=False)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at) "
            "VALUES (%s, 1, 5, %s)", (cases.NOTEBOOK_PLAIN, cases.NOW),
        )
    _mk_user(repo, "user-dirty-copy")
    new = repo.copy_notebook(cases.NOTEBOOK_PLAIN, new_owner_id="user-dirty-copy")
    state = _kg_state(repo, new.id)
    assert state is not None and int(state["dirty"]) == 1 and int(state["kg_mutation_seq"]) == 1
    fetch = _fetch(repo)
    copied = cases.read_copy(fetch, "%s", new.id).counts
    source = cases.read_copy(fetch, "%s", cases.NOTEBOOK_PLAIN).counts
    # 脏源库的成簇已过时:副本不带簇行,其余逐表相同。
    assert source["concept_clusters"] > 0 and copied["concept_clusters"] == 0
    assert {**copied, "concept_clusters": 0} == {**source, "concept_clusters": 0}


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
            store_module, "_MEMORY_COPY_SNAPSHOT_QUERIES", store_module._COPY_SNAPSHOT_QUERIES
        )
        patch.setattr(store_module, "strip_memory_evidence", lambda *_args: 0)
        legacy = store.snapshot_copy_rows(cases.NOTEBOOK)
    assert len(legacy["sources"]) == 4 and len(legacy["knowledge_relations"]) == 5
    assert {r["id"] for r in now_rows["sources"]} == {"src-doc"}
    assert {r["id"] for r in now_rows["knowledge_relations"]} == {"kr-shared"}
    assert {r["id"] for r in now_rows["knowledge_objects"]} == cases.SHARED_OBJECTS
    assert {r["canonical_id"] for r in now_rows["concept_clusters"]} == {"K-shared"}
    assert now_rows["notebooks"][0][store_module._COPY_DIRTY_MARK] == store_module._HOLDS_MEMORY
    cases.assert_snapshot_is_legacy_minus_memory(now_rows, legacy)


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
    assert (preview["node_count"], preview["edge_count"]) == (6, 1)
    assert preview["size"]["sources"] == preview["source_count"] == 1
    assert repo._runtime.sharing_store.memory_derived_kg_counts(cases.NOTEBOOK_PLAIN) == (0, 0, 0)


def test_a_memory_object_merged_into_a_shared_one_leaves_no_memory_text_in_the_copy(pg_app):
    from app.models.knowledge import MergeRequest

    repo, client = pg_app
    nb = cases.PROBE_NOTEBOOK
    _insert_rows(repo, cases.probe_world(nb))
    repo.merge_knowledge(nb, "ko-mem-p", MergeRequest(into_id="ko-shared-p"))
    merged = _fetch(repo)(
        "SELECT evidence::text AS e FROM knowledge_objects WHERE id='ko-shared-p'", ()
    )[0]["e"]
    assert cases.PROBE_MEMORY_TEXT in merged, "fixture: the merge carried the text"

    _mk_user(repo, "user-probe-copy")
    new = repo.copy_notebook(nb, new_owner_id="user-probe-copy")
    view = cases.read_copy(_fetch(repo), "%s", new.id)
    cases.assert_copy_objects_carry_no_memory_text(view)
    assert view.counts["knowledge_objects"] == 1

    token = client.post(f"/api/notebooks/{nb}/share").json()["share_token"]
    preview = client.get(f"/api/shared/{token}").json()
    assert preview["size"]["sources"] == preview["source_count"] == 1
    copied = client.post(f"/api/shared/{token}/copy")
    assert copied.status_code == 200, copied.text
    listing = client.get(f"/api/notebooks/{copied.json()['id']}/knowledge?type=concept")
    assert listing.status_code == 200 and listing.json()["items"], listing.text
    assert cases.MARK not in listing.text and "src-mem-p" not in listing.text


@pytest.mark.parametrize("canonical", sorted(cases.STALE_CLUSTER_CASES))
def test_a_cluster_seeded_by_a_since_deleted_memory(pg_app, canonical):
    repo, _client = pg_app
    nb = cases.PROBE_NOTEBOOK
    _insert_rows(repo, cases.probe_world(nb, cluster_canonical=canonical))
    repo.delete_source("src-mem-p")
    source_state = _kg_state(repo, nb)
    assert source_state is not None and int(source_state["dirty"]) == 1
    left = _fetch(repo)(
        "SELECT member_object_id FROM concept_clusters WHERE notebook_id=%s", (nb,)
    )
    assert [r["member_object_id"] for r in left] == ["ko-shared-p"]

    _mk_user(repo, "user-stale-copy")
    new = repo.copy_notebook(nb, new_owner_id="user-stale-copy")
    state = _kg_state(repo, new.id)
    assert state is not None and int(state["dirty"]) == 1
    view = cases.read_copy(_fetch(repo), "%s", new.id)
    assert view.counts["concept_clusters"] == cases.STALE_CLUSTER_CASES[canonical] == 0
    # 副本里任何一张表、任何一列都没有 Memory 的文本(簇名与描述随簇一起没带)。
    assert not [leaf for leaf in view.leaves if cases.MARK in leaf], view.dump


def test_schema_induction_sample_reads_no_memory_element(pg_app):
    repo, _client = pg_app
    _seed(repo)
    sample = repo._runtime.source_store.notebook_element_sample(cases.NOTEBOOK)
    texts = [item["text"] for item in sample]
    assert texts and all(cases.MARK not in text for text in texts), texts
    assert {"shared element el-doc-1", "shared element el-doc-2"} <= set(texts)


def test_share_size_of_a_notebook_without_memory_costs_one_statement(pg_app, monkeypatch):
    """没有 Memory 的笔记本,分享尺寸的 Memory 扣减只跑一条探测就返回 (0, 0, 0)。"""
    repo, _client = pg_app
    _seed(repo, cases.NOTEBOOK_PLAIN, with_memory=False)
    store = repo._runtime.sharing_store
    executed: list[str] = []
    original = store.database.connect

    from contextlib import contextmanager

    @contextmanager
    def counting_connect(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            real = connection.execute

            def execute(query, *rest, **kw):
                executed.append(str(query))
                return real(query, *rest, **kw)

            connection.execute = execute
            try:
                yield connection
            finally:
                connection.execute = real

    monkeypatch.setattr(store.database, "connect", counting_connect)
    assert store.memory_derived_kg_counts(cases.NOTEBOOK_PLAIN) == (0, 0, 0)
    work = [q for q in executed if q.lstrip().upper().startswith("SELECT")]
    assert len(work) == 1, executed


def test_snapshot_table_set_is_pinned_and_memory_carriers_are_not_in_it():
    from app.repositories.postgres import sharing_store as store_module

    for queries in (store_module._COPY_SNAPSHOT_QUERIES, store_module._MEMORY_COPY_SNAPSHOT_QUERIES):
        tables = {table for table, _query in queries}
        assert tables == cases.SNAPSHOT_TABLES
        assert not cases.MEMORY_CARRIERS_NOT_COPIED & tables


def test_shared_by_me_overview_sizes_exclude_memory(pg_app):
    repo, client = pg_app
    _seed(repo)
    token = client.post(f"/api/notebooks/{cases.NOTEBOOK}/share").json()["share_token"]
    preview = client.get(f"/api/shared/{token}").json()
    overview = client.get("/api/notebooks/shared-by-me")
    assert overview.status_code == 200, overview.text
    row = next(item for item in overview.json() if item["id"] == cases.NOTEBOOK)
    assert row["size"] == preview["size"]
    cases.assert_share_sizes_exclude_memory({"size": row["size"]}, preview)


def _race_validate(monkeypatch, before_validate):
    from app.repositories.postgres.sharing_store import SharingStore

    original = SharingStore.validate_copy

    def racing(store_self, source_id, new_id):
        before_validate()
        return original(store_self, source_id, new_id)

    monkeypatch.setattr(SharingStore, "validate_copy", racing)


def _set_source_dirty(repo, nb, dirty):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at) "
            "VALUES (%s, %s, 7, %s) ON CONFLICT (notebook_id) DO UPDATE SET dirty = excluded.dirty",
            (nb, dirty, cases.NOW),
        )


def test_copy_survives_the_source_turning_dirty_mid_copy(pg_app, monkeypatch):
    repo, client = pg_app
    _seed(repo)
    token = client.post(f"/api/notebooks/{cases.NOTEBOOK}/share").json()["share_token"]
    _race_validate(monkeypatch, lambda: _set_source_dirty(repo, cases.NOTEBOOK, 1))
    copied = client.post(f"/api/shared/{token}/copy")
    assert copied.status_code == 200, copied.text
    view = cases.read_copy(_fetch(repo), "%s", copied.json()["id"])
    assert view.counts["concept_clusters"] == cases.expected_copy_counts()["concept_clusters"]


def test_copy_survives_the_source_being_rebuilt_mid_copy(pg_app, monkeypatch):
    repo, client = pg_app
    _seed(repo)
    _set_source_dirty(repo, cases.NOTEBOOK, 1)
    token = client.post(f"/api/notebooks/{cases.NOTEBOOK}/share").json()["share_token"]
    _race_validate(monkeypatch, lambda: _set_source_dirty(repo, cases.NOTEBOOK, 0))
    copied = client.post(f"/api/shared/{token}/copy")
    assert copied.status_code == 200, copied.text
    view = cases.read_copy(_fetch(repo), "%s", copied.json()["id"])
    assert view.counts["concept_clusters"] == 0
    cases.assert_copy_has_no_memory(
        view, expected={**cases.expected_copy_counts(), "concept_clusters": 0}
    )


def test_validate_and_compensate_both_consume_the_copys_cluster_decision(pg_app, monkeypatch):
    from app.repositories.postgres.sharing_store import SharingStore

    repo, _client = pg_app
    _seed(repo)
    _mk_user(repo, "user-decision")
    store = repo._runtime.sharing_store
    repo.copy_notebook(cases.NOTEBOOK, new_owner_id="user-decision")
    assert store._clusters_dropped == {}

    original = SharingStore.insert_copy_rows

    def fail_after_the_root_row(store_self, table, rows, *, chunk_size):
        if table == "knowledge_objects":
            assert store_self._clusters_dropped, "the root row recorded the decision"
            raise RuntimeError("injected failure after the decision was recorded")
        return original(store_self, table, rows, chunk_size=chunk_size)

    monkeypatch.setattr(SharingStore, "insert_copy_rows", fail_after_the_root_row)
    with pytest.raises(RuntimeError, match="injected failure"):
        repo.copy_notebook(cases.NOTEBOOK, new_owner_id="user-decision")
    assert store._clusters_dropped == {}


def test_member_clusters_are_diffed_per_generation(pg_app):
    repo, client = pg_app
    _seed(repo)
    _insert_rows(repo, [
        cases.Row("concept_clusters", {
            "id": "cc-building-K-shared", "notebook_id": cases.NOTEBOOK,
            "canonical_id": "K-shared", "member_object_id": "ko-alice-1",
            "canonical_name": "shared topic", "object_type": "concept",
            "created_at": cases.NOW, "generation": 1,
        }, True),
    ])
    token = client.post(f"/api/notebooks/{cases.NOTEBOOK}/share").json()["share_token"]
    copied = client.post(f"/api/shared/{token}/copy")
    assert copied.status_code == 200, copied.text
    view = cases.read_copy(_fetch(repo), "%s", copied.json()["id"])
    assert view.counts["concept_clusters"] == cases.expected_copy_counts()["concept_clusters"]
    assert {r["canonical_id"] for r in view.rows["concept_clusters"]} == {"K-shared"}


# ------------------------------------------------------------------ EXPLAIN pins
# The distribution the pins run on: the target notebook (nb-big) is a thin slice of
# every big table and holds 40 Memory sources; ONE other notebook (nb-memheavy)
# holds 1,500 Memory sources and 30,000 Memory objects, relations and vectors — far
# more Memory than the target. A Memory set built deployment-wide shows up here as a
# Seq Scan / hashed SubPlan over the big tables; a set built from this notebook does
# not. Every statement production executes on the Memory-aware path is pinned, under
# the custom plan and the generic plan: the snapshot SELECTs, the COUNT wrappers the
# copy-size limit runs, the validate_copy extras, the share-size counts, the probes,
# and the schema-induction sample.
_BIG_TABLES = (
    "knowledge_relations", "source_elements", "chunks", "knowledge_objects",
    "concept_clusters", "element_embeddings", "chunk_embeddings", "knowledge_embeddings",
    "relation_embeddings", "chunk_questions", "sources", "knowledge_source_facts",
    "knowledge_source_fact_elements",
)


def _seed_scale(database) -> None:
    NOW = cases.NOW
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','a@x.test','a','user','active',%s,%s,'u-a','','',0) "
            "ON CONFLICT DO NOTHING", (NOW, NOW))
        # nb-big = notebook 0 (the target, 40 of its 400 sources are Memory);
        # notebook 20 = nb-memheavy (every source is Memory: 3000 sources, 60k objects).
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) SELECT CASE WHEN g=0 THEN 'nb-big' WHEN g=20 THEN "
            "'nb-memheavy' ELSE 'nb-'||g END,'N','','','ready','u-a',%s,%s,'personal' "
            "FROM generate_series(0,20) g", (NOW, NOW))
        nb = "CASE WHEN n=0 THEN 'nb-big' WHEN n=20 THEN 'nb-memheavy' ELSE 'nb-'||n END"
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,content_md,"
            "created_at,updated_at) SELECT 'mem-'||n||'-'||k,"
            f"{nb},'u-a','ask_answer','confirmed','t','x',%s,%s "
            "FROM generate_series(0,20) n, generate_series(0,1499) k "
            "WHERE (n=0 AND k<40) OR n=20", (NOW, NOW))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,updated_at) "
            f"SELECT 's-'||n||'-'||k,{nb},'t',"
            "CASE WHEN (n=0 AND k<40) OR n=20 THEN 'memory' ELSE 'document' END,"
            "CASE WHEN (n=0 AND k<40) OR n=20 THEN 'mem-'||n||'-'||k END,%s,%s "
            "FROM generate_series(0,20) n, generate_series(0,1499) k WHERE n=20 OR k<400",
            (NOW, NOW))
        # per notebook: 4000 rows per table for n<20; nb-memheavy 60k objects/relations.
        each = ("generate_series(0,20) n, generate_series(0,29999) g "
                "WHERE (n<20 AND g<4000) OR n=20")
        src = "'s-'||n||'-'||(CASE WHEN n=20 THEN g%%1500 ELSE g%%400 END)"
        doc_src = "'s-'||n||'-'||(40+g%%360)"
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) "
            f"SELECT 'el-'||n||'-'||g,{src},'para','p','t',%s FROM {each}", (NOW,))
        db.execute(
            "INSERT INTO element_embeddings(element_id,source_id,notebook_id,vector,created_at) "
            f"SELECT 'el-'||n||'-'||g,{src},{nb},'\\x00'::bytea,%s FROM {each}", (NOW,))
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) "
            f"SELECT 'ck-'||n||'-'||g,{nb},{doc_src},'t','[]'::jsonb,%s FROM {each} AND n<20",
            (NOW,))
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
            f"SELECT 'ck-'||n||'-'||g,{nb},'\\x00'::bytea,%s FROM {each} AND n<20", (NOW,))
        db.execute(
            "INSERT INTO chunk_questions(id,chunk_id,notebook_id,source_id,question,vector,"
            f"created_at) SELECT 'cq-'||n||'-'||g,'ck-'||n||'-'||g,{nb},{doc_src},'q',"
            f"'\\x00'::bytea,%s FROM {each} AND n<20", (NOW,))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,payload,"
            f"evidence,created_at,updated_at) SELECT 'ko-'||n||'-'||g,{nb},'concept',"
            f"'approved',{src},'{{}}'::jsonb,'[]'::jsonb,%s,%s FROM {each}", (NOW, NOW))
        db.execute(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            f"SELECT 'ko-'||n||'-'||g,{nb},'\\x00'::bytea,%s FROM {each}", (NOW,))
        span = "CASE WHEN n=20 THEN 30000 ELSE 4000 END"
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            f"target_object_id,edge_type,evidence,created_at) SELECT 'kr-'||n||'-'||g,{nb},"
            f"{src},'ko-'||n||'-'||g,'ko-'||n||'-'||((g*7+1)%%({span})),'r','[]'::jsonb,%s "
            f"FROM {each}", (NOW,))
        db.execute(
            "INSERT INTO relation_embeddings(relation_id,notebook_id,vector,created_at) "
            f"SELECT 'kr-'||n||'-'||g,{nb},'\\x00'::bytea,%s FROM {each}", (NOW,))
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            f"canonical_name,object_type,created_at,generation) SELECT 'cc-'||n||'-'||g,{nb},"
            # nb-big also holds one 600-member cluster ('K-hub') with Memory members:
            # the shape whose per-row member probe was O(cluster size squared).
            "CASE WHEN n=0 AND g<600 THEN 'K-hub' WHEN g%%50=0 THEN 'K-~ko-'||n||'-'||g "
            "ELSE 'K-'||n||'-'||(g/3) END,"
            f"'ko-'||n||'-'||g,'n','concept',%s,0 FROM {each}", (NOW,))
        db.execute(
            "INSERT INTO knowledge_source_facts(id,notebook_id,source_id,source_generation,"
            f"local_object_id,object_type,created_at,updated_at) SELECT 'f-'||n||'-'||g,{nb},"
            f"{src},'gen','l'||g,'concept',%s,%s FROM {each}", (NOW, NOW))
        db.execute(
            "INSERT INTO knowledge_source_fact_elements(fact_id,notebook_id,source_id,"
            f"source_generation,element_id,created_at) SELECT 'f-'||n||'-'||g,{nb},{src},'gen',"
            f"'el-'||n||'-'||g,%s FROM {each}", (NOW,))
        for table in _BIG_TABLES + ("memory_items", "unified_kg_state"):
            db.execute(f"ANALYZE {table}")


_SCAN = re.compile(r"(Seq Scan|Index Only Scan|Index Scan|Bitmap Heap Scan) (?:using \S+ )?on (\w+)")
_UNHASHED_SUBPLAN_USE = re.compile(r"(?<!hashed )SubPlan \d+")

#: Memory anti-joins each Memory-aware statement must plan (a Memory NOT EXISTS that
#: turns into a hashed SubPlan instead is a set built over the whole deployment).
_EXPECTED_ANTI_JOINS = {
    "sources": 0, "source_paper_meta": 0, "source_authors": 0, "source_elements": 0,
    "chunks": 1, "knowledge_objects": 1, "knowledge_source_facts": 1,
    "knowledge_source_fact_elements": 2, "knowledge_source_fact_backfills": 1,
    "knowledge_relations": 3, "chunk_embeddings": 1, "chunk_questions": 2,
    "element_embeddings": 0, "knowledge_embeddings": 1, "relation_embeddings": 3,
    # minted-seed arm + "the source's clustering is current"; the member arm is a set
    # read once (`_MEMORY_MEMBER_KEYS_SQL`), never a per-row probe
    "concept_clusters": 2,
}
#: The validate_copy extras judge per-source tables by their own source_id (an
#: anti-join) where the snapshot joins `sources` (a filter).
_EXPECTED_VALIDATE_ANTI_JOINS = {
    **_EXPECTED_ANTI_JOINS, "source_paper_meta": 1, "source_authors": 1,
}
#: Per-row probes a Memory-aware statement may add over its pre-M2 text: the element
#: probe (element_embeddings, fact elements) and the minted-seed probe (clusters).
_EXTRA_ROW_PROBES = {
    "knowledge_source_fact_elements": 1, "element_embeddings": 1, "concept_clusters": 1,
}


#: How production counts the Memory-aware cluster statements (the size bound and the
#: validate source side): one row per cluster, the Memory-member set subtracted in Python.
_CLUSTER_GROUP_COUNT = (
    "SELECT c.canonical_id, c.generation, COUNT(*) AS n "
    "FROM ({q}) c GROUP BY c.canonical_id, c.generation"
)


def _seq_scanned(plan: str) -> set[str]:
    return {table for kind, table in _SCAN.findall(plan) if kind == "Seq Scan"}


def _correlated_subplans(plan: str) -> int:
    return sum(
        len(_UNHASHED_SUBPLAN_USE.findall(line))
        for line in plan.splitlines()
        if not line.strip().startswith("SubPlan")
    )


def _plans(database, query: str, params: tuple) -> dict[str, str]:
    """EXPLAIN under a custom plan and under a forced generic plan."""
    out = {}
    with database.connect() as db:
        rows = db.execute(f"EXPLAIN (COSTS OFF) {query}", params).fetchall()
        out["custom"] = "\n".join(str(r["QUERY PLAN"]) for r in rows)
        counter = iter(range(1, len(params) + 1))
        numbered = re.sub(r"%s", lambda _m: f"${next(counter)}", query)
        db.execute("SET plan_cache_mode = force_generic_plan")
        db.execute(f"PREPARE pin_stmt AS {numbered}")
        literals = ",".join("'" + str(p).replace("'", "''") + "'" for p in params)
        rows = db.execute(f"EXPLAIN (COSTS OFF) EXECUTE pin_stmt({literals})").fetchall()
        db.execute("DEALLOCATE pin_stmt")
        db.execute("RESET plan_cache_mode")
        out["generic"] = "\n".join(str(r["QUERY PLAN"]) for r in rows)
    return out


def _assert_notebook_scoped(label, new_plan, legacy_plan="", *, anti_joins=None, extra_probes=0):
    """No new Seq Scan on a big table, no hashed SubPlan beyond the pre-M2 text's (the
    knowhow NOT IN), no per-row SubPlan beyond the registered probes, no join compared
    by a filter, and — when given — exactly the expected number of Memory anti-joins."""
    assert (_seq_scanned(new_plan) & set(_BIG_TABLES)) <= _seq_scanned(legacy_plan), (
        f"{label}: a new sequential scan\n--- new\n{new_plan}\n--- legacy\n{legacy_plan}"
    )
    assert new_plan.count("hashed SubPlan") <= legacy_plan.count("hashed SubPlan"), (
        f"{label}: a hashed SubPlan the pre-M2 text did not have\n{new_plan}"
    )
    assert _correlated_subplans(new_plan) <= _correlated_subplans(legacy_plan) + extra_probes, (
        f"{label}: an unregistered per-row SubPlan\n{new_plan}"
    )
    # A nested loop that compares with a Join Filter instead of an index or hash key
    # is quadratic in the notebook (the per-arm NOT EXISTS around relation vectors
    # did 1.44M comparisons at 3,600 x 400).
    assert new_plan.count("Join Filter") <= legacy_plan.count("Join Filter"), (
        f"{label}: a join compared by filter, not by key\n{new_plan}"
    )
    if anti_joins is not None:
        assert new_plan.count("Anti Join") == anti_joins, (
            f"{label}: expected {anti_joins} Memory anti-joins\n{new_plan}"
        )


@pytest.fixture(scope="module")
def scale_database(tmp_path_factory):
    """One seeded scale world for all the pins (module-scoped: seeding is the cost)."""
    import os

    from app.core.config import Settings
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator
    from pathlib import Path

    from tests.postgres.conftest import _isolated_postgres_scope

    base = os.environ.get("TEST_POSTGRES_URL")
    if not base:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with _isolated_postgres_scope(base) as scope:
        database = PostgresDatabase(
            Settings(database_url=scope.url, postgres_statement_timeout_seconds=600),
            Path(__file__).resolve().parents[3],
        )
        try:
            assert PostgresMigrator(database).migrate()
            _seed_scale(database)
            yield database
        finally:
            database.close()


def test_memory_aware_snapshot_statements_and_their_counts_build_every_set_per_notebook(
    scale_database,
):
    from app.repositories.postgres import sharing_store as store_module

    legacy = dict(store_module._COPY_SNAPSHOT_QUERIES)
    for table, query in store_module._MEMORY_COPY_SNAPSHOT_QUERIES:
        if table not in store_module._MEMORY_SNAPSHOT_TEXT:
            assert query == legacy[table]
            continue
        count_wrap = (
            _CLUSTER_GROUP_COUNT if table == "concept_clusters"
            else "SELECT COUNT(*) AS n FROM ({q}) AS _c"
        )
        for wrap in ("{q}", count_wrap):
            new = _plans(scale_database, wrap.format(q=query), ("nb-big",))
            old = _plans(scale_database, wrap.format(q=legacy[table]), ("nb-big",))
            for mode in ("custom", "generic"):
                _assert_notebook_scoped(
                    f"{table} {mode} {wrap[:6]}", new[mode], old[mode],
                    anti_joins=_EXPECTED_ANTI_JOINS[table],
                    extra_probes=_EXTRA_ROW_PROBES.get(table, 0),
                )


def test_memory_aware_validate_extras_build_every_set_per_notebook(scale_database):
    from app.repositories.postgres import sharing_store as store_module

    legacy = dict(store_module._COPY_VALIDATED_TABLES)
    for table, extra in store_module._MEMORY_COPY_VALIDATED_TABLES:
        if table not in store_module._MEMORY_VALIDATED_EXTRAS:
            continue
        statement = f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=%s "
        new = _plans(scale_database, statement + extra, ("nb-big",))
        old = _plans(scale_database, statement + legacy[table], ("nb-big",))
        for mode in ("custom", "generic"):
            _assert_notebook_scoped(
                f"validate {table} {mode}", new[mode], old[mode],
                anti_joins=_EXPECTED_VALIDATE_ANTI_JOINS[table],
                extra_probes=_EXTRA_ROW_PROBES.get(table, 0),
            )


def test_share_size_counts_and_probes_build_every_set_per_notebook(scale_database):
    from app.repositories.postgres import sharing_store as store_module

    for name, params in (
        ("_MEMORY_PRESENT_SQL", ("nb-big",)),
        ("_MEMORY_SOURCES_SQL", ("nb-big",)),
        ("_MEMORY_SOURCE_IDS_SQL", ("nb-big",)),
        ("_COPY_DIRTY_SQL", ("nb-big",) * 2),
        ("_COPY_FILTERED_SQL", ("nb-big",)),
        ("_MEMORY_NODES_SQL", ("nb-big",)),
        ("_MEMORY_EDGES_SQL", ("nb-big",) * 3),
    ):
        plans = _plans(scale_database, getattr(store_module, name), params)
        for mode, plan in plans.items():
            _assert_notebook_scoped(f"{name} {mode}", plan)


def test_schema_induction_sample_keeps_its_scan_shape(scale_database, monkeypatch):
    """The sample statement production executes (captured from the real method), against
    its pre-E5-1 text: no new scan, no new SubPlan."""
    from contextlib import contextmanager

    from app.repositories.postgres.source_store import SourceStore

    captured: list[tuple[str, tuple]] = []
    original = scale_database.connect

    @contextmanager
    def capture(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            real = connection.execute

            def execute(query, params=None, *rest, **kw):
                if "source_elements" in str(query):
                    captured.append((str(query), tuple(params or ())))
                return real(query, params, *rest, **kw)

            connection.execute = execute
            try:
                yield connection
            finally:
                connection.execute = real

    store = SourceStore.__new__(SourceStore)
    store.database = scale_database
    monkeypatch.setattr(scale_database, "connect", capture)
    store.notebook_element_sample("nb-big", max_chars=200)
    monkeypatch.undo()
    query, params = captured[0]
    legacy = re.sub(r"AND NOT \(s\.source_type = 'memory'\) ", "", query)
    assert legacy != query
    new = _plans(scale_database, query, params)
    old = _plans(scale_database, legacy, params)
    for mode in ("custom", "generic"):
        _assert_notebook_scoped(f"sample {mode}", new[mode], old[mode])


def test_child_rows_are_judged_by_their_memory_parent_too(pg_app):
    repo, _client = pg_app
    _seed(repo)
    _insert_rows(repo, cases.ADVERSARIAL_ROWS)
    _mk_user(repo, "user-adv-copy")
    new = repo.copy_notebook(cases.NOTEBOOK, new_owner_id="user-adv-copy")
    cases.assert_copy_has_no_memory(cases.read_copy(_fetch(repo), "%s", new.id))


_LOOPS_ON = re.compile(r"(?:Scan|Scan Backward) (?:using \S+ )?on (\w+).*?loops=(\d+)")


def _max_loops_on(plan: str, table: str) -> int:
    return max((int(n) for t, n in _LOOPS_ON.findall(plan) if t == table), default=0)


def test_cluster_statements_never_probe_a_whole_cluster_per_row(scale_database):
    """P1-1:nb-big 有一个 600 成员、含 Memory 成员的簇。拷贝的簇语句(快照、尺寸计数、validate
    源侧)各只扫一遍 concept_clusters(loops=1);「含 Memory 成员的簇」集合从 Memory 一侧驱动,
    对 concept_clusters 的探测次数至多是本库 Memory 对象数(400),与簇行数(4000)无关。按簇行
    对整簇做相关探测的写法在这里是 loops=簇行数。"""
    from app.repositories.postgres import sharing_store as store_module

    snapshot = dict(store_module._MEMORY_COPY_SNAPSHOT_QUERIES)["concept_clusters"]
    statements = {
        "snapshot": snapshot,
        "size count": _CLUSTER_GROUP_COUNT.format(q=snapshot),
        "validate source": _CLUSTER_GROUP_COUNT.format(q=store_module._MEMORY_CLUSTERS_BASE_SQL),
    }
    with scale_database.connect() as db:
        memory_objects = db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects o JOIN sources s ON s.id = o.source_id "
            "WHERE o.notebook_id = 'nb-big' AND s.source_type = 'memory'"
        ).fetchone()["n"]
        hub = db.execute(
            "SELECT COUNT(*) AS n FROM concept_clusters WHERE canonical_id = 'K-hub'"
        ).fetchone()["n"]
        assert hub >= 500
        for label, query in statements.items():
            rows = db.execute(
                f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) {query}", ("nb-big",)
            ).fetchall()
            plan = "\n".join(str(r["QUERY PLAN"]) for r in rows)
            assert _max_loops_on(plan, "concept_clusters") == 1, (label, plan)
        rows = db.execute(
            f"EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) {store_module._MEMORY_MEMBER_KEYS_SQL}",
            ("nb-big",),
        ).fetchall()
        plan = "\n".join(str(r["QUERY PLAN"]) for r in rows)
        assert _max_loops_on(plan, "concept_clusters") <= memory_objects, plan
        keys = store_module._memory_member_keys(db, "nb-big")
    assert ("K-hub", 0) in keys


def test_memory_member_keys_and_cluster_counts_build_every_set_per_notebook(scale_database):
    from app.repositories.postgres import sharing_store as store_module

    legacy_clusters = _CLUSTER_GROUP_COUNT.format(
        q=dict(store_module._COPY_SNAPSHOT_QUERIES)["concept_clusters"]
    )
    for label, query, legacy in (
        ("member keys", store_module._MEMORY_MEMBER_KEYS_SQL, ""),
        (
            "validate source",
            _CLUSTER_GROUP_COUNT.format(q=store_module._MEMORY_CLUSTERS_BASE_SQL),
            legacy_clusters,
        ),
    ):
        plans = _plans(scale_database, query, ("nb-big",))
        old = _plans(scale_database, legacy, ("nb-big",)) if legacy else {}
        for mode, plan in plans.items():
            _assert_notebook_scoped(
                f"{label} {mode}", plan, old.get(mode, ""),
                extra_probes=_EXTRA_ROW_PROBES["concept_clusters"] if legacy else 0,
            )
