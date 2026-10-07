"""E4-3 / M1 写侧的 PostgreSQL 孪生(`test_memory_kg_isolation_write.py` 的 SQLite 侧)。

PostgreSQL 是主环境:这里在真 PG 上钉住

* `merge_objects_in_transaction` 在加锁的那条语句里判定:别人的(含孤儿)Memory 派生对象
  当作不存在(KeyError → 路由 404,与未知 id 同一回答),调用者本人的跨类/同主人合并拒绝
  (409 文案),拒绝时一行不写;共享对象照常合并;
* 按 id 写的路由(PATCH、合并、贡献申请、关系审核)对非属主与「不存在」同答,属主保留 409;
  审批时对象已不存在 → 关闭为 object_missing;含 Memory 的笔记本不能发布为公共库(H11 计数);
* 增量融合的语句数与库里 Memory 来源条数无关(N = 0 / 5 / 1000,两个分支);
* 冲突检测的两个读者排除 Memory 派生对象、向量与关系(排除在 `LIMIT` 护栏之前);
* `incremental_fuse_source` 经真实 `PostgresRepository`:Memory 来源入口即返回、不写簇行;
  共享来源融合时 Tier2(暴力分支)不桥接到 Memory 派生 concept(对照组为普通来源);
* 新增 SQL 的 EXPLAIN pin(同 `test_memory_sql_explain_pins.py` 的判据:关掉
  seqscan/bitmapscan,断言索引路径与反连接形态)。
"""
from __future__ import annotations

import json

import pytest

from app.domain.memory_kg_isolation import (
    CROSS_CLASS_MESSAGE,
    SAME_OWNER_MESSAGE,
    MemoryKnowledgeMergeRefused,
)
from app.domain.vector_index import encode_vector
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.repository_runtime import RepositoryCompatibilitySeams

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_kg_isolation_write"),
]

NOW = "2026-09-29T00:00:00+00:00"


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-test",
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed(database) -> None:
    """nb: shared source `src-s` (+ `src-s2`), Memory sources of u-a (`src-ma1`,
    `src-ma2`), of u-b (`src-mb`) and one orphan (`src-mo`, memory_id NULL); one
    concept per source, one relation per source."""
    with database.write() as db:
        for user in ("u-a", "u-b"):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user, f"{user}@example.test", user, NOW, NOW, user),
            )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nb','N','','','ready','u-a',%s,%s,'personal')",
            (NOW, NOW),
        )
        for memory_id, owner in (("mem-a1", "u-a"), ("mem-a2", "u-a"), ("mem-b", "u-b")):
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                "content_md,created_at,updated_at) "
                "VALUES (%s,'nb',%s,'ask_answer','confirmed','t','x',%s,%s)",
                (memory_id, owner, NOW, NOW),
            )
        for source_id, source_type, memory_id in (
            ("src-s", "file", None),
            ("src-s2", "file", None),
            ("src-ma1", "memory", "mem-a1"),
            ("src-ma2", "memory", "mem-a2"),
            ("src-mb", "memory", "mem-b"),
            ("src-mo", "memory", None),
        ):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) VALUES (%s,'nb',%s,%s,%s,%s,%s)",
                (source_id, source_id, source_type, memory_id, NOW, NOW),
            )
            object_id = "ko-" + source_id[4:]
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,'nb','claim','approved',%s,%s,%s,%s,%s)",
                (object_id, json.dumps({"name": object_id}),
                 json.dumps([{"source_id": source_id, "element_id": f"el-{source_id}",
                              "quoted_span": object_id}]),
                 source_id, NOW, NOW),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                "VALUES (%s,'nb',%s,%s)",
                (object_id, encode_vector([1.0] + [0.0] * 15), NOW),
            )
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,created_at) VALUES (%s,'nb',%s,%s,%s,'r',%s)",
                ("kr-" + source_id[4:], source_id, object_id, object_id, NOW),
            )


@pytest.fixture
def seeded(postgres_database):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    return postgres_database, GovernanceStore(postgres_database, _seams())


def _snapshot(database) -> dict:
    with database.connect() as db:
        return {
            r["id"]: (r["status"], json.dumps(r["evidence"], sort_keys=True))
            for r in db.execute(
                "SELECT id, status, evidence FROM knowledge_objects WHERE notebook_id='nb'"
            ).fetchall()
        }


@pytest.mark.parametrize(
    ("source_id", "into_id", "message"),
    [
        ("ko-ma1", "ko-s", CROSS_CLASS_MESSAGE),
        ("ko-s", "ko-ma1", CROSS_CLASS_MESSAGE),
        ("ko-ma1", "ko-ma2", SAME_OWNER_MESSAGE),
    ],
)
def test_pg_store_refuses_the_owners_memory_merges_and_writes_nothing(
    seeded, source_id, into_id, message
):
    database, store = seeded
    before = _snapshot(database)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        with database.write() as db:
            store.merge_objects_in_transaction(
                db, "nb", source_id, into_id, NOW, actor_id="u-a"
            )
    assert refused.value.user_message == message
    assert _snapshot(database) == before


@pytest.mark.parametrize(
    ("actor", "source_id", "into_id"),
    [
        ("u-a", "ko-ma1", "ko-mb"),     # another member's Memory
        ("u-a", "ko-mb", "ko-s"),
        ("u-a", "ko-ma1", "ko-mo"),     # orphan Memory source: nobody's
        ("u-b", "ko-ma1", "ko-s"),
        (None, "ko-ma1", "ko-s"),       # internal caller owns no Memory
        ("u-a", "ko-unknown", "ko-s"),  # the answer they must all equal
    ],
)
def test_pg_store_treats_foreign_memory_as_missing(seeded, actor, source_id, into_id):
    database, store = seeded
    before = _snapshot(database)
    with pytest.raises(KeyError) as missing:
        with database.write() as db:
            store.merge_objects_in_transaction(
                db, "nb", source_id, into_id, NOW, actor_id=actor
            )
    assert type(missing.value) is KeyError
    assert _snapshot(database) == before


def test_pg_store_still_merges_shared_objects(seeded):
    database, store = seeded
    with database.write() as db:
        row = store.merge_objects_in_transaction(db, "nb", "ko-s", "ko-s2", NOW)
    assert row["id"] == "ko-s2"
    assert _snapshot(database)["ko-s"][0] == "deprecated"


def test_pg_conflict_readers_exclude_memory_rows(seeded):
    database, store = seeded
    memory_objects = {"ko-ma1", "ko-ma2", "ko-mb", "ko-mo"}
    memory_relations = {"kr-ma1", "kr-ma2", "kr-mb", "kr-mo"}
    with database.connect() as db:
        objects, vectors, _nb = store.conflict_resolution_rows(db, "nb")
        relations = store.conflict_relation_rows(db, "nb")
        bounded = store.conflict_relation_rows(db, "nb", max_rows=2)
    assert {r["id"] for r in objects} == {"ko-s", "ko-s2"}
    assert {r["object_id"] for r in vectors} == {"ko-s", "ko-s2"}
    assert {r["id"] for r in relations} == {"kr-s", "kr-s2"}
    assert {r["id"] for r in bounded} == {"kr-s", "kr-s2"}
    assert not ({r["id"] for r in objects} & memory_objects)
    assert not ({r["id"] for r in relations} & memory_relations)


# --------------------------------------------------------------------------
# incremental_fuse_source through the real PostgreSQL repository
# --------------------------------------------------------------------------

@pytest.fixture
def pg_repository(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.kg_auto_extract = False
    postgres_settings.embed_dim = 16
    repository = PostgresRepository(postgres_settings)
    yield repository
    repository._runtime.database.close()


def _fusion_seed(repository, existing_source_type: str) -> None:
    from app.services.kg_merge import _norm

    with repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','u-a@example.test','u-a','user','active',%s,%s,'u-a','','',0)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nbf','N','','','ready','u-a',%s,%s,'personal')",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "VALUES ('mem-f','nbf','u-a','ask_answer','confirmed','t','x',%s,%s)",
            (NOW, NOW),
        )
        for source_id, source_type, memory_id in (
            ("src-e", existing_source_type,
             "mem-f" if existing_source_type == "memory" else None),
            ("src-new", "file", None),
        ):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) VALUES (%s,'nbf',%s,%s,%s,%s,%s)",
                (source_id, source_id, source_type, memory_id, NOW, NOW),
            )
        for object_id, name, source_id, vector in (
            ("ko-e", "Expert Routing", "src-e", [1.0, 0.05] + [0.0] * 14),
            ("ko-new", "MoE Gating", "src-new", [0.99, 0.04] + [0.0] * 14),
        ):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,'nbf','concept','approved',%s,'[]',%s,%s,%s)",
                (object_id, json.dumps({"name": name}), source_id, NOW, NOW),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                "VALUES (%s,'nbf',%s,%s)",
                (object_id, encode_vector(vector), NOW),
            )
        # Legacy (pre-migration) cluster row on the existing concept, so the
        # Tier-2 exclusion is what keeps the Memory side out.
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "VALUES ('cc-e','nbf',%s,'ko-e','Expert Routing','concept',%s,0)",
            ("K-" + _norm("Expert Routing"), NOW),
        )


def _clusters_and_candidates(repository):
    with repository._runtime.database.connect() as db:
        members = {r["member_object_id"] for r in db.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id='nbf'"
        ).fetchall()}
        pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in db.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
            "WHERE notebook_id='nbf'"
        ).fetchall()}
    return members, pairs


@pytest.mark.parametrize("existing_source_type", ["file", "memory"])
def test_pg_tier2_never_bridges_to_a_memory_concept(pg_repository, existing_source_type):
    from app.services.kg_merge import _norm

    _fusion_seed(pg_repository, existing_source_type)
    pg_repository.incremental_fuse_source("nbf", "src-new")
    members, pairs = _clusters_and_candidates(pg_repository)
    assert "ko-new" in members
    pair = tuple(sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Expert Routing"))))
    assert pairs == ({pair} if existing_source_type == "file" else set())


def test_pg_memory_source_fusion_returns_at_entry(pg_repository):
    _fusion_seed(pg_repository, "memory")
    with pg_repository._runtime.database.write() as db:
        db.execute("DELETE FROM concept_clusters WHERE notebook_id='nbf'")
    pg_repository.incremental_fuse_source("nbf", "src-e")
    members, pairs = _clusters_and_candidates(pg_repository)
    assert members == set() and pairs == set()


# --------------------------------------------------------------------------
# EXPLAIN pins for the SQL this task added
# --------------------------------------------------------------------------

def _plan(connection, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


class _CapturingConnection:
    """Records each statement a store method issues, then runs it."""

    def __init__(self, connection):
        self.connection = connection
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=None):
        self.statements.append((str(sql), tuple(params or ())))
        return self.connection.execute(sql, params)


_EXPLAIN_TABLES = ("sources", "memory_items", "knowledge_objects",
                   "knowledge_relations", "knowledge_embeddings")


def _bulk_seed(database) -> None:
    """A non-trivial second notebook (same shape as the E0 memory_sql pins:
    4000 sources of which 200 Memory, 8000 objects/relations) so the planner
    has real cost differences to choose between."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nbx','X','','','ready','u-a',%s,%s,'personal')",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'memx-'||g,'nbx',CASE WHEN g%%2=0 THEN 'u-a' ELSE 'u-b' END,"
            "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(0,199) g",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'srcx-'||g,'nbx','t',"
            "CASE WHEN g<200 THEN 'memory' ELSE 'upload' END,"
            "CASE WHEN g<200 THEN 'memx-'||g END,%s,%s FROM generate_series(0,3999) g",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "created_at,updated_at) SELECT 'kox-'||g,'nbx','concept','approved',"
            "'srcx-'||(g%%4000),%s,%s FROM generate_series(0,7999) g",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "SELECT 'kox-'||g,'nbx',%s,%s FROM generate_series(0,7999) g",
            (encode_vector([1.0] + [0.0] * 15), NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) SELECT 'krx-'||g,'nbx','srcx-'||(g%%4000),"
            "'kox-0','kox-1','r',%s FROM generate_series(0,7999) g",
            (NOW,),
        )
    import psycopg

    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        for table in _EXPLAIN_TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


def test_pg_new_statements_keep_index_paths(seeded):
    database, store = seeded
    _bulk_seed(database)
    with database.connect() as db:
        captured = _CapturingConnection(db)
        store.conflict_resolution_rows(captured, "nbx")
        # The production rail: kg_conflict_max_relations (1,000,000) + 1.
        store.conflict_relation_rows(captured, "nbx", max_rows=1_000_001)
        detection = [s for s in captured.statements if "memory" in s[0]]
    assert len(detection) == 3  # objects, vectors, relations
    with database.write() as db:
        merge_capture = _CapturingConnection(db)
        store.merge_objects_in_transaction(
            merge_capture, "nb", "ko-s", "ko-s2", NOW, actor_id="u-a"
        )
    (locking,) = [s for s in merge_capture.statements if "FOR UPDATE" in s[0]]

    with database.connect() as db:
        plans = [_plan(db, sql, params) for sql, params in detection]
        merge_plan = _plan(db, *locking)

    for plan in plans:
        assert "Seq Scan" not in plan, plan
        # D4 classifier decorrelated into an anti join whose inner side reads
        # the Memory sources through idx_sources_nb_hidden_type (all of the
        # deployment's: the classifier is not notebook-scoped) — no per-row
        # SubPlan.
        assert "Anti Join" in plan and "SubPlan" not in plan, plan
        assert "idx_sources_nb_hidden_type" in plan, plan
    assert "Seq Scan" not in merge_plan, merge_plan
    # The classifier and the "not someone else's Memory" filter are
    # primary-key probes on the two locked rows' sources / memory rows.
    assert "pk_sources" in merge_plan, merge_plan
    assert "pk_memory_items" in merge_plan, merge_plan


# --------------------------------------------------------------------------
# Promotion through the real HTTP routes on PostgreSQL
# (scenarios shared with SQLite: tests/memory_promotion_isolation_cases.py)
# --------------------------------------------------------------------------

@pytest.fixture
def pg_promotion_world(postgres_scope, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import repository
    from app.core.config import get_settings
    from app.main import create_app
    from tests import memory_promotion_isolation_cases as cases

    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "4")
    get_settings.cache_clear()
    client = TestClient(create_app())
    repo = repository()
    assert type(repo).__name__ == "PostgresRepository"
    try:
        yield cases, cases.build(client, repo, lambda sql: sql.replace("?", "%s"))
    finally:
        repo.close()


def test_pg_member_cannot_propose_an_object_derived_from_anothers_memory(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.member_b_cannot_propose_a_memory_object(world)


def test_pg_owner_is_told_why_her_memory_object_cannot_be_proposed(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_cannot_propose_her_memory_object_here(world)


def test_pg_non_owner_write_routes_answer_like_a_missing_id(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.non_owner_write_routes_answer_like_a_missing_id(world)


def test_pg_owner_keeps_the_reason_on_write_routes(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_keeps_the_reason(world)


def test_pg_proposal_of_a_vanished_object_is_closed_on_approval(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.proposal_of_a_vanished_object_is_closed_on_approval(world)


def test_pg_publishing_a_notebook_with_memory_is_refused(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.publishing_a_notebook_with_memory_is_refused(world)


def test_pg_checkup_counts_memory_in_a_public_library(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.checkup_counts_memory_in_a_public_library(world)


def test_pg_shared_review_surfaces_leave_memory_rows_out(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.shared_review_surfaces_leave_memory_rows_out(world)


def test_pg_queued_memory_object_proposal_is_refused_and_closed(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.queued_memory_proposal_is_refused_on_approval(world)


def test_pg_withdrawn_memory_object_proposal_stays_withdrawn(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.withdrawn_memory_proposal_stays_withdrawn(world)


def test_pg_creator_memory_promotion_still_works_end_to_end(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.creator_memory_promotion_still_works(world)


def test_pg_ordinary_object_still_promotes(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.ordinary_object_still_promotes(world)


def test_pg_promotion_statements_keep_index_paths(seeded):
    """EXPLAIN pin for the two promotion reads this task changed: both add the
    D4 classifier to a primary-key read of one object."""
    from app.domain.memory_kg_isolation import MemoryPromotionRefused

    database, store = seeded
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nb-pub','P','','','ready','u-a',%s,%s,'base')",
            (NOW, NOW),
        )
        store.insert_promotion_candidate(
            db, "promo-pin", "nb", "ko-ma1", "claim", NOW, target_base_id="nb-pub"
        )
    with database.connect() as db:
        captured = _CapturingConnection(db)
        row = store.promotion_object_type_row(captured, "nb", "ko-ma1", actor_id="u-a")
        assert row["memory_derived"] is True
        assert store.promotion_object_type_row(db, "nb", "ko-ma1", actor_id="u-b") is None
        assert store.promotion_object_type_row(
            db, "nb", "ko-s", actor_id="u-b"
        )["memory_derived"] is False
        (type_row,) = captured.statements
    with pytest.raises(MemoryPromotionRefused):
        with database.write() as db:
            approve_capture = _CapturingConnection(db)
            store.approve_promotion_in_transaction(approve_capture, "promo-pin", NOW)
    (object_read,) = [
        s for s in approve_capture.statements if "AS memory_derived" in s[0]
    ]
    _bulk_seed(database)  # realistic statistics before planning
    with database.connect() as db:
        type_plan = _plan(db, *type_row)
        approve_plan = _plan(db, *object_read)
    for plan in (type_plan, approve_plan):
        assert "Seq Scan" not in plan, plan
        assert "pk_knowledge_objects" in plan, plan
        assert "pk_sources" in plan, plan


# --------------------------------------------------------------------------
# Review round: EXPLAIN pins for every statement this round changed, taken
# from what the store methods actually execute (captured, then planned on the
# 4000-source / 8000-object notebook).
# --------------------------------------------------------------------------

def test_pg_owner_scoped_writes_and_fusion_reads_keep_index_paths(seeded):
    from app.models.knowledge import KnowledgeUpdate
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.notebook_store import NotebookStore

    database, store = seeded
    _bulk_seed(database)
    captured: dict[str, tuple[str, tuple]] = {}

    def last(conn: _CapturingConnection, needle: str) -> tuple[str, tuple]:
        (hit,) = [s for s in conn.statements if needle in s[0]]
        return hit

    with database.write() as db:
        c = _CapturingConnection(db)
        store.update_object_in_transaction(
            c, "nbx", "kox-500", KnowledgeUpdate(status="approved"), NOW, actor_id="u-a"
        )
        captured["update_object"] = last(c, "FOR UPDATE")
        c = _CapturingConnection(db)
        store.update_edge_review(c, "nbx", "krx-500", "verified", actor_id="u-a")
        captured["edge_review"] = last(c, "UPDATE knowledge_relations")
        db.rollback()
    with database.connect() as db:
        c = _CapturingConnection(db)
        KnowledgeStore.incremental_object_rows(
            c, "nbx", "srcx-3999", "concept", exclude_source=True,
            exclude_memory_derived=True,
        )
        captured["tier2_pool"] = c.statements[-1]
        c = _CapturingConnection(db)
        KnowledgeStore.valid_object_ids(
            c, [f"kox-{i}" for i in range(0, 400, 7)], exclude_memory_derived=True
        )
        captured["ann_alive"] = c.statements[-1]
        c = _CapturingConnection(db)
        NotebookStore.public_library_memory_source_count(c, "nbx")
        captured["h11"] = c.statements[-1]
    # The publish UPDATE, captured from the real store method on a notebook
    # with Memory (refused; the conditional UPDATE itself still runs).
    from contextlib import contextmanager

    from app.domain.memory_kg_isolation import NotebookHoldsMemory

    publish: list[tuple[str, tuple]] = []

    class _CaptureDatabase:
        @contextmanager
        def write(self):
            with database.write() as db:
                conn = _CapturingConnection(db)
                try:
                    yield conn
                finally:
                    publish.extend(conn.statements)

    notebooks = NotebookStore(
        _CaptureDatabase(), new_id=lambda prefix: prefix, now=lambda: NOW,
        activity_retention_days=30,
    )
    with pytest.raises(NotebookHoldsMemory):
        notebooks.set_tier("nbx", "base")
    captured["publish"] = next(s for s in publish if s[0].startswith("UPDATE notebooks"))

    with database.connect() as db:
        plans = {name: _plan(db, sql, params) for name, (sql, params) in captured.items()}
    for name, plan in plans.items():
        assert "Seq Scan" not in plan, (name, plan)
        assert "SubPlan" not in plan or name in ("update_object", "edge_review"), (name, plan)
    # Owner filter on the two single-row writes: primary-key probes.
    for name in ("update_object", "edge_review"):
        assert "pk_sources" in plans[name], (name, plans[name])
    assert "pk_knowledge_objects" in plans["update_object"], plans["update_object"]
    # The Tier-2 pool and the ANN alive-check fold the D4 classifier into the
    # read they already were: anti join on the Memory-source index.
    for name in ("tier2_pool", "ann_alive"):
        assert "Anti Join" in plans[name], (name, plans[name])
    assert "pk_knowledge_objects" in plans["ann_alive"], plans["ann_alive"]
    # Publish refusal and H11: the (notebook_id, source_type) index.
    for name in ("publish", "h11"):
        assert "idx_sources" in plans[name], (name, plans[name])


# --------------------------------------------------------------------------
# Cost pin: incremental fusion statements do not grow with Memory sources
# --------------------------------------------------------------------------

def _count_pg_statements(monkeypatch) -> dict:
    from app.repositories.postgres import database as pgdb

    counter = {"n": 0}
    original = pgdb._SafeDiagnosticConnection.execute

    def counted(self, *args, **kwargs):
        sql = str(args[0]) if args else ""
        # Pool housekeeping (connection reset / session settings on checkout)
        # varies with pool reuse, not with the work: not counted.
        if sql.strip() and not sql.startswith("RESET") and "set_config(" not in sql:
            counter["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pgdb._SafeDiagnosticConnection, "execute", counted)
    return counter


_FAR_CONCEPTS = 3000


def _cost_seed(
    repository, notebook_id: str, n: int, *, deprecated_ordinary: bool, far: bool = True
) -> None:
    from app.services.kg_merge import _norm

    with repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES (%s,'N','','','ready','u-c',%s,%s,'personal')",
            (notebook_id, NOW, NOW),
        )
        if n:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                "content_md,created_at,updated_at) SELECT %s||'-mem'||g,%s,'u-c',"
                "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(1,%s) g",
                (notebook_id, notebook_id, NOW, NOW, n),
            )
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) SELECT %s||'-srcm'||g,%s,'t',%s,"
                "CASE WHEN %s THEN NULL ELSE %s||'-mem'||g END,%s,%s "
                "FROM generate_series(1,%s) g",
                (notebook_id, notebook_id, "file" if deprecated_ordinary else "memory",
                 deprecated_ordinary, notebook_id, NOW, NOW, n),
            )
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) SELECT %s||'-kom'||g,%s,'concept',"
                "%s,jsonb_build_object('name','Private notion '||g),'[]',"
                "%s||'-srcm'||g,%s,%s FROM generate_series(1,%s) g",
                (notebook_id, notebook_id, "deprecated" if deprecated_ordinary else "approved",
                 notebook_id, NOW, NOW, n),
            )
            for g in range(1, n + 1):
                # Distinct vectors (HNSW recall degrades on exact duplicates),
                # all behind the shared concept but well above the bridge
                # threshold, so they crowd the ANN window.
                vec = [1.0] + [0.0] * 15
                vec[1 + g % 14] = 0.2 + (g % 50) * 0.002
                db.execute(
                    "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                    "VALUES (%s,%s,%s,%s)",
                    (f"{notebook_id}-kom{g}", notebook_id, encode_vector(vec), NOW),
                )
        for source_id in ("src-A", "src-B", "src-far"):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES (%s,%s,%s,'file',%s,%s)",
                (f"{notebook_id}-{source_id}", notebook_id, source_id, NOW, NOW),
            )
        # Far-away ordinary concepts (cosine to the query well under lo): the
        # widening loop's tail drops below lo on them long before k reaches
        # n_labels, so no arm ever asks hnswlib for every label (the query
        # that intermittently fails, quality re-review P1-1).
        far_rows = []
        for g in range(_FAR_CONCEPTS if far else 0):
            vec = [0.0] * 16
            vec[8 + g % 8] = 1.0
            vec[1 + g % 7] = 0.1 + (g % 97) * 0.003
            far_rows.append((f"{notebook_id}-kof{g}", notebook_id,
                        json.dumps({"name": f"Unrelated {g}"}),
                        f"{notebook_id}-src-far", NOW, NOW, encode_vector(vec)))
        cursor = db.cursor()
        cursor.executemany(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES (%s,%s,'concept','approved',%s,'[]',%s,%s,%s)",
            [row[:6] for row in far_rows],
        )
        cursor.executemany(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "VALUES (%s,%s,%s,%s)",
            [(row[0], row[1], row[6], NOW) for row in far_rows],
        )
        shared = [1.0] + [0.0] * 15
        shared[15] = 0.01
        for object_id, name, source_id, vec in (
            ("ko-s", "Expert Routing", "src-A", shared),
            ("ko-new", "MoE Gating", "src-B", [1.0] + [0.0] * 15),
        ):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,%s,'concept','approved',%s,'[]',%s,%s,%s)",
                (f"{notebook_id}-{object_id}", notebook_id, json.dumps({"name": name}),
                 f"{notebook_id}-{source_id}", NOW, NOW),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                "VALUES (%s,%s,%s,%s)",
                (f"{notebook_id}-{object_id}", notebook_id, encode_vector(vec), NOW),
            )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "VALUES (%s,%s,%s,%s,'Expert Routing','concept',%s,0)",
            (f"{notebook_id}-cc", notebook_id, "K-" + _norm("Expert Routing"),
             f"{notebook_id}-ko-s", NOW),
        )


@pytest.mark.parametrize("branch", ["bruteforce", "ann"])
def test_pg_fusion_statements_do_not_grow_with_memory_sources(pg_repository, monkeypatch, branch):
    """Twin of the SQLite cost pin (quality review P1-1 / F2): brute force is
    flat over 0 / 5 / 1,000 Memory sources, and so is ANN: the scale index
    leaves Memory-derived objects out of its ANN labels (E4-6), so 1,000
    Memory concepts never crowd the window and cost what none cost -- never
    one read per Memory source, and never more than 1,000 deprecated ordinary
    concepts in the same places (which do crowd it: the existing bounded
    doubling loop)."""
    from app.services.kg_merge import _norm

    with pg_repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-c','u-c@example.test','u-c','user','active',%s,%s,'u-c','','',0)",
            (NOW, NOW),
        )
    counts = {}
    for label, n, deprecated in (("0", 0, False), ("5", 5, False),
                                 ("1000", 1000, False), ("deprecated", 1000, True)):
        notebook_id = f"nbc{label}"
        # Far-away concepts only matter to the ANN window; the brute-force arm
        # stays small (it runs the whole pool through Python cosine).
        _cost_seed(pg_repository, notebook_id, n, deprecated_ordinary=deprecated,
                   far=branch == "ann")
        if branch == "ann":
            pg_repository.build_scale_index(notebook_id)
            monkeypatch.setattr(pg_repository.settings, "kg_incremental_tier2_max_entities", 0)
        new_source = f"{notebook_id}-src-B"
        pg_repository.incremental_fuse_source(notebook_id, new_source)  # warm-up
        with pg_repository._runtime.database.write() as db:
            db.execute("DELETE FROM concept_merge_candidates WHERE notebook_id=%s", (notebook_id,))
        counter = _count_pg_statements(monkeypatch)
        pg_repository.incremental_fuse_source(notebook_id, new_source)
        counts[label] = counter["n"]
        monkeypatch.undo()
        with pg_repository._runtime.database.connect() as db:
            pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=%s", (notebook_id,)
            ).fetchall()}
        assert pairs == {tuple(sorted(("K-" + _norm("MoE Gating"),
                                       "K-" + _norm("Expert Routing"))))}, (label, pairs)
    assert counts["0"] == counts["5"] == counts["1000"], counts
    if branch == "bruteforce":
        assert counts["1000"] == counts["deprecated"], counts
    else:
        assert counts["1000"] <= counts["deprecated"], counts


# --------------------------------------------------------------------------
# Publish window (fix3 P2): a Memory confirmed before the notebook is
# published never lands in the public library
# --------------------------------------------------------------------------

def _pg_memory_world(pg_repository, monkeypatch):
    from app.models.schemas import NotebookCreate

    repo = pg_repository
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-p','u-p@example.test','u-p','user','active',%s,%s,'u-p','','',0)",
            (NOW, NOW),
        )
    nb = repo.create_notebook(NotebookCreate(name="to be published")).id
    with repo._runtime.database.write() as db:
        db.execute("UPDATE notebooks SET created_by='u-p' WHERE id=%s", (nb,))
    monkeypatch.setattr(repo._runtime.source_ingestion.settings, "kg_auto_extract", True)
    seen: list = []
    original = repo._runtime.event_log.emit
    monkeypatch.setattr(
        repo._runtime.event_log, "emit",
        lambda event: (seen.append(dict(event)), original(event))[1],
    )
    return repo, nb, seen


def _pg_memory_rows(repo, nb: str) -> tuple[list, list]:
    with repo._runtime.database.connect() as db:
        sources = [r["id"] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=%s AND source_type='memory'", (nb,)
        ).fetchall()]
        objects = [r["id"] for r in db.execute(
            "SELECT ko.id FROM knowledge_objects ko JOIN sources s ON s.id = ko.source_id "
            "WHERE ko.notebook_id=%s AND s.source_type='memory'", (nb,)
        ).fetchall()]
    return sources, objects


def test_pg_memory_queued_before_publish_never_reaches_the_public_library(
    pg_repository, monkeypatch
):
    repo, nb, seen = _pg_memory_world(pg_repository, monkeypatch)
    service = repo._runtime.memory_service
    queued: list = []
    monkeypatch.setattr(service, "kg_ingest_scheduler", lambda fn, item: queued.append((fn, item)))
    memory = repo.create_memory_candidate(nb, "u-p", None, "req-pg-q", "Queued", "Private x", [], "", {}, [])
    repo.confirm_memory(memory.id, "u-p")
    assert len(queued) == 1

    repo.mark_notebook_base(nb)
    fn, item = queued[0]
    fn(item)

    assert _pg_memory_rows(repo, nb) == ([], [])
    assert {c.code: c.count for c in repo.checkup.run(nb).checks}["H11"] == 0
    assert [e for e in seen if e.get("status") == "skipped"] == [
        {"kind": "memory_kg", "notebook_id": nb, "memory_id": memory.id,
         "status": "skipped", "reason": "not_eligible"}
    ]


def test_pg_memory_source_insert_is_refused_in_a_public_library(pg_repository, monkeypatch):
    repo, nb, seen = _pg_memory_world(pg_repository, monkeypatch)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES ('mem-pgpub',%s,'u-p','ask_answer',"
            "'confirmed','t','Private x',%s,%s)",
            (nb, NOW, NOW),
        )
    repo.mark_notebook_base(nb)
    ingestion = repo._runtime.source_ingestion
    assert ingestion.ingest_memory_source(nb, "mem-pgpub", "t", "Private x") is None
    assert _pg_memory_rows(repo, nb) == ([], [])
    assert [e for e in seen if e.get("status") == "skipped"] == [
        {"kind": "memory_kg", "notebook_id": nb, "memory_id": "mem-pgpub",
         "status": "skipped", "reason": "public_library"}
    ]
    # Store level, and the control on a shared notebook.
    store = repo._runtime.source_store
    kwargs = dict(title="t", source_type="memory", status="active", parse_status="parsed",
                  file_name="", file_path="", file_size=0, file_hash="h", summary="",
                  doc_type="", memory_id="mem-pgpub", unless_public_library=True)
    assert store.insert_source(source_id="src-pgpub-1", notebook_id=nb, **kwargs) is False
    repo.set_notebook_personal(nb)
    assert store.insert_source(source_id="src-pgpub-2", notebook_id=nb, **kwargs) is True
    assert _pg_memory_rows(repo, nb)[0] == ["src-pgpub-2"]


def test_pg_conditional_memory_insert_keeps_the_notebook_primary_key_path(pg_repository):
    """EXPLAIN pin for the INSERT … SELECT … FOR SHARE: a primary-key read of
    the one notebook row."""
    repo = pg_repository
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="pin")).id
    store = repo._runtime.source_store
    with repo._runtime.database.write() as db:
        captured = _CapturingConnection(db)
        assert store.insert_source(
            source_id="src-pin", notebook_id=nb, title="t", source_type="memory",
            status="active", parse_status="parsed", file_name="", file_path="",
            file_size=0, file_hash="h", summary="", doc_type="", memory_id="",
            connection=captured, unless_public_library=True,
        ) is True
        (insert,) = [s for s in captured.statements if s[0].startswith("INSERT INTO sources")]
        db.rollback()
    with repo._runtime.database.write() as db:
        plan = _plan(db, *insert)
        db.rollback()
    assert "Seq Scan" not in plan, plan
    assert "pk_notebooks" in plan and "LockRows" in plan, plan


def test_pg_publish_waiting_on_an_uncommitted_memory_insert_is_refused(pg_repository):
    """Statement-level race: a Memory-source insert holds the notebook row FOR
    SHARE and has not committed yet; a publish arrives and waits. When the
    insert commits, the publish must see the new source and refuse — its
    decision is taken on a snapshot from after the lock was granted."""
    import threading
    import time

    from app.domain.memory_kg_isolation import NotebookHoldsMemory
    from app.models.schemas import NotebookCreate

    repo = pg_repository
    nb = repo.create_notebook(NotebookCreate(name="race")).id
    store = repo._runtime.source_store
    outcome: dict = {}

    def publish():
        try:
            repo._runtime.notebook_store.set_tier(nb, "base")
            outcome["result"] = "published"
        except NotebookHoldsMemory:
            outcome["result"] = "refused"
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["result"] = repr(exc)

    import psycopg
    from psycopg.rows import dict_row

    url = repo._runtime.database.settings.database_url  # carries the test schema
    # Raw connections outside the (small) test pool: the insert transaction
    # held open, and a probe that watches the publish block.
    with psycopg.connect(url, row_factory=dict_row) as insert_tx, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        assert store.insert_source(
            source_id="src-race", notebook_id=nb, title="t", source_type="memory",
            status="active", parse_status="parsed", file_name="", file_path="",
            file_size=0, file_hash="h", summary="", doc_type="", memory_id="",
            connection=insert_tx, unless_public_library=True,
        ) is True
        worker = threading.Thread(target=publish)
        worker.start()
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            waiting = probe.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%notebooks%'"
            ).fetchone()["n"]
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "the publish never blocked on the notebook row"
        insert_tx.commit()
    worker.join(timeout=10)
    assert outcome == {"result": "refused"}
    with repo._runtime.database.connect() as db:
        tier = db.execute("SELECT tier FROM notebooks WHERE id=%s", (nb,)).fetchone()["tier"]
    assert tier == "personal"


def test_pg_shared_review_readers_keep_index_paths(seeded):
    """EXPLAIN pins (quality re-review P2-1): the edge review queue and the
    duplicate seed read leave Memory-derived rows out with the D4 classifier —
    an anti join, no Seq Scan, no per-row SubPlan."""
    from app.repositories.postgres.knowledge_store import KnowledgeStore

    database, store = seeded
    _bulk_seed(database)
    with database.connect() as db:
        c = _CapturingConnection(db)
        store.review_queue_rows(c, "nbx")
        (queue_read,) = [s for s in c.statements if "FROM knowledge_relations kr" in s[0]]
        c = _CapturingConnection(db)
        KnowledgeStore.duplicate_seed_rows(c, "nbx", "concept")
        KnowledgeStore.duplicate_seed_rows(c, "nbx", "procedure")
        seed_reads = list(c.statements)
    assert len(seed_reads) == 2
    with database.connect() as db:
        queue_plan = _plan(db, *queue_read)
        seed_plans = [_plan(db, *s) for s in seed_reads]
    # The queue read keeps its pre-existing per-row SubPlan (the evidence
    # anchor flag over jsonb_array_elements); the Memory classifier itself is
    # the decorrelated anti join on the relation's source.
    assert "Seq Scan" not in queue_plan, queue_plan
    assert "Anti Join" in queue_plan and "(kr.source_id = ds.id)" in queue_plan, queue_plan
    for plan in seed_plans:
        assert "Seq Scan" not in plan, plan
        assert "Anti Join" in plan and "SubPlan" not in plan, plan


def test_pg_bell_edge_count_keeps_its_index_path(seeded, monkeypatch):
    """EXPLAIN pin for the bell's 「关系审核」 count (fix4 P2): the pending-edge
    GROUP BY now carries the Memory exclusion — relations still reached through
    a notebook_id index, the classifier an anti join."""
    from app.repositories.postgres import database as pgdb
    from app.repositories.postgres.query_store import QueryStore

    database, _store = seeded
    _bulk_seed(database)
    captured: list[tuple[str, tuple]] = []
    original = pgdb._SafeDiagnosticConnection.execute

    def capture(self, query, params=None, *args, **kwargs):
        text = query.as_string(self) if hasattr(query, "as_string") else str(query)
        if "knowledge_relations" in text and "GROUP BY notebook_id" in text:
            captured.append((text, tuple(params or ())))
        return original(self, query, params, *args, **kwargs)

    monkeypatch.setattr(pgdb._SafeDiagnosticConnection, "execute", capture)
    projection = QueryStore(database).pending_actions_projection_rows("u-a")
    monkeypatch.undo()
    assert "nbx" in projection["notebook_ids"]
    (edge_count,) = captured
    with database.connect() as db:
        plan = _plan(db, *edge_count)
    assert "Seq Scan" not in plan, plan
    # Relations reached by a notebook_id index (the planner picks between the
    # notebook-leading indexes by statistics), never a full scan.
    assert "on knowledge_relations" in plan and "Index Cond: (notebook_id = ANY" in plan, plan
    assert "Anti Join" in plan and "SubPlan" not in plan, plan


def test_pg_memory_insert_waiting_on_an_uncommitted_publish_inserts_nothing(pg_repository):
    """Reverse order of the race above (fix4 P3-2): the publish holds the
    notebook row (FOR NO KEY UPDATE, then its UPDATE) and has not committed; a
    Memory-source insert arrives and waits on its FOR SHARE read. When the
    publish commits, the insert's locking read re-checks the new row version,
    finds tier='base' and inserts nothing."""
    import threading
    import time

    import psycopg
    from psycopg.rows import dict_row

    from app.models.schemas import NotebookCreate

    repo = pg_repository
    nb = repo.create_notebook(NotebookCreate(name="race-reverse")).id
    store = repo._runtime.source_store
    outcome: dict = {}
    url = repo._runtime.database.settings.database_url  # carries the test schema

    def insert():
        with psycopg.connect(url, row_factory=dict_row) as tx:
            try:
                outcome["inserted"] = store.insert_source(
                    source_id="src-race-rev", notebook_id=nb, title="t",
                    source_type="memory", status="active", parse_status="parsed",
                    file_name="", file_path="", file_size=0, file_hash="h",
                    summary="", doc_type="", memory_id="",
                    connection=tx, unless_public_library=True,
                )
                tx.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                outcome["inserted"] = repr(exc)

    with psycopg.connect(url, row_factory=dict_row) as publish_tx, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        publish_tx.execute("SELECT 1 FROM notebooks WHERE id=%s FOR NO KEY UPDATE", (nb,))
        publish_tx.execute("UPDATE notebooks SET tier='base' WHERE id=%s", (nb,))
        worker = threading.Thread(target=insert)
        worker.start()
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            waiting = probe.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%INSERT INTO sources%'"
            ).fetchone()["n"]
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "the Memory insert never blocked on the notebook row"
        publish_tx.commit()
    worker.join(timeout=10)
    assert outcome == {"inserted": False}
    with repo._runtime.database.connect() as db:
        rows = db.execute("SELECT id FROM sources WHERE id='src-race-rev'").fetchall()
    assert rows == []
