"""M3「挂载仅对挂载人生效」带查看者挂载片段的 PostgreSQL 契约(真 PG)。

行为矩阵与 SQLite 侧同一份数据(`tests/test_mount_sql_contract.py` 的世界、期望值与
判定器),这里只换方言跑一遍——`postgres/mount_sql.py` 的 `%s` 拼装、`CAST(... AS text)`
的查看者行、以及 `COLLATE "C"` 列与查看者值的比较都要在真 PG 上判出同一个集合。

EXPLAIN pin:这些片段在后续波次会进参与集解析、KG 可用性门与 follow 起点子查询,
都是每次提问都跑的热路径。夹具是真实形状的几千本笔记本(规模选在计划器会在两种
join 之间取舍的那一段,理由与实测表见 `PIN_NOTEBOOKS`),断言默认代价下新片段不顺序
扫描任何访问控制表,并保留 `enable_seqscan=off` 的能力判据。
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


# ---------------------------------------------------------------- EXPLAIN pin
#
# 真实形状的夹具:几千本笔记本、每本挂 1–8 个库、成员/授权/组成员按生产量级的比例。
# 全部由 generate_series + hashtext 确定性地生成(同一份输入在任何机器上是同一份数据),
# 用 ON CONFLICT DO NOTHING 吸收伪随机撞上的主键。
#
# 规模为什么是 6000:这条 pin 只在「计划器可能翻成 hash join + 顺序扫描 notebooks」
# 的那一段规模上才有牙齿。实测(本夹具,探针 nbP0,估算代价,NL = 按主键逐条取被挂库,
# hash = 顺序扫描 notebooks 建哈希表):
#
#     规模   旧片段 NL / hash    新片段 NL / hash     查看者支里加回引用 a 的一支 NL / hash
#     3000   (旧片段自己已翻成 hash)                  —
#     5000   259.6 / 261.2       392.3 / 233121       445.7 / 261.2  → 翻
#     6000   209.5 / 310.4       321.1 / 229330       362.0 / 310.4  → 翻
#     8000   213.1 / 409.9       306.8 / 271602       348.6 / 409.9  → 不翻,pin 空转
#
# 6000 本时旧片段离翻转有近五成余量(前置断言稳定),而「查看者支变成 join 条件」
# 这种回退仍会让新片段翻成顺序扫描,正好是要挡的那一类。新片段的 hash 代价高出
# 几个数量级,是因为查看者支只引用 b,被下推到 b 的扫描上逐行求值读权子查询。
PIN_NOTEBOOKS = 6000
PIN_USERS = 1000
PIN_GROUPS = 200


def seed_realistic_mount_fixture(execute, notebooks: int, now) -> None:
    """用 `execute(sql, params)` 把真实形状的访问控制数据灌进已迁移的库。

    探针:u7 的已共享笔记本 nbP0(成员 u500、u501)挂着 u7 自己的五本私有库
    nbP1–nbP5、两本公共库 nb1/nb2、一本 everyone 授权的库 nbPE;u501 另是 nbP1 的
    只读成员。
    """
    base_count = max(notebooks // 100, 2)
    stmts = [
        (
            "INSERT INTO users "
            "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "SELECT 'u'||g, 'u'||g||'@t', 'u'||g, 'u'||g, 'x', 'user', %s, %s "
            "FROM generate_series(1, %s) g",
            (now, now, PIN_USERS),
        ),
        (
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "SELECT 'g'||g, 'g'||g, 'project', '', 'u1', %s, %s "
            "FROM generate_series(1, %s) g",
            (now, now, PIN_GROUPS),
        ),
        (
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "SELECT 'g'||g, 'u'||(1 + abs(hashtext('gm'||g||'-'||k)) %% %s), "
            "CASE WHEN k %% 10 = 0 THEN 'admin' ELSE 'member' END, %s, 'u1' "
            "FROM generate_series(1, %s) g CROSS JOIN generate_series(1, 25) k "
            "ON CONFLICT DO NOTHING",
            (PIN_USERS, now, PIN_GROUPS),
        ),
        (
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "SELECT 'nb'||g, 'nb'||g, '', 'Semiconductor', 'draft', "
            "'u'||(1 + g %% %s), %s, %s, "
            "CASE WHEN g <= %s THEN 'base' ELSE 'personal' END "
            "FROM generate_series(1, %s) g",
            (PIN_USERS, now, now, base_count, notebooks),
        ),
        (
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "SELECT 'nb'||(1 + abs(hashtext('mn'||g)) %% %s), "
            "'u'||(1 + abs(hashtext('mu'||g)) %% %s), 'reader', %s "
            "FROM generate_series(1, 30000) g ON CONFLICT DO NOTHING",
            (notebooks, PIN_USERS, now),
        ),
        (
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "SELECT 'gr'||g, 'nb'||(1 + abs(hashtext('gn'||g)) %% %s), "
            "CASE WHEN g <= 7000 THEN 'user' WHEN g <= 14000 THEN 'group' "
            "WHEN g <= 14900 THEN 'group_admins' ELSE 'everyone' END, "
            "CASE WHEN g <= 7000 THEN 'u'||(1 + abs(hashtext('gu'||g)) %% %s) "
            "WHEN g <= 14900 THEN 'g'||(1 + abs(hashtext('gg'||g)) %% %s) ELSE '' END, "
            "'viewer', 'u1', %s "
            "FROM generate_series(1, 15000) g ON CONFLICT DO NOTHING",
            (notebooks, PIN_USERS, PIN_GROUPS, now),
        ),
        # 每本 1–8 条挂载边:四成挂同 owner 的库(g ± k·用户数),两成挂公共库,
        # 其余随机。
        (
            "INSERT INTO notebook_bases "
            "(notebook_id,base_notebook_id,created_at,created_by) "
            "SELECT 'nb'||a, 'nb'||t, %s, NULL FROM ("
            "SELECT a, CASE "
            "WHEN abs(hashtext('ek'||a||'-'||j)) %% 10 < 4 "
            "THEN 1 + (a - 1 + j * %s) %% %s "
            "WHEN abs(hashtext('ek'||a||'-'||j)) %% 10 < 6 "
            "THEN 1 + abs(hashtext('eb'||a||'-'||j)) %% %s "
            "ELSE 1 + abs(hashtext('er'||a||'-'||j)) %% %s END AS t "
            "FROM generate_series(1, %s) a CROSS JOIN generate_series(1, 8) j "
            "WHERE j <= 1 + abs(hashtext('ec'||a)) %% 8"
            ") edges WHERE t <> a ON CONFLICT DO NOTHING",
            (now, PIN_USERS, notebooks, base_count, notebooks, notebooks),
        ),
        (
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "SELECT id, id, '', 'Semiconductor', 'draft', owner, %s, %s, 'personal' "
            "FROM (VALUES ('nbP0','u7'),('nbP1','u7'),('nbP2','u7'),('nbP3','u7'),"
            "('nbP4','u7'),('nbP5','u7'),('nbPE','u9')) v(id, owner)",
            (now, now),
        ),
        (
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES ('gr-probe-everyone','nbPE','everyone','','viewer','u9',%s)",
            (now,),
        ),
        (
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES ('nbP0','u500','reader',%s),('nbP0','u501','reader',%s),"
            "('nbP1','u501','reader',%s)",
            (now, now, now),
        ),
        (
            "INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
            "SELECT 'nbP0', b, %s, NULL FROM unnest(ARRAY["
            "'nbP1','nbP2','nbP3','nbP4','nbP5','nb1','nb2','nbPE']) b",
            (now,),
        ),
    ]
    for sql, params in stmts:
        execute(sql, params)


_PROBE = "nbP0"
# (标签, 查看者):读不到任何私有挂载的成员、挂载人本人、读得到其中一本的成员、空查看者。
_PIN_VIEWERS = (("member", "u500"), ("mounter", "u7"), ("reader", "u501"), ("empty", None))


@pytest.fixture
def pg_realistic_mounts(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        seed_realistic_mount_fixture(db.execute, PIN_NOTEBOOKS, now)
    # VACUUM 设置可见性图、ANALYZE 给出真实选择度;不能进事务,走独立 autocommit 连接。
    with psycopg.connect(
        postgres_database.settings.database_url, autocommit=True
    ) as raw:
        for table in _ACCESS_TABLES + ("users", "groups"):
            raw.execute(f"VACUUM (ANALYZE) {table}")
    return postgres_database


def _plan(connection, sql: str, params: tuple, *, seqscan: bool = True) -> str:
    if not seqscan:
        connection.execute("SET LOCAL enable_seqscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    if not seqscan:
        connection.execute("RESET enable_seqscan")
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _shapes(module, *, viewer: bool) -> dict[str, str]:
    """后续波次的三种消费形状:过滤+排序(参与集)、EXISTS 门(KG 可用性)、
    IN 子查询(follow 起点)。`viewer=False` 给出同形的旧片段作基线。"""
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


def test_viewer_fragments_keep_the_index_path_at_realistic_scale(pg_realistic_mounts):
    """默认代价下,新片段在旧片段走索引的那个规模上同样不顺序扫描任何访问控制表。

    * **前置**:旧片段的三种形状在这套夹具上都没有 `Seq Scan on notebooks`。它失败
      说明夹具不再落在要钉的那一段规模上(计划器版本或代价参数变了),要先重新定
      规模,而不是放宽下面的断言。
    * **主断言**(绝对判据,不是「⊆ 旧片段」):对每种形状、每类查看者,新片段的
      计划里没有任何 `Seq Scan on <访问控制表>`。
    * **能力**(`enable_seqscan=off`,沿用 `test_cluster_generation_explain_pins.py`
      的 scale-free 判据):关掉顺序扫描后仍出现 `Seq Scan on <表>`,说明那张表根本
      没有可用的索引路径——例如查看者值与 `COLLATE "C"` 列比较时排序规则不一致。
    """
    old_shapes = _shapes(pg_mount_sql, viewer=False)
    new_shapes = _shapes(pg_mount_sql, viewer=True)
    with pg_realistic_mounts.connect() as connection:
        for name, sql in old_shapes.items():
            plan = _plan(connection, sql, (_PROBE,))
            assert "Seq Scan on notebooks" not in plan, (
                f"前置失败:旧片段 {name} 在 {PIN_NOTEBOOKS} 本的夹具上已经顺序扫描 "
                f"notebooks,这条 pin 失去基线:\n{plan}"
            )
        for name, sql in new_shapes.items():
            for label, viewer in _PIN_VIEWERS:
                params = (viewer, _PROBE)
                plan = _plan(connection, sql, params)
                assert not _SEQ_SCAN.search(plan), f"{name} / {label}:\n{plan}"
                capability = _plan(connection, sql, params, seqscan=False)
                assert not _SEQ_SCAN.search(capability), (
                    f"{name} / {label}(enable_seqscan=off):\n{capability}"
                )
