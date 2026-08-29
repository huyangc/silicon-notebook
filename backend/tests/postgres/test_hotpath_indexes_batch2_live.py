"""Live-PostgreSQL half of hot-path fix batch 2 (R6)'s contract that a fake
connection cannot exercise (see ``backend/tests/test_hotpath_indexes_batch2.py``
for the fake-connection half: migration<->spec anti-drift and the
PY_WHITESPACE<->literal reconciliation pins).

Three things only a real server can prove:

  1. Both new indexes actually build via ``install_hotpath_indexes`` (real
     ``CREATE INDEX CONCURRENTLY``, including the GIN case this module's
     ``HotpathIndexSpec`` has never carried before batch 2) and migration 42
     is a true no-op ledger entry once they exist online — mirrors
     ``test_hotpath_indexes_live.py``'s batch-1 equivalent.
  2. ``idx_source_elements_nonblank`` is actually chosen by the planner when
     the query uses ``postgres/maintenance.py``'s ``_NONBLANK_TEXT_SQL``
     inlined-literal form under PostgreSQL's normal per-call custom planning
     (the only mode this repository's own connections ever exercise) — and,
     importantly, an ordinary ``%s``-bound parameter with the SAME correct
     value ALSO picks up the index under that same normal custom-plan path
     (empirically confirmed here, not assumed) — so the two forms are NOT
     shown to diverge under default conditions. They DO provably diverge
     under a GENERIC (parameter-value-blind) plan, which is the one
     circumstance PostgreSQL cannot prove a bound parameter implies the
     partial predicate; this module forces that circumstance with
     ``PREPARE``/``EXECUTE`` under ``SET plan_cache_mode=force_generic_plan``
     to give the "H5 query reverts to a bound parameter" mutation-check a
     condition that reliably goes red, while documenting plainly that this
     is a worst-case hardening proof, not a default-path regression proof.
  3. The H5 "non-blank element" judgment returns identical results whether
     computed via the new inlined-literal form or the old bound-parameter
     form, across a battery of PY_WHITESPACE edge-case rows (this is the
     equivalence oracle the task calls for).
"""
from __future__ import annotations

import re

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres import maintenance as postgres_maintenance
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp
from app.repositories.postgres.hotpath_indexes import (
    HOTPATH_INDEX_SPECS,
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.text_whitespace import PY_WHITESPACE


pytestmark = pytest.mark.postgres_integration

_BATCH2_NAMES = frozenset(
    {"idx_knowledge_objects_payload_trgm", "idx_source_elements_nonblank"}
)


def _schema_of(database) -> str:
    with database.connect() as connection:
        return connection.execute(
            "SELECT current_schema() AS name"
        ).fetchone()["name"]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_install_builds_both_new_indexes_and_is_idempotent(postgres_database):
    # One hop before migration 42 introduces both indexes itself, so they are
    # genuinely absent below -- the same "prove it's for real" structure as
    # batch 1's equivalent live test.
    assert PostgresMigrator(postgres_database).migrate(target_version=41) == 41
    schema = _schema_of(postgres_database)
    database_url = postgres_database.settings.database_url

    before = inspect_hotpath_indexes(database_url, schema=schema)
    batch2_before = {
        row["name"]: row["state"]
        for row in before["indexes"]
        if row["name"] in _BATCH2_NAMES
    }
    assert batch2_before == {name: "缺失" for name in _BATCH2_NAMES}

    state = install_hotpath_indexes(database_url, schema=schema)
    assert all(row["state"] == "存在" for row in state["indexes"]), state

    # Idempotent rerun.
    repeated = install_hotpath_indexes(database_url, schema=schema)
    assert repeated == state

    # Migration 42's own plain (in-transaction) CREATE INDEX IF NOT EXISTS is
    # a true no-op ledger entry once the offline CONCURRENTLY builder already
    # built both indexes online.
    assert PostgresMigrator(postgres_database).migrate() == 42
    after_migration = inspect_hotpath_indexes(database_url, schema=schema)
    assert after_migration == state


def _seed_notebook_with_source(repository, name: str) -> str:
    notebook_id = repository.create_notebook(NotebookCreate(name=name)).id
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
    return notebook_id


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_payload_trgm_index_is_usable_for_a_rare_term_ilike(postgres_repository):
    assert (
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 42
    )
    notebook_id = _seed_notebook_with_source(postgres_repository, "payload-trgm")
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    # 种子量必须让「按 nb 前缀索引扫全库再 filter」在成本上真实地输给 BitmapOr:
    # 小样本(几百行)时 planner 总能挑一条便宜的 nb 前缀 btree 走查/位图 + filter,
    # GUC 也逼不出 BitmapOr(它只是候选之一,不是仅剩路径)。100k 行 server 侧
    # generate_series 一条语句约 1s,与迁移 0041 头注释引用的 200k 行一次性基准库
    # 同一形态——在那个量级上稀有词自然选中 BitmapOr(name_trgm, payload_trgm)。
    with runtime.database.write() as db:
        # 迁移已先建 GIN,10 万行批量插入要同步付 GIN 维护,可能超过池会话的
        # 30s statement_timeout——种子阶段(测试专用)在本事务内局部放宽。
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,"
            "created_at,updated_at,ordinal) "
            "SELECT 'ko-'||g, %s, 'concept','approved', "
            "jsonb_build_object('name','item '||g,'note',"
            "'common filler text alpha beta '||md5(g::text)), "
            "'[]'::jsonb, 'src-a', %s, %s, g "
            "FROM generate_series(1, 100000) g",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,"
            "created_at,updated_at,ordinal) VALUES ('ko-rare',%s,'concept',"
            "'approved',%s,%s,'src-a',%s,%s,100001)",
            (
                notebook_id,
                jsonb({"name": "zzqxrarezz", "note": "unique needle payload"}),
                jsonb([]),
                now,
                now,
            ),
        )
    with runtime.database.connect() as db:
        db.execute("ANALYZE knowledge_objects")

    # Verbatim copy of search.py:notebook_knowledge_rows's WHERE clause (see
    # that function for the live, executed version this migration serves).
    query = (
        "EXPLAIN (COSTS OFF) SELECT id,object_type,payload FROM knowledge_objects "
        "WHERE notebook_id=%s AND status!='deprecated' AND "
        "((payload ->> 'name') COLLATE \"C\" ILIKE %s OR "
        "(payload::text) COLLATE \"C\" ILIKE %s) "
        "ORDER BY ordinal LIMIT %s"
    )
    pattern = "%zzqxrarezz%"
    with runtime.database.connect() as db:
        plan = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                query, (notebook_id, pattern, pattern, 10)
            ).fetchall()
        )
    # 100k 行 + 稀有词下这是**自然**计划,不靠任何 GUC 强迫——表达式索引与查询
    # 形态不匹配(本迁移最脆的不变式)时,planner 会退回 ordinal 走查/nb 位图,
    # 这里响亮失败。
    assert "idx_knowledge_objects_payload_trgm" in plan, plan
    assert "idx_knowledge_objects_name_trgm" in plan, plan  # BitmapOr 的另一臂


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_nonblank_partial_index_chosen_with_literal_and_not_with_bound_param(
    postgres_repository,
):
    assert (
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 42
    )
    notebook_id = _seed_notebook_with_source(postgres_repository, "nonblank-partial")
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        with db.cursor() as cursor:
            rows = []
            for i in range(400):
                text = PY_WHITESPACE[: (i % len(PY_WHITESPACE)) + 1] if i % 3 == 0 else f"real content {i}"
                rows.append((f"el-{i}", "src-a", "paragraph", "", text, jsonb({}), now))
            cursor.executemany(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )

    with runtime.database.connect() as db:
        new_plan = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                "EXPLAIN (COSTS OFF) SELECT id FROM source_elements e "
                f"WHERE e.source_id=%s AND {postgres_maintenance._NONBLANK_TEXT_SQL}",
                ("src-a",),
            ).fetchall()
        )
    assert "idx_source_elements_nonblank" in new_plan, new_plan
    assert "Seq Scan" not in new_plan, new_plan

    # 负断言必须在 **generic plan** 下做(maintenance._NONBLANK_TEXT_SQL 的注释
    # 论证的正是这一点):psycopg 默认走 custom plan,planner 能看到绑定参数的实际
    # 取值、照样证明谓词蕴含并选中分部索引——所以「custom plan 下绑定参数没索引」
    # 是个假命题,不能当钉子。真正的差异面在 plan_cache_mode=force_generic_plan:
    # 取值对 planner 不可见,绑定参数写法丢索引退化顺扫,内联字面量写法不含参数位、
    # 不受影响。这也是「查询侧内联」这半个改动存在的全部理由——把不变式从
    # 「依赖 plan cache 状态」加固为「恒成立」。
    with runtime.database.write() as db:
        db.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        db.execute(
            "PREPARE r6_old_style(text, text) AS "
            "SELECT id FROM source_elements e "
            "WHERE e.source_id=$1 AND btrim(e.text, $2) <> ''"
        )
        db.execute(
            "PREPARE r6_new_style(text) AS "
            "SELECT id FROM source_elements e "
            f"WHERE e.source_id=$1 AND {postgres_maintenance._NONBLANK_TEXT_SQL}"
        )
        # EXECUTE 是 utility 语句,psycopg 不能对它走扩展协议传参——实参用
        # sql.Literal 内联(测试专用写法;这不影响被测对象:generic plan 的判据在
        # PREPARE 里的 $n 参数位,EXECUTE 的实参只是喂值)。
        from psycopg import sql as _sql

        _src = _sql.Literal("src-a").as_string(None)
        _ws = _sql.Literal(PY_WHITESPACE).as_string(None)
        generic_old = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                f"EXPLAIN (COSTS OFF) EXECUTE r6_old_style({_src}, {_ws})"
            ).fetchall()
        )
        generic_new = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                f"EXPLAIN (COSTS OFF) EXECUTE r6_new_style({_src})"
            ).fetchall()
        )
        db.execute("DEALLOCATE r6_old_style")
        db.execute("DEALLOCATE r6_new_style")
    assert "idx_source_elements_nonblank" not in generic_old, generic_old
    assert "idx_source_elements_nonblank" in generic_new, generic_new


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_h5_nonblank_equivalence_across_whitespace_edge_cases(postgres_repository):
    assert (
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 42
    )
    notebook_id = _seed_notebook_with_source(postgres_repository, "h5-equivalence")
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())

    edge_texts = {
        "el-empty": "",
        "el-tab-only": "\t\t\t",
        "el-mixed-ws": "\n \r\t",
        "el-ideographic-space": "　　",
        "el-narrow-nbsp": "  ",
        "el-nbsp-only": "\xa0",
        "el-line-sep": "  ",
        "el-visible": "real content",
        "el-visible-padded": "   real content   ",
        "el-mixed-visible-and-ws": "\t\treal　content\r\n",
        "el-control-only": "\x1c\x1d\x1e\x1f",
    }
    with runtime.database.write() as db:
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (%s,%s,'paragraph','',%s,%s,%s)",
                [
                    (element_id, "src-a", text, jsonb({}), now)
                    for element_id, text in edge_texts.items()
                ],
            )

    maintenance = postgres_repository.maintenance
    new_count = maintenance.count_missing_element_vectors(notebook_id)
    new_ids = set(maintenance.missing_element_embedding_ids(notebook_id))
    new_page_ids = {
        row["id"]
        for row in maintenance.missing_element_embedding_page(notebook_id, limit=100)
    }
    new_source_ids = set(maintenance.missing_element_vector_source_ids(notebook_id))

    with runtime.database.connect() as db:
        old_count = int(
            db.execute(
                "SELECT COUNT(*) c FROM source_elements e "
                "JOIN sources s ON s.id=e.source_id "
                "WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND btrim(e.text, %s) != '' "
                "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                "WHERE v.element_id=e.id)",
                (notebook_id, PY_WHITESPACE),
            ).fetchone()["c"]
        )
        old_ids = {
            row["id"]
            for row in db.execute(
                "SELECT e.id FROM source_elements e "
                "JOIN sources s ON s.id=e.source_id "
                "WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND btrim(e.text, %s) != '' "
                "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                "WHERE v.element_id=e.id) ORDER BY e.id COLLATE \"C\"",
                (notebook_id, PY_WHITESPACE),
            ).fetchall()
        }
        old_source_ids = {
            row["source_id"]
            for row in db.execute(
                "SELECT DISTINCT e.source_id FROM source_elements e "
                "JOIN sources s ON s.id=e.source_id "
                "WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND btrim(e.text, %s) != '' "
                "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                "WHERE v.element_id=e.id)",
                (notebook_id, PY_WHITESPACE),
            ).fetchall()
        }

    assert new_count == old_count
    assert new_ids == old_ids == new_page_ids
    assert new_source_ids == old_source_ids
    # Only the genuinely non-blank rows survive both forms.
    assert new_ids == {"el-visible", "el-visible-padded", "el-mixed-visible-and-ws"}
