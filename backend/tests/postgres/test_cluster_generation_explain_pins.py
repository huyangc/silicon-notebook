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
    assert PostgresMigrator(postgres_database).migrate() == 51
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
    assert PostgresMigrator(postgres_database).migrate() == 51
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


def test_concept_clusters_count_skip_gate_leg_stays_index_only(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 51
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
