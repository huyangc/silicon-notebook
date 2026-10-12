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
* 簇片段的内层按来源逐层点查:knowledge_objects 走任一条 `source_id` 前导索引
  (`idx_knowledge_objects_source_id` 或 `idx_knowledge_objects_source`),且它的**每个**
  扫描节点都带以 `source_id` 为键的 `Index Cond`,再走 `idx_clusters_member`;
* foreign / derived / cluster 都被规划器去相关成反连接(`Anti Join`),不含 SubPlan。

簇片段不钉 Index Only Scan(C6):可见性图未设置时(同库有旁路长事务挡住 VACUUM),
覆盖索引 `(source_id, id)` 与普通 `(source_id)` 索引的成本相当,规划器在两者间换着选
——都是按来源点查,都可接受。要挡的是另一种翻转:`pk_knowledge_objects` 全索引扫描 +
Hash Join(关掉 seqscan 后全表扫描就长这样,生产上是扫遍全部对象),它的扫描节点
没有 `source_id` 的 Index Cond,会红。种子里另放一个笔记本的一大批对象,让全扫在
成本上与按来源点查拉开(片段内层的 mo 没有笔记本谓词,全扫会读到它们)。整条用例在
「无旁路事务」与「旁路 REPEATABLE READ 事务一直持有」两种状态下各跑一次
(``bystander_snapshot``)。

不用 cluster 钉那种「只留目标索引」的 EXPLAIN(``explain_with_sole_index``):要钉的恰是
「按来源点查胜过 pk 全扫」这个竞争选择,删掉 pk 就没有可比的灾难计划了(pk 还被外键
引用,helper 会拒绝)。
"""
from __future__ import annotations

import re

import pytest

from app.repositories.postgres import memory_sql
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_sql_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"
_OTHER_NOTEBOOK_OBJECTS = 20_000
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
        # Another notebook's objects (no Memory there, two per source): the
        # cluster fragment's inner ``mo`` has no notebook predicate, so a full
        # scan of knowledge_objects reads these too and costs well apart from
        # the source-driven probes of this notebook's Memory objects (PG16,
        # chosen vs forced-pk plan cost: 2344 vs 3556 with the visibility map
        # blocked, 1394 vs 3556 without; with no such objects the pk plan wins
        # outright when the map is blocked). 28k objects in all stay under
        # ANALYZE's 30k-row sample, so the statistics are exact. Their ids
        # interleave with this notebook's in pk order, as random production ids
        # do, so a merge join along the pk cannot stop after this notebook's
        # range.
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb-other','O','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'other-src-'||g,'nb-other','t','upload',%s,%s "
            "FROM generate_series(0,%s) g",
            (_NOW, _NOW, _OTHER_NOTEBOOK_OBJECTS // 2 - 1),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "created_at,updated_at) SELECT 'ko-'||(8000+g),'nb-other','concept','approved',"
            "'other-src-'||(g/2),%s,%s FROM generate_series(0,%s) g",
            (_NOW, _NOW, _OTHER_NOTEBOOK_OBJECTS - 1),
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


def _plans(postgres_database, bystander_snapshot) -> dict[str, str]:
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
        bystander_snapshot.assert_visibility_map_blocked(connection, *_TABLES)
        for name, (sql, params) in queries.items():
            plans[name] = _plan(connection, sql, params)
    return plans


_SOURCE_LED_OBJECT_SCAN = re.compile(
    r"Index (Only )?Scan using "
    r"(idx_knowledge_objects_source_id|idx_knowledge_objects_source) on knowledge_objects\b"
)


def _scans_with_own_conditions(plan: str, table: str) -> list:
    """Each scan node of ``table`` with the condition lines directly under it
    (indented deeper, before the next node) -- never a neighbouring node's."""
    lines = plan.splitlines()
    scans = []
    for at, line in enumerate(lines):
        if not re.search(rf"\bScan\b.* on {table}\b", line):
            continue
        depth = len(line) - len(line.lstrip())
        own = []
        for below in lines[at + 1:]:
            stripped = below.lstrip()
            if len(below) - len(stripped) <= depth or stripped.startswith("->"):
                break
            own.append(stripped)
        scans.append((line.strip(), own))
    return scans


def test_memory_sql_fragments_keep_their_index_paths(postgres_database, bystander_snapshot):
    plans = _plans(postgres_database, bystander_snapshot)

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
    assert _SOURCE_LED_OBJECT_SCAN.search(cluster), cluster
    object_scans = _scans_with_own_conditions(cluster, "knowledge_objects")
    assert object_scans, cluster
    for node, own in object_scans:
        assert _SOURCE_LED_OBJECT_SCAN.search(node), (node, cluster)
        assert any(
            cond.startswith("Index Cond:") and "source_id" in cond for cond in own
        ), (node, own, cluster)
    assert "idx_clusters_member" in cluster, cluster
