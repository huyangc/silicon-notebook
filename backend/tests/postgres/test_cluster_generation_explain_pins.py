"""批 3·W2 PR-1 §5.7 EXPLAIN pin(真 PG):published 代次谓词落地后,三个
被点名的热读者必须保住 Index Only Scan 形态且子查询是一次求值的 InitPlan。

内评实测背书(50 万行复制品):谓词不配 INCLUDE 索引时 `cluster_member_rows`
稳态就退化(buffers 8.8×);`INCLUDE (generation)` 恢复 IOS ≈1×。本文件把
「整改后形态」钉死——回退索引或把谓词写成相关子查询(逐行求值)都会红。
"""
from __future__ import annotations

import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_cluster_generation_explain"),
]


def _seed(postgres_database, notebook_id: str, rows: int) -> None:
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,'g',%s,%s)",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, cluster_generation, updated_at) "
            "VALUES (%s, 0, %s)",
            (notebook_id, now),
        )
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "created_at,generation) "
            "SELECT 'cc-'||g, %s, 'can-'||(g/10), 'ko-'||g, 'N', 'concept', %s, 0 "
            "FROM generate_series(0, %s) g",
            (notebook_id, now, rows - 1),
        )
        db.execute("ANALYZE concept_clusters")
    # VACUUM 设置可见性图——Index Only Scan 的成本模型依赖它;fresh 表不
    # VACUUM 时计划器会合理地选普通 Index Scan(与生产稳态不符)。VACUUM
    # 不能进事务,走独立 autocommit 连接。
    import psycopg

    with psycopg.connect(
        postgres_database.settings.database_url, autocommit=True
    ) as raw:
        raw.execute("VACUUM (ANALYZE) concept_clusters")


def _plan(connection, sql: str, params: tuple) -> str:
    # 0043 先例的 scale-free 判据:关掉 seqscan/bitmapscan,把计划选择聚焦到
    # 「覆盖索引能否独立服务该查询」——测试台几千行不具备生产 9.65M 行的
    # 成本区分度(小表上任何 notebook_id 前导窄索引 + 回表都近似最优),
    # 而这里要钉的是能力问题:INCLUDE (generation) 后 IOS 必须可行。
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


_PUBLISHED = (
    "COALESCE((SELECT cluster_generation FROM unified_kg_state "
    "WHERE notebook_id = %s), 0)"
)


def test_cluster_member_rows_keeps_index_only_scan_with_the_predicate(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, "nb-ios", 5000)
    with postgres_database.connect() as connection:
        plan = _plan(
            connection,
            "SELECT canonical_id, member_object_id FROM concept_clusters "
            "WHERE notebook_id = %s "
            f"AND generation = {_PUBLISHED} "
            'ORDER BY canonical_id COLLATE "C", member_object_id COLLATE "C"',
            ("nb-ios", "nb-ios"),
        )
    assert "Index Only Scan" in plan and "idx_clusters_nb_canonical_member_gen" in plan, plan
    assert "Seq Scan on concept_clusters" not in plan, plan
    # 绑定参数子查询 = uncorrelated InitPlan(一次求值);相关写法是 SubPlan
    assert "InitPlan" in plan and "SubPlan" not in plan.replace("InitPlan", ""), plan


def test_version_facts_cluster_component_scans_the_created_gen_index(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, "nb-vf", 5000)
    with postgres_database.connect() as connection:
        plan = _plan(
            connection,
            "SELECT COUNT(*) AS c, MAX(created_at) AS ts FROM concept_clusters "
            "WHERE notebook_id=%s "
            f"AND generation = {_PUBLISHED}",
            ("nb-vf", "nb-vf"),
        )
    assert "Index Only Scan" in plan and "idx_clusters_nb_created_gen" in plan, plan
    assert "Seq Scan on concept_clusters" not in plan, plan
    assert "InitPlan" in plan, plan


# PR-A·A1:``node_context`` 在来源天花板下的语句都不绑天花板(成员关系在 Python
# 里判),这里钉它们的计划形态,并钉「同一连接执行十几次、切到 generic plan 之后」
# 形态不退化(绑 49k 数组的旧写法在第 ~10 次执行后切到 generic plan,慢一个量级)。
_NC_EXPLAIN_NOTEBOOK = "nb-nc-explain"


def _generic_plan_after_repeats(connection, sql: str, types: str, params: tuple) -> str:
    """同一连接上 PREPARE 后连续 EXECUTE 12 次,再 EXPLAIN EXECUTE:计划缓存此时
    已按代价比较决定是否改用 generic plan,这里拿到的就是之后每次执行的形态。"""
    parts = sql.split("%s")
    numbered = "".join(
        part + (f"${index + 1}" if index < len(parts) - 1 else "")
        for index, part in enumerate(parts)
    )
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    from psycopg import sql as pg_sql

    connection.execute(f"PREPARE nc_plan ({types}) AS {numbered}")
    arguments = pg_sql.SQL(",").join(pg_sql.Literal(value) for value in params)
    execute = pg_sql.SQL("EXECUTE nc_plan ({})").format(arguments)
    for _ in range(12):
        connection.execute(execute).fetchall()
    rows = connection.execute(
        pg_sql.SQL("EXPLAIN (COSTS OFF) {}").format(execute)).fetchall()
    connection.execute("DEALLOCATE nc_plan")
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _seed_node_context_explain(postgres_database) -> None:
    notebook_id = _NC_EXPLAIN_NOTEBOOK
    _seed(postgres_database, notebook_id, 5000)
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "UPDATE concept_clusters SET canonical_description='d' WHERE notebook_id=%s",
            (notebook_id,),
        )
        db.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,payload,evidence,created_at,updated_at) "
            "SELECT 'ko-'||g, %s, CASE WHEN g%%5=0 THEN 'procedure' ELSE 'concept' END, "
            "jsonb_build_object('section_path', 'S'||(g%%50)), "
            "jsonb_build_array(jsonb_build_object('source_id', 'src-'||(g%%7))), %s, %s "
            "FROM generate_series(0, 4999) g",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "SELECT 'ko-'||g, 'src-'||(g%%7), %s FROM generate_series(0, 4999) g",
            (notebook_id,),
        )
        db.execute(
            "INSERT INTO knowledge_relations"
            "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
            "evidence,created_at) "
            "SELECT 'rel-'||g, %s, NULL, 'ko-'||(g+1), 'ko-'||g, "
            "CASE WHEN g%%3=0 THEN 'defines' ELSE 'related_to' END, '[]'::jsonb, %s "
            "FROM generate_series(0, 4998) g",
            (notebook_id, now),
        )
        db.execute("ANALYZE knowledge_objects")
        db.execute("ANALYZE knowledge_object_sources")
        db.execute("ANALYZE knowledge_relations")


@pytest.mark.parametrize("authoritative", [False, True])
def test_node_context_ceiling_queries_never_seq_scan(postgres_database, authoritative):
    """PR-A·A1:``node_context`` 在来源天花板下的簇查询(读回成员来源交给
    Python 判)与来源多于探针上限时的 keyset 续读语句,首次执行与同一连接上执行
    12 次之后(generic plan)两种形态。两条支都不许顺扫 ``concept_clusters`` /
    ``knowledge_object_sources`` / ``knowledge_objects``:成员走
    (notebook_id, canonical_id, ...) 前导索引,来源走 ``knowledge_object_sources``
    的 (object_id, ...) 索引,权威支按主键回表读 evidence。published 代次谓词仍是
    一次求值的 InitPlan。"""
    from app.repositories.postgres.knowledge_store import (
        _node_context_cluster_sources_after_sql, _node_context_cluster_sql,
    )

    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed_node_context_explain(postgres_database)
    notebook_id = _NC_EXPLAIN_NOTEBOOK
    cluster_sql = _node_context_cluster_sql(authoritative=authoritative)
    after_sql = _node_context_cluster_sources_after_sql(authoritative=authoritative)
    cluster_params = (notebook_id, "ko-42", notebook_id)
    after_params = (notebook_id, "can-4", 0, "src-1")
    plans = {}
    with postgres_database.connect() as connection:
        plans["cluster"] = _plan(connection, cluster_sql, cluster_params)
    with postgres_database.connect() as connection:
        plans["after"] = _plan(connection, after_sql, after_params)
    with postgres_database.connect() as connection:
        plans["cluster@12"] = _generic_plan_after_repeats(
            connection, cluster_sql, "text, text, text", cluster_params)
    with postgres_database.connect() as connection:
        plans["after@12"] = _generic_plan_after_repeats(
            connection, after_sql, "text, text, bigint, text", after_params)
    for label, plan in plans.items():
        for table in ("concept_clusters", "knowledge_object_sources", "knowledge_objects"):
            assert f"Seq Scan on {table}" not in plan, (label, plan)
        if not authoritative:
            assert "knowledge_object_sources" in plan, (label, plan)
    assert "InitPlan" in plans["cluster"], plans["cluster"]
    assert "InitPlan" in plans["cluster@12"], plans["cluster@12"]


def test_node_context_legacy_sibling_queries_never_seq_scan(postgres_database):
    """legacy 兄弟过程查询(都不绑天花板):有 section 的一路、无天花板时无
    section 的 ``LIMIT 500`` 一路,都走 knowledge_objects 的
    (notebook_id, object_type, ...) 索引;天花板下无 section 的 keyset 翻页沿
    (notebook_id, object_type, created_at, id) 索引按序读、不排序,首次执行与同一
    连接执行 12 次之后形态相同。"""
    from app.repositories.postgres.knowledge_store import (
        _LEGACY_SIBLINGS_BY_SECTION_SQL, _LEGACY_SIBLINGS_UNSECTIONED_PAGE_SQL,
        _LEGACY_SIBLINGS_UNSECTIONED_SQL,
    )

    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed_node_context_explain(postgres_database)
    notebook_id = _NC_EXPLAIN_NOTEBOOK
    first_page = (notebook_id, normalize_timestamp("0001-01-01T00:00:00+00:00"), "")
    plans = {}
    with postgres_database.connect() as connection:
        plans["sectioned"] = _plan(
            connection, _LEGACY_SIBLINGS_BY_SECTION_SQL, (notebook_id, "S5"))
    with postgres_database.connect() as connection:
        plans["unsectioned"] = _plan(
            connection, _LEGACY_SIBLINGS_UNSECTIONED_SQL, (notebook_id,))
    with postgres_database.connect() as connection:
        plans["page"] = _plan(connection, _LEGACY_SIBLINGS_UNSECTIONED_PAGE_SQL, first_page)
    with postgres_database.connect() as connection:
        plans["page@12"] = _generic_plan_after_repeats(
            connection, _LEGACY_SIBLINGS_UNSECTIONED_PAGE_SQL,
            "text, timestamptz, text", first_page)
    for label, plan in plans.items():
        assert "Seq Scan on knowledge_objects" not in plan, (label, plan)
        assert "idx_knowledge_objects_nb_type" in plan, (label, plan)
    for label in ("page", "page@12"):
        assert "idx_knowledge_objects_nb_type_created" in plans[label], plans[label]
        assert "Sort" not in plans[label], plans[label]


def test_node_context_defines_query_walks_the_target_index_in_id_order(postgres_database):
    """有序、有界的 ``defines`` 回落(台账 B-8 的两道状态过滤):沿
    knowledge_relations(notebook_id, target_object_id, id) 索引按 id 序读、不排序,
    定义者按主键回表,不顺扫。"""
    from app.repositories.postgres.knowledge_store import _NODE_CONTEXT_DEFINES_SQL

    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed_node_context_explain(postgres_database)
    params = (_NC_EXPLAIN_NOTEBOOK, "ko-42", 8)
    with postgres_database.connect() as connection:
        plan = _plan(connection, _NODE_CONTEXT_DEFINES_SQL, params)
    with postgres_database.connect() as connection:
        repeated = _generic_plan_after_repeats(
            connection, _NODE_CONTEXT_DEFINES_SQL, "text, text, bigint", params)
    for shape in (plan, repeated):
        for table in ("knowledge_relations", "knowledge_objects"):
            assert f"Seq Scan on {table}" not in shape, shape
        assert "Sort" not in shape, shape
        assert "pk_knowledge_objects" in shape, shape


def test_concept_clusters_count_skip_gate_leg_stays_index_only(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, "nb-cnt", 5000)
    with postgres_database.connect() as connection:
        plan = _plan(
            connection,
            "SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=%s "
            f"AND generation = {_PUBLISHED}",
            ("nb-cnt", "nb-cnt"),
        )
    assert "Index Only Scan" in plan, plan
    assert "Seq Scan on concept_clusters" not in plan, plan
