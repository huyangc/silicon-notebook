"""M3「挂载仅对挂载人生效」带查看者挂载片段的 PostgreSQL 契约(真 PG)。

行为矩阵与 SQLite 侧同一份数据(`tests/test_mount_sql_contract.py` 的世界、期望值与
判定器),这里只换方言跑一遍——`postgres/mount_sql.py` 的 `%s` 拼装、`CAST(... AS text)`
的查看者行、以及 `COLLATE "C"` 列与查看者值的比较都要在真 PG 上判出同一个集合。

EXPLAIN pin:这些片段在后续波次会进参与集解析、KG 可用性门与 follow 起点子查询,
都是每次提问都跑的热路径。钉住在带填充数据的世界上 notebooks / notebook_bases /
notebook_members / notebook_grants / group_members 一律走索引,没有顺序扫描——查看者
支里的读权子查询若写成拿不到索引的形状(例如查看者值与 `COLLATE "C"` 列比较时
排序规则不一致),这里会红。
"""
from __future__ import annotations

import re

import psycopg
import pytest

from app.repositories.postgres import mount_sql as pg_mount_sql
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from tests.test_mount_sql_contract import (
    mount_viewer_contract_failures,
    seed_mount_world,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mount_sql_viewer"),
]

_ACCESS_TABLES = (
    "notebooks",
    "notebook_bases",
    "notebook_members",
    "notebook_grants",
    "group_members",
)


@pytest.fixture
def pg_mount_world(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        seed_mount_world(db.execute, "%s", now)
    return postgres_database


def _pg_query(database):
    def query(sql, params, *, all_columns=False):
        with database.connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        if all_columns:
            return [tuple(row.values()) for row in rows]
        return [next(iter(row.values())) for row in rows]

    return query


def test_postgres_mount_viewer_matrix(pg_mount_world):
    """M3 行为矩阵(PostgreSQL,真实表),与 SQLite 侧逐格同一份期望。"""
    failures = mount_viewer_contract_failures(_pg_query(pg_mount_world), pg_mount_sql)
    assert failures == []


def test_postgres_viewer_fragments_refuse_a_single_parameter(pg_mount_world):
    """漏改的调用点照旧只绑一个 notebook_id:当场报错,而不是静默错绑。"""
    for sql in (
        "SELECT b.id " + pg_mount_sql.MOUNT_VIEWER_JOIN
        + pg_mount_sql.MOUNT_EFFECTIVE_FOR_VIEWER,
        "SELECT 1 WHERE 'nb-b' IN ("
        + pg_mount_sql.MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY + ")",
    ):
        with pg_mount_world.connect() as connection:
            with pytest.raises(psycopg.ProgrammingError):
                connection.execute(sql, ("nb-a",)).fetchall()


def _seed_filler(database, rows: int) -> None:
    """给访问控制表灌上与世界无关的填充行,让计划器有真实的选择度可比。"""
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    users = rows // 4
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "SELECT 'u-f'||g, 'u-f'||g||'@t', 'f', 'u-f'||g, 'x', 'user', %s, %s "
            "FROM generate_series(1, %s) g",
            (now, now, users),
        )
        db.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "SELECT 'g-f'||g, 'g-f'||g, 'project', '', 'u-f1', %s, %s "
            "FROM generate_series(1, %s) g",
            (now, now, users),
        )
        db.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "SELECT 'g-f'||g, 'u-f'||g, 'member', %s, 'u-f1' "
            "FROM generate_series(1, %s) g",
            (now, users),
        )
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "SELECT 'nb-f'||g, 'f'||g, '', 'Semiconductor', 'draft', "
            "'u-f'||(1 + g %% %s), %s, %s, 'personal' "
            "FROM generate_series(1, %s) g",
            (users, now, now, rows),
        )
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "SELECT 'nb-f'||g, 'u-f'||(1 + (g + 1) %% %s), 'reader', %s "
            "FROM generate_series(1, %s) g",
            (users, now, rows),
        )
        db.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "SELECT 'gr-f'||g, 'nb-f'||g, 'group', 'g-f'||(1 + g %% %s), 'viewer', "
            "'u-f1', %s FROM generate_series(1, %s) g",
            (users, now, rows),
        )
        db.execute(
            "INSERT INTO notebook_bases "
            "(notebook_id,base_notebook_id,created_at,created_by) "
            "SELECT 'nb-f'||g, 'nb-f'||(1 + g %% %s), %s, 'u-f1' "
            "FROM generate_series(1, %s) g WHERE 1 + g %% %s <> g",
            (rows, now, rows, rows),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        for table in _ACCESS_TABLES + ("users", "groups"):
            raw.execute(f"VACUUM (ANALYZE) {table}")


def _plan(connection, sql: str, params: tuple, *, seqscan: bool) -> str:
    if not seqscan:
        connection.execute("SET LOCAL enable_seqscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    if not seqscan:
        connection.execute("RESET enable_seqscan")
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _shapes(module, *, viewer: bool) -> dict[str, str]:
    """后续波次的三种消费形状:过滤+排序(参与集)、EXISTS 门(KG 可用性)、
    IN 子查询(follow 起点)。`viewer=False` 给出同形的旧片段作对照基线。"""
    join = module.MOUNT_VIEWER_JOIN if viewer else module.MOUNT_JOIN
    valid = module.MOUNT_EFFECTIVE_FOR_VIEWER if viewer else module.MOUNT_VALID
    ids = (
        module.MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY
        if viewer
        else module.MOUNTED_BASE_IDS_SUBQUERY
    )
    return {
        "filtered": "SELECT b.id " + join + valid + module.MOUNT_ORDER,
        "exists": "SELECT EXISTS(SELECT 1 " + join + valid + ")",
        "in_subquery": "SELECT n.id FROM notebooks n WHERE n.id IN (" + ids + ")",
    }


_SEQ_SCAN = re.compile(r"Seq Scan on (" + "|".join(_ACCESS_TABLES) + r")\b")


def test_viewer_fragments_never_seq_scan_access_tables(pg_mount_world):
    """三种消费形状对访问控制表一律有索引路径,且不比同形的旧片段多出顺序扫描。

    查看者取已共享库里读不到私有挂载的成员 u-plain:读权支的每一臂(owner 比较、
    成员 EXISTS、授权边 EXISTS 及其组成员 EXISTS)都要被求值,不会被前面的 OR
    短路掉。

    两条判据:

    * **能力**(`enable_seqscan=off`,沿用 `test_cluster_generation_explain_pins.py`
      的 scale-free 判据):关掉顺序扫描后计划里仍出现 `Seq Scan on <表>`,说明那张表
      根本没有可用的索引路径——例如查看者值与 `COLLATE "C"` 列比较时排序规则不一致。
    * **不回退**(默认代价):几千行的测试台上计划器会合理地对 `notebooks b` 选
      hash join + 顺序扫描(旧片段同样如此,生产表规模下不成立);这里只钉新片段
      顺序扫描的表集合 ⊆ 同形旧片段的集合——查看者支不许引入新的顺序扫描。
    """
    _seed_filler(pg_mount_world, 4000)
    new_shapes = _shapes(pg_mount_sql, viewer=True)
    old_shapes = _shapes(pg_mount_sql, viewer=False)
    with pg_mount_world.connect() as connection:
        for name, sql in new_shapes.items():
            plan = _plan(connection, sql, ("u-plain", "nb-a"), seqscan=False)
            assert not _SEQ_SCAN.search(plan), f"{name}:\n{plan}"
            new_seq = set(
                _SEQ_SCAN.findall(
                    _plan(connection, sql, ("u-plain", "nb-a"), seqscan=True)
                )
            )
            old_seq = set(
                _SEQ_SCAN.findall(
                    _plan(connection, old_shapes[name], ("nb-a",), seqscan=True)
                )
            )
            assert new_seq <= old_seq, f"{name}: {sorted(new_seq - old_seq)}"
