"""M3「挂载仅对挂载人生效」带查看者挂载片段的 PostgreSQL 契约(真 PG)。

行为矩阵与 SQLite 侧同一份数据(`tests/test_mount_sql_contract.py` 的世界、期望值与
判定器),这里只换方言跑一遍——`postgres/mount_sql.py` 的 `%s` 拼装、`CAST(... AS text)`
的查看者行、以及 `COLLATE "C"` 列与查看者值的比较都要在真 PG 上判出同一个集合。

EXPLAIN pin:同一轮整改的任务 E6-2 把参与集解析、KG 可用性门与 follow 起点子查询
切到这些片段,都是每次提问都跑的热路径(此刻还没有调用点)。夹具是真实形状的几千本
笔记本,跑 3000/6000/8000 三档(规模选在计划器会在两种 join 之间取舍的那一段,理由与
实测表见 `PIN_SCALES`),custom 与 generic 两种计划都看:断言发布的片段不顺序扫描任何
访问控制表,保留 `enable_seqscan=off` 的能力判据,并用正对照证明已知的回退写法在同一
夹具上确实会翻成顺序扫描。
"""
from __future__ import annotations

import re

import psycopg
import pytest
from psycopg import sql

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
# 这条 pin 只在「计划器可能翻成 hash join + 顺序扫描 notebooks」的那一段规模上才有
# 牙齿,所以不止跑一档规模,也不只看 custom plan。要挡的回退是「查看者支变成 join
# 条件」:往查看者支里加一支引用挂载方 a 的条件(把 `v.uid = a.created_by` 加回来),
# 或者把整个谓词包进 COALESCE 再当过滤条件用。两者在本夹具上(PG 16,探针 nbP0,每档
# 规模重复 ANALYZE 5 次,结论不变)的表现:
#
#     规模     custom plan                     generic plan(force_generic_plan)
#     3000     三种形状都翻(空查看者除外)      三种形状都翻,含空查看者
#     6000     filtered / in_subquery 翻        filtered / in_subquery 翻
#     8000     都不翻                           filtered / in_subquery 翻
#     20000    都不翻                           都不翻
#
# 发布的片段在这四档、两种计划、四类查看者下都没有任何顺序扫描。20000 档对回退没有
# 鉴别力,所以不跑。custom plan 下空查看者不翻,是因为 `NULL = a.created_by` 被常量
# 折叠掉了;generic plan 看不到参数值,折叠不了。
#
# `PIN_SCALES` 记下每档规模上正对照必须翻的(计划, 形状):这些格子里回退写法**必须**
# 出现 `Seq Scan on notebooks`。计划器或统计口径一变、回退不再翻,正对照先红——pin
# 不会悄悄变成空转。`exists` 形状(KG 可用性门)是 EXISTS 子查询,计划器按首行代价
# 选 nested loop,只有 3000 这一档它才翻,所以它的鉴别力只挂在 3000 上;6000/8000
# 两档它只承担「没有顺序扫描」与 `enable_seqscan=off` 能力判据。
PIN_SCALES: dict[int, dict[str, tuple[str, ...]]] = {
    3000: {
        "custom": ("filtered", "exists", "in_subquery"),
        "generic": ("filtered", "exists", "in_subquery"),
    },
    6000: {
        "custom": ("filtered", "in_subquery"),
        "generic": ("filtered", "in_subquery"),
    },
    8000: {
        "custom": (),
        "generic": ("filtered", "in_subquery"),
    },
}
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
def pg_realistic_mounts(request, postgres_database):
    """`request.param` 本笔记本规模的真实形状夹具,返回 `(database, 规模)`。"""
    notebooks = request.param
    PostgresMigrator(postgres_database).migrate()
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        seed_realistic_mount_fixture(db.execute, notebooks, now)
    # VACUUM 设置可见性图、ANALYZE 给出真实选择度;不能进事务,走独立 autocommit 连接。
    with psycopg.connect(
        postgres_database.settings.database_url, autocommit=True
    ) as raw:
        for table in _ACCESS_TABLES + ("users", "groups"):
            raw.execute(f"VACUUM (ANALYZE) {table}")
    return postgres_database, notebooks


def _plan(
    connection, query: str, params: tuple, *, mode: str = "custom", seqscan: bool = True
) -> str:
    """`mode="custom"`:带参数的 EXPLAIN,计划器看得到参数值(psycopg 默认、前 5 次
    执行的形态)。`mode="generic"`:`PREPARE` + `force_generic_plan`,计划器看不到参数值
    ——psycopg 第 5 次执行起改用服务端预编译语句,PostgreSQL 之后可能改用这种计划。
    两者都在连接池的隐式事务里 `SET LOCAL`(池是 `autocommit=False`),事务结束自动复原。"""
    if not seqscan:
        connection.execute("SET LOCAL enable_seqscan = off")
    if mode == "custom":
        rows = connection.execute(f"EXPLAIN (COSTS OFF) {query}", params).fetchall()
    else:
        assert mode == "generic", mode
        connection.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        counter = iter(range(1, query.count("%s") + 1))
        dollar = re.sub(r"%s", lambda _match: f"${next(counter)}", query)
        types = ", ".join(["text"] * len(params))
        connection.execute(f"PREPARE mount_pin({types}) AS {dollar}")
        # EXECUTE 是 utility 语句,不能走扩展协议传参:实参用 sql.Literal 内联(只是
        # 喂值;generic plan 的判据在 PREPARE 的 $n 参数位上)。
        literals = ", ".join(sql.Literal(value).as_string(None) for value in params)
        rows = connection.execute(
            f"EXPLAIN (COSTS OFF) EXECUTE mount_pin({literals})"
        ).fetchall()
        connection.execute("DEALLOCATE mount_pin")
        connection.execute("RESET plan_cache_mode")
    if not seqscan:
        connection.execute("RESET enable_seqscan")
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _shapes(join: str, effective: str) -> dict[str, str]:
    """E6-2 的三种消费形状:过滤+排序(参与集)、EXISTS 门(KG 可用性)、IN 子查询
    (follow 起点)。`effective` 是追加在 `join` 之后的有效性过滤后缀。"""
    return {
        "filtered": "SELECT b.id " + join + effective + pg_mount_sql.MOUNT_ORDER,
        "exists": "SELECT EXISTS(SELECT 1 " + join + effective + ")",
        "in_subquery": (
            "SELECT n.id FROM notebooks n WHERE n.id IN (SELECT b.id "
            + join + effective + ")"
        ),
    }


def _shipped_shapes() -> dict[str, str]:
    shapes = _shapes(
        pg_mount_sql.MOUNT_VIEWER_JOIN, pg_mount_sql.MOUNT_EFFECTIVE_FOR_VIEWER
    )
    # IN 子查询形状用发布的子查询常量本身,而不是就地重拼。
    shapes["in_subquery"] = (
        "SELECT n.id FROM notebooks n WHERE n.id IN ("
        + pg_mount_sql.MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY + ")"
    )
    return shapes


def _regressed_shapes() -> dict[str, dict[str, str]]:
    """已知的坏写法(正对照):都让查看者支变成 join 条件。"""
    valid = pg_mount_sql.MOUNT_VALID_EXPR
    arm = pg_mount_sql._VIEWER_REACHES_MOUNT_EXPR
    assert arm.endswith(")")
    mounter_arm = arm[:-1] + " OR v.uid = a.created_by)"
    return {
        # 把挂载人支加回查看者支(E6-1 第一版的写法)。
        "mounter_arm": _shapes(
            pg_mount_sql.MOUNT_VIEWER_JOIN,
            " AND (" + valid + " AND " + mounter_arm + ")",
        ),
        # 拿 COALESCE 包住的整个谓词(即公开布尔列 `MOUNT_EFFECTIVE_FOR_VIEWER_EXPR`
        # 的写法)当正向过滤条件。就地拼出,不引用那个常量:正对照必须是固定的坏写法,
        # 不能随被测常量一起变。
        "coalesce_filter": _shapes(
            pg_mount_sql.MOUNT_VIEWER_JOIN,
            " AND COALESCE(" + pg_mount_sql._MOUNT_EFFECTIVE_FOR_VIEWER_PRED + ", FALSE)",
        ),
    }


_SEQ_SCAN = re.compile(r"Seq Scan on (" + "|".join(_ACCESS_TABLES) + r")\b")


@pytest.mark.parametrize("pg_realistic_mounts", sorted(PIN_SCALES), indirect=True)
def test_viewer_fragments_keep_the_index_path_at_realistic_scale(pg_realistic_mounts):
    """发布的片段不顺序扫描任何访问控制表;已知的回退写法在同一夹具上必须顺序扫描。

    * **主断言**(绝对判据):每档规模、每种形状、每类查看者(含空查看者)、custom 与
      generic 两种计划,发布片段的计划里都没有 `Seq Scan on <访问控制表>`。
    * **能力**(`enable_seqscan=off`,沿用 `test_cluster_generation_explain_pins.py`
      的 scale-free 判据):关掉顺序扫描后仍出现 `Seq Scan on <表>`,说明那张表根本
      没有可用的索引路径——例如查看者值与 `COLLATE "C"` 列比较时排序规则不一致。
    * **正对照**:`PIN_SCALES` 列出的每个 (计划, 形状) 格子里,两种已知回退写法都
      必须出现 `Seq Scan on notebooks`(custom plan 跳过空查看者,理由见 `PIN_SCALES`
      上方的注释)。它红了说明夹具不再落在回退会翻转的那段规模上,主断言已经不能
      区分好坏——要重新定规模,而不是删掉正对照。它取代了旧版「旧片段不顺序扫描」
      那条前置断言:旧片段在 3000 档自己就顺序扫描,而正对照直接证明这档有鉴别力。
    """
    database, notebooks = pg_realistic_mounts
    shipped = _shipped_shapes()
    regressed = _regressed_shapes()
    with database.connect() as connection:
        for name, query in shipped.items():
            for label, viewer in _PIN_VIEWERS:
                params = (viewer, _PROBE)
                for mode in ("custom", "generic"):
                    plan = _plan(connection, query, params, mode=mode)
                    assert not _SEQ_SCAN.search(plan), (
                        f"{notebooks} 本 / {mode} / {name} / {label}:\n{plan}"
                    )
                capability = _plan(connection, query, params, seqscan=False)
                assert not _SEQ_SCAN.search(capability), (
                    f"{notebooks} 本 / {name} / {label}(enable_seqscan=off):\n{capability}"
                )
        for mode, shape_names in PIN_SCALES[notebooks].items():
            viewers = [
                (label, viewer)
                for label, viewer in _PIN_VIEWERS
                if mode == "generic" or viewer is not None
            ]
            for variant, shapes in regressed.items():
                for name in shape_names:
                    for label, viewer in viewers:
                        plan = _plan(
                            connection, shapes[name], (viewer, _PROBE), mode=mode
                        )
                        assert "Seq Scan on notebooks" in plan, (
                            f"正对照失效:{notebooks} 本 / {mode} / {variant} / {name} / "
                            f"{label} 不再顺序扫描 notebooks,这条 pin 在这档规模上已经"
                            f"失去鉴别力,重新定规模:\n{plan}"
                        )


# ------------------------------------------- E6-2:真实调用点发出的语句
#
# 上面钉的是片段拼出来的三种形状;这里钉**七个 store 调用点真正发出的语句**。语句
# 是录下来的(包一层只记录的连接代理),不是在测试里重拼——调用点换回旧片段、或拼
# 错参数顺序,录到的语句就不是发布的形状,或者计划里出现顺序扫描。


class _Recorder:
    """只记录 `execute(sql, params)` 再原样转发的连接代理。"""

    def __init__(self, connection):
        self._connection = connection
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, query, params=None, *args, **kwargs):
        self.calls.append((str(query), tuple(params or ())))
        return self._connection.execute(query, params, *args, **kwargs)


def _recorded_call_site_statements(database, viewer):
    """调用七个调用点各一次,返回 `{站点: [(sql, params), ...]}`(只留挂载语句)。"""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.domain.knowledge_contracts import USABLE_STATUSES
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.notebook_store import NotebookStore
    from app.repositories.postgres.query_store import QueryStore
    from app.repositories.postgres.unified_kg_store import UnifiedKgStore

    out: dict[str, list] = {}
    with database.connect() as connection:

        def record(site, call):
            recorder = _Recorder(connection)
            call(recorder)
            out[site] = [
                (sql, params) for sql, params in recorder.calls
                if "FROM notebook_bases" in sql
            ]

        kw = {"viewer_id": viewer}
        record("resolve_participants",
               lambda db: NotebookStore.resolve_participants(db, _PROBE, **kw))
        record("participant_rows",
               lambda db: NotebookStore.participant_rows(db, _PROBE, **kw))
        record("notebook_has_usable_base_kg",
               lambda db: QueryStore.notebook_has_usable_base_kg(db, _PROBE, **kw))
        record("mounted_bases_row",
               lambda db: QueryStore.mounted_bases_row(db, _PROBE, **kw))
        record("any_mounted_has_kg_on",
               lambda db: KnowledgeStore.any_mounted_has_kg_on(db, _PROBE, **kw))
        record("follow_start_row",
               lambda db: KnowledgeStore.follow_start_row(
                   db, "ko-none", _PROBE, USABLE_STATUSES, **kw))

        def mounted_base_ids(recorder):
            @contextmanager
            def connect():
                yield recorder

            unified = UnifiedKgStore.__new__(UnifiedKgStore)
            unified.database = SimpleNamespace(connect=connect)
            unified.mounted_base_ids(_PROBE, **kw)

        record("mounted_base_ids", mounted_base_ids)
    return out


@pytest.mark.parametrize("pg_realistic_mounts", [6000], indirect=True)
def test_call_site_statements_are_the_published_viewer_fragments(pg_realistic_mounts):
    """七个调用点各录到一条挂载语句:它含发布的带查看者片段、参数按
    `(viewer, notebook)` 绑定,并且在真实形状夹具上(custom / generic 两种计划,
    含 `enable_seqscan=off` 的能力判据)不顺序扫描任何访问控制表。"""
    database, _notebooks = pg_realistic_mounts
    with database.connect() as connection:
        for label, viewer in _PIN_VIEWERS:
            recorded = _recorded_call_site_statements(database, viewer)
            assert sorted(recorded) == sorted([
                "resolve_participants", "participant_rows",
                "notebook_has_usable_base_kg", "mounted_bases_row",
                "any_mounted_has_kg_on", "follow_start_row", "mounted_base_ids",
            ])
            for site, statements in recorded.items():
                assert len(statements) == 1, (site, statements)
                query, params = statements[0]
                assert pg_mount_sql.MOUNT_VIEWER_JOIN in query, (site, query)
                assert pg_mount_sql._MOUNT_EFFECTIVE_FOR_VIEWER_PRED in query, (site, query)
                # 查看者紧挨在它所属笔记本之前(查看者行先于 WHERE 出现)。
                at = [i for i, value in enumerate(params) if value == viewer]
                assert len(at) == 1 and params[at[0] + 1] == _PROBE, (site, params)
                modes = ("custom",) if any(isinstance(p, list) for p in params) else (
                    "custom", "generic"
                )
                for mode in modes:
                    plan = _plan(connection, query, params, mode=mode)
                    assert not _SEQ_SCAN.search(plan), f"{site} / {label} / {mode}:\n{plan}"
                capability = _plan(connection, query, params, seqscan=False)
                assert not _SEQ_SCAN.search(capability), (
                    f"{site} / {label}(enable_seqscan=off):\n{capability}"
                )
