"""`memory_sql` 六个片段的 EXPLAIN pin(真 PG)。

E0 没有新增生产语句,但 E3/E4 会把这些片段嵌进大量读者;这里先钉住片段**单独**在一个
非平凡数据集上的计划形态,让后续任务嵌入时有基线可比(计划第 5 节通用红线:新增 SQL
配 EXPLAIN pin)。

判据沿用 `test_cluster_generation_explain_pins.py`:关掉 seqscan/bitmapscan,把断言聚焦在
「有没有可用的索引路径」——测试台几千行没有生产规模的成本区分度,但一旦片段被改写成
没有索引路径的形状,计划里就会出现 Seq Scan。钉的是**规划器实际选中**的索引:

* Memory 来源集合走 `idx_sources_nb_hidden_type`(按 `source_type` 探,全库 Memory 来源
  数量很小);
* `memory_items` 走属主索引 `idx_memory_owner_notebook_status`(`created_by = 查看者`);
  readable 是一次求值的 hashed SubPlan,不是逐行相关求值;
* 簇片段的内层走 `idx_knowledge_objects_source_id`(Index Only Scan)与
  `idx_clusters_member`;
* foreign / derived / cluster 都被规划器去相关成反连接(`Anti Join`),不含 SubPlan。
"""
from __future__ import annotations

import pytest

from app.repositories.postgres import memory_sql
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_sql_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"
_TABLES = (
    "sources",
    "memory_items",
    "knowledge_objects",
    "knowledge_relations",
    "concept_clusters",
)


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for user in ("u-a", "u-b"):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user, f"{user}@example.test", user, _NOW, _NOW, user),
            )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb','N','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'mem-'||g,'nb',CASE WHEN g%%2=0 THEN 'u-a' ELSE 'u-b' END,"
            "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(0,199) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||g,'nb','t',"
            "CASE WHEN g<200 THEN 'memory' WHEN g<400 THEN 'knowhow' ELSE 'upload' END,"
            "CASE WHEN g<200 THEN 'mem-'||g END,%s,%s FROM generate_series(0,3999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "created_at,updated_at) SELECT 'ko-'||g,'nb','concept','approved',"
            "'src-'||(g%%4000),%s,%s FROM generate_series(0,7999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) SELECT 'kr-'||g,'nb','src-'||(g%%4000),"
            "'ko-0','ko-1','r',%s FROM generate_series(0,7999) g",
            (_NOW,),
        )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "SELECT 'cc-'||g,'nb','can-'||(g/3),'ko-'||g,'n','concept',%s,0 "
            "FROM generate_series(0,5999) g",
            (_NOW,),
        )
        for table in _TABLES:
            db.execute(f"ANALYZE {table}")
    # VACUUM 设置可见性图(Index Only Scan 的成本模型依赖它),不能进事务。
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


def _plan(connection, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _plans(postgres_database) -> dict[str, str]:
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    queries = {
        "readable": (
            "SELECT s.id FROM sources s WHERE s.notebook_id=%s AND "
            + memory_sql.memory_source_readable("s"),
            ("nb", "u-a"),
        ),
        "foreign_object": (
            "SELECT count(*) FROM knowledge_objects o WHERE o.notebook_id=%s AND "
            + memory_sql.foreign_memory_object_excluded("o"),
            ("nb", "u-a"),
        ),
        "foreign_relation": (
            "SELECT count(*) FROM knowledge_relations r WHERE r.notebook_id=%s AND "
            + memory_sql.foreign_memory_relation_excluded("r"),
            ("nb", "u-a"),
        ),
        "derived_object": (
            "SELECT count(*) FROM knowledge_objects o WHERE o.notebook_id=%s AND NOT "
            + memory_sql.memory_derived_object("o"),
            ("nb",),
        ),
        "derived_relation": (
            "SELECT count(*) FROM knowledge_relations r WHERE r.notebook_id=%s AND NOT "
            + memory_sql.memory_derived_relation("r"),
            ("nb",),
        ),
        "cluster": (
            "SELECT count(*) FROM concept_clusters c WHERE c.notebook_id=%s "
            "AND c.generation=0 AND " + memory_sql.no_memory_member_cluster("c"),
            ("nb",),
        ),
    }
    plans = {}
    with postgres_database.connect() as connection:
        for name, (sql, params) in queries.items():
            plans[name] = _plan(connection, sql, params)
    return plans


def test_memory_sql_fragments_keep_their_index_paths(postgres_database):
    plans = _plans(postgres_database)

    for name, plan in plans.items():
        assert "Seq Scan" not in plan, (name, plan)

    # readable:一次求值的 hashed SubPlan,内层走属主索引,不是逐行相关求值。
    readable = plans["readable"]
    assert "hashed SubPlan" in readable, readable
    assert "idx_memory_owner_notebook_status" in readable, readable

    # foreign:Memory 来源集合 + 属主索引,整体被去相关成反连接。
    for name in ("foreign_object", "foreign_relation"):
        plan = plans[name]
        assert "Anti Join" in plan and "SubPlan" not in plan, (name, plan)
        assert "idx_sources_nb_hidden_type" in plan, (name, plan)
        assert "idx_memory_owner_notebook_status" in plan, (name, plan)

    # derived:只探 Memory 来源集合。
    for name in ("derived_object", "derived_relation"):
        plan = plans[name]
        assert "Anti Join" in plan and "SubPlan" not in plan, (name, plan)
        assert "idx_sources_nb_hidden_type" in plan, (name, plan)

    # cluster:Memory 来源 → 其对象(覆盖索引)→ 成员簇行,逐层点查。
    cluster = plans["cluster"]
    assert "Anti Join" in cluster and "SubPlan" not in cluster, cluster
    assert "idx_sources_nb_hidden_type" in cluster, cluster
    assert "Index Only Scan using idx_knowledge_objects_source_id" in cluster, cluster
    assert "idx_clusters_member" in cluster, cluster
