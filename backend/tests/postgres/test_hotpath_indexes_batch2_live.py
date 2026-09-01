"""Live-PostgreSQL half of hot-path fix batch 2 (R6)'s contract that a fake
connection cannot exercise (see ``backend/tests/test_hotpath_indexes_batch2.py``
for the fake-connection half: migration<->spec anti-drift and the
PY_WHITESPACE<->literal reconciliation pins).

Things only a real server can prove:

  1. Both new indexes actually build via ``install_hotpath_indexes`` (real
     ``CREATE INDEX CONCURRENTLY``, including the composite partial GIN case
     this module's ``HotpathIndexSpec`` has never carried before batch 2) and
     migration 42 is a true no-op ledger entry once they exist online —
     mirrors ``test_hotpath_indexes_live.py``'s batch-1 equivalent, and (new
     with codex #636 R1 P2) also exercises the migration's pre-existing-index
     validation DO block on its accept path — including the per-key
     pg_get_indexdef echo the DO block compares, so a PostgreSQL deparser
     rendering change fails here loudly. The reject paths — a same-named
     wrong-shape index, and a REAL INVALID residue row (a failed
     CREATE UNIQUE INDEX CONCURRENTLY over duplicate data, no superuser
     needed) — get their own tests below, as does the accept path for an
     index carrying reloptions (``SET (fastupdate = off)``), which the DO
     block must tolerate because it compares semantic catalog dimensions,
     not the full indexdef text. The DO block's expected values are
     reconciled against ``HOTPATH_INDEX_SPECS`` in the unit half
     (``test_hotpath_indexes_batch2.py``).
  1b. The composite payload GIN keeps ``notebook_id`` INSIDE index access
     (codex #636 R1 P1): a term concentrated in OTHER notebooks must not
     build a global bitmap that survives to heap recheck — the failure mode
     docs/operations.md documents for the legacy single-expression trigram
     indexes.
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

import psycopg
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
    {"idx_knowledge_objects_nb_payload_trgm", "idx_source_elements_nonblank"}
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
    assert PostgresMigrator(postgres_database).migrate() == 47
    after_migration = inspect_hotpath_indexes(database_url, schema=schema)
    assert after_migration == state


def _seed_notebook_with_source(repository, name: str, source_id: str = "src-a") -> str:
    notebook_id = repository.create_notebook(NotebookCreate(name=name)).id
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            "VALUES (%s,%s,'source','markdown','extracted','parsed',"
            "'a.md','',0,%s,'',%s,%s,'textbook')",
            (source_id, notebook_id, f"hash-{source_id}", now, now),
        )
    return notebook_id


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_payload_trgm_index_is_usable_for_a_rare_term_ilike(postgres_repository):
    assert (
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 47
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
    with runtime.database.write() as db:
        # ANALYZE 在 100k 行上本机 0.15s,但 CI runner 慢 3-5 倍且本仓库有
        # 计时器用例被 CPU 抢占挤成假回归的前科——与种子同样局部放宽。
        db.execute("SET LOCAL statement_timeout = '0'")
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
    assert "idx_knowledge_objects_nb_payload_trgm" in plan, plan
    assert "idx_knowledge_objects_name_trgm" in plan, plan  # BitmapOr 的另一臂
    # codex #636 R1 P1 的结构性判据:复合索引臂的 Index Cond 必须把 notebook_id
    # 等值带进索引访问本身——这正是「全局位图、heap recheck 才丢行」教训
    # (docs/operations.md)的反面。谓词形对了但 notebook_id 没进 Index Cond
    # (例如有人把索引改回单表达式全局形)时,这条正则响亮失败。
    assert re.search(
        r"Bitmap Index Scan on idx_knowledge_objects_nb_payload_trgm\n"
        r"\s+Index Cond: \(\(notebook_id = ",
        plan,
    ), plan


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_payload_index_stays_notebook_scoped_when_term_lives_in_another_notebook(
    postgres_repository,
):
    """codex #636 R1 P1 的场景重演:词在**别的** notebook 里高频、在被查的
    notebook 里不存在。legacy 单表达式全局 GIN 在这里会先建 ~2 万行的全局位图、
    到 heap recheck 才按 notebook 丢行(docs/operations.md 已记录的超时教训);
    复合形必须把 notebook 等值带进索引访问,位图从一开始就是空的。"""
    assert (
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 47
    )
    nb_queried = _seed_notebook_with_source(
        postgres_repository, "cross-nb-queried", source_id="src-a"
    )
    nb_other = _seed_notebook_with_source(
        postgres_repository, "cross-nb-other", source_id="src-b"
    )
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        # 被查 notebook:100k 行填充,不含探针词——量级要大到「nb 前缀 btree
        # 位图 + heap filter 全过一遍」真实地贵过复合 GIN 臂,否则 planner 对
        # 小 notebook 选 nb 前缀位图(那也是 notebook 内的,不是本条要防的
        # 全局位图病灶,但会让断言落空)。
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,"
            "created_at,updated_at,ordinal) "
            "SELECT 'ko-q-'||g, %s, 'concept','approved', "
            "jsonb_build_object('name','item '||g,'note','filler '||md5(g::text)), "
            "'[]'::jsonb, 'src-a', %s, %s, g FROM generate_series(1, 100000) g",
            (nb_queried, now, now),
        )
        # 另一个 notebook:2 万行全部含探针词——词在全库层面高度可选中,
        # 但与被查 notebook 零交集。
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,"
            "created_at,updated_at,ordinal) "
            "SELECT 'ko-o-'||g, %s, 'concept','approved', "
            "jsonb_build_object('name','item '||g,'note',"
            "'crossnbneedle payload '||md5(g::text)), "
            "'[]'::jsonb, 'src-b', %s, %s, 100000+g "
            "FROM generate_series(1, 20000) g",
            (nb_other, now, now),
        )
    with runtime.database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute("ANALYZE knowledge_objects")

    query_body = (
        "SELECT id,object_type,payload FROM knowledge_objects "
        "WHERE notebook_id=%s AND status!='deprecated' AND "
        "((payload ->> 'name') COLLATE \"C\" ILIKE %s OR "
        "(payload::text) COLLATE \"C\" ILIKE %s) "
        "ORDER BY ordinal LIMIT %s"
    )
    pattern = "%crossnbneedle%"
    with runtime.database.write() as db:
        # 高频词的自然计划本来就可能选 ordinal 走查(LIMIT 早停),那不是这条
        # 要钉的性质——这里强制 bitmap 路径,专门检查「一旦走本索引,位图是否
        # notebook 内」。enable_indexscan 关的是 plain index scan(ordinal 走查),
        # bitmap 家族由 enable_bitmapscan 单独控制,不受影响。
        db.execute(
            "SET LOCAL enable_seqscan = off; SET LOCAL enable_indexscan = off"
        )
        plan = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
                + query_body,
                (nb_queried, pattern, pattern, 10),
            ).fetchall()
        )
    match = re.search(
        r"Bitmap Index Scan on idx_knowledge_objects_nb_payload_trgm[^\n]*"
        r"\(actual rows=(\d+)[^\n]*\)\n\s+Index Cond: \(\(notebook_id = ",
        plan,
    )
    assert match, plan
    # 位图行数是**索引访问返回**的 TID 数:notebook 内交集为空 → 0;
    # 全局形会在这里返回 ~20000(另一 notebook 的所有命中行)。
    assert int(match.group(1)) == 0, plan
    # 真实执行结果:被查 notebook 里确实没有这个词。
    with runtime.database.connect() as db:
        rows = db.execute(
            query_body, (nb_queried, pattern, pattern, 10)
        ).fetchall()
    assert rows == []


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_nonblank_partial_index_chosen_with_literal_and_not_with_bound_param(
    postgres_repository,
):
    assert (
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 47
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

    # docstring 里「绑定参数在 custom plan 下同样命中索引」那半的实证就在这里:
    # psycopg 默认逐次 custom plan,planner 能看到实参值并证明谓词蕴含。
    with runtime.database.connect() as db:
        custom_old = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                "EXPLAIN (COSTS OFF) SELECT id FROM source_elements e "
                "WHERE e.source_id=%s AND btrim(e.text, %s) <> ''",
                ("src-a", PY_WHITESPACE),
            ).fetchall()
        )
    assert "idx_source_elements_nonblank" in custom_old, custom_old

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
        PostgresMigrator(postgres_repository._runtime.database).migrate() == 47
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


# ---------------------------------------------------------------------------
# codex #636 R1 P2: migration 42 must validate a pre-existing same-named index
# before recording its ledger entry -- IF NOT EXISTS alone would silently skip
# creation over an INVALID residue row or an operator's wrong-shape index and
# still mark the migration applied.
# ---------------------------------------------------------------------------

_COMPOSITE_GIN_DDL = (
    "CREATE INDEX idx_knowledge_objects_nb_payload_trgm "
    "ON knowledge_objects USING gin ("
    "notebook_id public.text_ops, "
    '((payload::text) COLLATE "C") public.gin_trgm_ops'
    ") WHERE status != 'deprecated'"
)


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_migration_rejects_a_same_named_wrong_shape_index(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=41) == 41
    with postgres_database.write() as db:
        db.execute(
            "CREATE INDEX idx_knowledge_objects_nb_payload_trgm "
            "ON knowledge_objects USING btree (notebook_id)"
        )
    with pytest.raises(
        psycopg.errors.RaiseException, match="does not match the expected definition"
    ):
        migrator.migrate()
    # 账本没有前进——RAISE 让整个迁移事务(含 ledger INSERT)回滚。
    assert migrator.migrate(target_version=41) == 41
    # 运维按报错指引清掉同名冲突后,迁移正常走完。
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_knowledge_objects_nb_payload_trgm")
    assert migrator.migrate() == 47


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_migration_rejects_a_same_named_index_on_the_wrong_table(postgres_database):
    """codex #636 R2 P2 的场景 1:1:chunks 恰好也有 (source_id, id, text) 且
    三列同为 COLLATE "C",在它上面建同名同形 partial 索引,除表归属外每一维都
    与期望一致。索引名是 schema 级唯一的,`CREATE INDEX IF NOT EXISTS` 会因此
    静默跳过真正要建的 source_elements 索引——DO 块必须按 indrelid 拒绝。"""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=41) == 41
    nonblank_spec = next(
        spec for spec in HOTPATH_INDEX_SPECS
        if spec.name == "idx_source_elements_nonblank"
    )
    with postgres_database.write() as db:
        db.execute(
            "CREATE INDEX idx_source_elements_nonblank ON chunks(source_id, id) "
            f"WHERE {nonblank_spec.predicate}"
        )
    with pytest.raises(
        psycopg.errors.RaiseException, match="does not match the expected definition"
    ):
        migrator.migrate()
    assert migrator.migrate(target_version=41) == 41
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_source_elements_nonblank")
    assert migrator.migrate() == 47


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_migration_rejects_an_invalid_same_named_index(postgres_database):
    """真实的 INVALID 残留,不做 superuser 目录手术(质量评审 P4:CI 的 PG 角色
    是 NOSUPERUSER,靠翻 pg_index 的版本在 CI 恒 skip,分支等于零覆盖):对已有
    两行同 notebook_id 的表跑 CREATE UNIQUE INDEX CONCURRENTLY,第二阶段唯一性
    失败会留下 indisvalid=false 的目录行——与中断的 CONCURRENTLY 构建同形。
    DO 块先查 INVALID 再查形态,所以 match="INVALID" 同时区分了两条分支。"""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=41) == 41
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id, name, created_at, updated_at) "
            "VALUES ('nb-inv', 'invalid-residue', now(), now())"
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, created_at, updated_at, ordinal) "
            "VALUES ('ko-inv-1', 'nb-inv', 'concept', now(), now(), 1), "
            "('ko-inv-2', 'nb-inv', 'concept', now(), now(), 2)"
        )
    with psycopg.connect(
        postgres_database.settings.database_url, autocommit=True
    ) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY "
                "idx_knowledge_objects_nb_payload_trgm "
                "ON knowledge_objects (notebook_id)"
            )
        residue = conn.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() "
            "AND c.relname = 'idx_knowledge_objects_nb_payload_trgm'"
        ).fetchone()
    assert residue is not None and residue[0] is False
    with pytest.raises(psycopg.errors.RaiseException, match="INVALID"):
        migrator.migrate()
    assert migrator.migrate(target_version=41) == 41
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_knowledge_objects_nb_payload_trgm")
    assert migrator.migrate() == 47


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch2")
def test_migration_accepts_a_prebuilt_index_with_reloptions(postgres_database):
    """质量评审 P1 的反面钉:DO 块比对的是与 _matches_shape 相同的语义维度,
    不是 pg_get_indexdef 全文——运维对这条登记过写放大债的 GIN 做标准缓解
    `SET (fastupdate = off)`(会进 indexdef 的 WITH 子句)后,迁移必须照常通过,
    且 inspect 与迁移对同一目录行给同一结论。"""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=41) == 41
    with postgres_database.write() as db:
        db.execute(_COMPOSITE_GIN_DDL)
        db.execute(
            "ALTER INDEX idx_knowledge_objects_nb_payload_trgm "
            "SET (fastupdate = off)"
        )
    assert migrator.migrate() == 47
    schema = _schema_of(postgres_database)
    state = inspect_hotpath_indexes(
        postgres_database.settings.database_url, schema=schema
    )
    by_name = {row["name"]: row["state"] for row in state["indexes"]}
    assert by_name["idx_knowledge_objects_nb_payload_trgm"] == "存在"
