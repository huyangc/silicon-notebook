# backend/tests/test_mount_sql_contract.py
"""挂载有效性谓词(`MOUNT_VALID_EXPR`)唯一定义点的结构性守卫。

`sqlite/mount_sql.py` 与 `postgres/mount_sql.py` 是这条谓词的一对唯一定义点
(互为镜像,双后端同修——见两份文件的模块 docstring)。参与集解析、KG 可用性门、
summary 投影、社区扩展、晋升目标五处消费点一律 import 这两份文件里的常量,不许
另抄一份。这份契约钉的是 A8:「公共库 ∨ 同 owner」这条最容易被顺手复刻的分支
(`tier='base' OR created_by=created_by`)只应出现在这两个定义点,出现在第三处就是
一条静默分叉的挂载判定——今天语义相同,某天这里扩了一个 OR 分支,复刻处就漏掉。

判据形状从 `MOUNT_VALID_EXPR` 运行时取值里按两个锚点(`tier=base` 与
`created_by=a.created_by`)提取:锚点**之间**的文本自动跟随谓词变化;锚点本身
(列名、别名)改动时提取失败并响亮报错,需要人工重估这条契约。守卫只覆盖**逐字
复刻**(同别名、同 OR 顺序);换别名或交换 OR 两边的语义复刻不在覆盖面内,与
`test_access_sql_contract.py` 同一类文本守卫的既有边界一致。

后半部分是 M3「挂载仅对挂载人生效」带查看者片段(`*_FOR_VIEWER` / `MOUNT_VIEWER_JOIN`)
的契约:每个片段的位置参数个数固定(旧片段一个不变,新片段两个且顺序
`(viewer, notebook)`),以及一份在真实表上跑的行为矩阵。矩阵的世界与期望值是后端
无关的数据,SQLite 在这里跑,PostgreSQL 在 `tests/postgres/test_mount_sql_viewer_pg.py`
里 import 同一份数据跑(外加 EXPLAIN pin)。
"""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import pytest

from app.core.config import Settings
from app.repositories.postgres import access_sql as pg_access_sql
from app.repositories.postgres import mount_sql as pg_mount_sql
from app.repositories.sqlite import access_sql as sqlite_access_sql
from app.repositories.sqlite import mount_sql as sqlite_mount_sql
from app.repositories.sqlite.mount_sql import MOUNT_VALID_EXPR as SQLITE_MOUNT_VALID_EXPR
from app.repositories.postgres.mount_sql import (
    MOUNT_VALID_EXPR as PG_MOUNT_VALID_EXPR,
)
from app.services.sqlite_repository import SQLiteRepository

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"

# 两个唯一定义点自身(互为镜像),按相对路径豁免——按文件名豁免会让将来任何
# 同名模块(比如 services/kg/mount_sql.py)里的复刻被静默放行。
_DEFINITION_POINTS = {
    "repositories/sqlite/mount_sql.py",
    "repositories/postgres/mount_sql.py",
}

_SHAPE_PATTERN = re.compile(r"tier=base.*?created_by=a\.created_by")


def _collapsed(text: str) -> str:
    """去掉引号与全部空白:让跨行字符串拼接与源码换行都现出连续的 SQL 形状。"""
    return re.sub(r"[\s'\"]+", "", text)


def _mount_valid_shape(expr: str, backend: str) -> str:
    """从某一侧 `MOUNT_VALID_EXPR` 的运行时取值里提取「公共库 ∨ 同 owner」分支的形状。"""
    match = _SHAPE_PATTERN.search(_collapsed(expr))
    assert match is not None, (
        f"{backend} MOUNT_VALID_EXPR 里找不到「tier='base' OR created_by=created_by」"
        "这支——谓词形状变了,先确认这条契约测试是否还有意义,再更新判据"
    )
    return match.group(0)


def test_sqlite_and_postgres_mount_valid_expr_share_the_tier_or_owner_shape():
    """两份镜像在「公共库 ∨ 同 owner」这支上必须逐字相同(双后端同修的可执行证据)。

    两侧各自独立提取再比对:任一侧单独改形状都会红,而不是只有 PG 侧会红。
    """
    sqlite_shape = _mount_valid_shape(SQLITE_MOUNT_VALID_EXPR, "sqlite")
    pg_shape = _mount_valid_shape(PG_MOUNT_VALID_EXPR, "postgres")
    assert sqlite_shape == pg_shape


def test_mount_valid_tier_or_owner_shape_lives_only_in_mount_sql():
    """「公共库 ∨ 同 owner」的挂载有效性形状只许出现在两份 mount_sql.py。

    唯一定义点的价值在于没有第三份复刻:手写一份逐字相同的判定,今天语义相同,
    未来 MOUNT_VALID_EXPR 扩展第五支可挂范围那天就是一条静默分叉的挂载判定。这条
    移动变异守卫保证「把定义搬回消费点」会报红,而不是靠 docstring 清单的自觉。
    """
    pattern = re.compile(re.escape(_mount_valid_shape(SQLITE_MOUNT_VALID_EXPR, "sqlite")))
    offenders = []
    seen_definition_points = set()
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        rel = str(path.relative_to(_BACKEND_APP))
        if rel in _DEFINITION_POINTS:
            seen_definition_points.add(rel)
            continue
        if pattern.search(_collapsed(path.read_text(encoding="utf-8"))):
            offenders.append(rel)
    # 定义点改名/搬家时这条守卫会退化成永真,先钉住两份文件都被扫到过。
    assert seen_definition_points == _DEFINITION_POINTS, (
        f"唯一定义点未被扫到:{sorted(_DEFINITION_POINTS - seen_definition_points)},"
        "mount_sql.py 搬家后请同步 _DEFINITION_POINTS"
    )
    assert offenders == [], (
        f"挂载有效性谓词的「公共库 ∨ 同 owner」形状出现在唯一定义点之外:{offenders}。"
        "请改用 mount_sql.MOUNT_VALID_EXPR / MOUNT_VALID,不要手写内联复刻。"
        "(扫描的是源码全文:注释或 docstring 里逐字引用这条谓词同样会命中,"
        "请改为引用常量名。)"
    )


# ====================================================================== M3
# 带查看者的挂载片段(挂载仅对挂载人生效)。

# 片段 -> 它消费几个位置参数。旧片段逐个钉住「不变」,新片段钉住「恰好两个」。
# 给片段加/减一个占位符就是改了所有调用点的绑定契约,必须先来这里改期望。
OLD_FRAGMENT_PARAM_COUNTS = {
    "MOUNT_JOIN": 1,
    "MOUNTED_BASE_IDS_SUBQUERY": 1,
    "MOUNT_VALID_EXPR": 0,
    "MOUNT_VALID": 0,
    "MOUNT_GATE_CLOSED_EXPR": 0,
    "MOUNT_ORIGIN_COLUMN": 0,
    "MOUNT_ORDER": 0,
}
VIEWER_FRAGMENT_PARAM_COUNTS = {
    "MOUNT_VIEWER_JOIN": 2,
    "MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY": 2,
    "MOUNT_EFFECTIVE_FOR_VIEWER_EXPR": 0,
    "MOUNT_EFFECTIVE_FOR_VIEWER": 0,
}
_BACKENDS = (
    ("sqlite", sqlite_mount_sql, sqlite_access_sql, "?"),
    ("postgres", pg_mount_sql, pg_access_sql, "%s"),
)


def test_every_mount_fragment_consumes_a_fixed_number_of_parameters():
    """旧片段的参数个数一个都没变;新片段恰好两个(或零个,纯列引用)。"""
    for backend, module, _access, placeholder in _BACKENDS:
        for name, expected in {
            **OLD_FRAGMENT_PARAM_COUNTS,
            **VIEWER_FRAGMENT_PARAM_COUNTS,
        }.items():
            actual = getattr(module, name).count(placeholder)
            assert actual == expected, f"{backend}.{name}: {actual} != {expected}"


def test_viewer_join_binds_viewer_before_notebook():
    """参数顺序 `(viewer, notebook)`:查看者占位符在 FROM 子句里,先于 WHERE 出现。

    文本侧只钉「查看者行里恰好一个占位符、`e.notebook_id` 恰好比较第二个」;行为侧的
    错序绑定探针在矩阵测试里(错序时返回空,而不是别的库)。
    """
    for backend, module, _access, placeholder in _BACKENDS:
        join = module.MOUNT_VIEWER_JOIN
        head, sep, tail = join.partition(" AS uid) v ")
        assert sep, f"{backend}: 查看者行 `(... AS uid) v` 不见了"
        assert head.count(placeholder) == 1, backend
        assert tail.count(placeholder) == 1, backend
        assert f"e.notebook_id = {placeholder}" in tail, backend
        # 空查看者失败即关:空串与 None 都归一成 NULL。
        assert "NULLIF(" in head, backend
        subquery = module.MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY
        assert subquery.startswith("SELECT b.id " + join), backend


def test_viewer_predicate_is_derived_from_the_existing_definitions():
    """带查看者的谓词 = 既有有效性原样 ∧ 查看者支;读权复用 access_sql,不另抄。

    `MOUNT_VALID_EXPR` 原样嵌在里面,「借来的不转借」、copying 哨兵与四支可挂范围就
    对所有查看者照旧成立;查看者支逐字等于「公共库 ∨ `read_access_clause` 的列引用形式」。
    两个后端的查看者支逐字相同(纯列引用,没有方言差异)——双后端同修的可执行证据。

    查看者支**不许引用挂载方 `a`**:只引用 `b` 与查看者常量时,它作为 `b` 上的限制
    条件下推到 `b` 的扫描上,计划器稳定按主键取被挂库;一旦 OR 里出现 `a.` 的列(比如
    把「查看者就是挂载人」`v.uid = a.created_by` 作为短路加回来),它就变成 join 条件,
    几千本笔记本的规模上计划器翻成顺序扫描 notebooks(EXPLAIN pin 在
    `tests/postgres/test_mount_sql_viewer_pg.py`)。挂载人照样生效,靠的是与
    `MOUNT_VALID_EXPR` 的蕴含,由行为矩阵里「挂载人的有序结果等于旧片段」守着。
    """
    arms = []
    for backend, module, access, _placeholder in _BACKENDS:
        expr = module.MOUNT_EFFECTIVE_FOR_VIEWER_EXPR
        assert expr.startswith("(" + module.MOUNT_VALID_EXPR + " AND "), backend
        assert module.MOUNT_EFFECTIVE_FOR_VIEWER == " AND " + expr, backend
        read_arm = access.read_access_clause(
            "b",
            "vm",
            user_ref="v.uid",
            grant_alias="vg",
            group_alias="vgm",
            group_admin_alias="vga",
        )
        arm = module._VIEWER_REACHES_MOUNT_EXPR
        assert arm == "(b.tier = 'base' OR " + read_arm + ")", backend
        assert re.search(r"(?<![\w.])a\.", arm) is None, backend
        arms.append(module._VIEWER_REACHES_MOUNT_EXPR)
    assert arms[0] == arms[1]


# ---------------------------------------------------------------- 行为矩阵
#
# 世界(与后端无关的数据):
#
# * nb-a:u-owner 的**已共享**库(四个只读成员)。挂着:
#   nb-b(u-owner 自己的私有库)、nb-p(公共库)、nb-e(everyone 授权)、
#   nb-y(u-owner 从 u-carol 借来的——nb-a 已共享,未共享门关着)、
#   nb-k(u-owner 自己的库,正在深拷贝 status='copying')。
# * nb-b 的读者:u-bmember(只读成员)、u-bgrantee(点名授权边)、u-bgroup(组授权)。
#   u-plain 是 nb-a 的成员但读不了 nb-b——M3 要挡的正是他。
# * nb-c:u-owner 的**未共享**库,挂着借来的 nb-x(u-xowner 的库,u-owner 是只读成员)
#   与 nb-b。借入边对挂载人生效,对被借库的主人 u-xowner 也生效(他本来就能读 nb-x)。
# * u-all 同时是 nb-b 与 nb-x 的只读成员:他读得到每一本私有挂载,于是在每本库上看到
#   的都与旧片段相同——和挂载人一起承担「有序结果逐行等于旧片段」那条断言。
# * nb-n:挂载人未知(created_by 为 NULL),挂着 nb-p、nb-e、nb-b。
# * 空 id 用户 `""` 是 nb-b 的只读成员:外键今天只在 `users` 里真有 `id=''` 时才放得
#   进这种行,这里手插它,钉住「空查看者不等于任何人」的归一(NULLIF),不让谓词
#   寄生在「没人用空 id」这个外部事实上。
MOUNT_WORLD_USERS = (
    "u-owner",
    "u-plain",
    "u-bmember",
    "u-bgrantee",
    "u-bgroup",
    "u-carol",
    "u-xowner",
    "u-stranger",
    "u-all",
    "",
)
# (id, created_by, tier, status)
MOUNT_WORLD_NOTEBOOKS = (
    ("nb-a", "u-owner", "personal", "draft"),
    ("nb-b", "u-owner", "personal", "draft"),
    ("nb-p", "u-carol", "base", "draft"),
    ("nb-e", "u-carol", "personal", "draft"),
    ("nb-y", "u-carol", "personal", "draft"),
    ("nb-k", "u-owner", "personal", "copying"),
    ("nb-c", "u-owner", "personal", "draft"),
    ("nb-x", "u-xowner", "personal", "draft"),
    ("nb-n", None, "personal", "draft"),
)
MOUNT_WORLD_MEMBERS = (
    ("nb-a", "u-plain"),
    ("nb-a", "u-bmember"),
    ("nb-a", "u-bgrantee"),
    ("nb-a", "u-bgroup"),
    ("nb-b", "u-bmember"),
    ("nb-b", ""),
    ("nb-y", "u-owner"),
    ("nb-x", "u-owner"),
    ("nb-b", "u-all"),
    ("nb-x", "u-all"),
)
# (id, notebook_id, principal_type, principal_id)
MOUNT_WORLD_GRANTS = (
    ("gr-b-user", "nb-b", "user", "u-bgrantee"),
    ("gr-b-group", "nb-b", "group", "g-b"),
    ("gr-e-everyone", "nb-e", "everyone", ""),
)
# (notebook_id, base_notebook_id, created_by)
MOUNT_WORLD_BASES = (
    ("nb-a", "nb-b", "u-owner"),
    ("nb-a", "nb-p", "u-owner"),
    ("nb-a", "nb-e", "u-owner"),
    ("nb-a", "nb-y", "u-owner"),
    ("nb-a", "nb-k", "u-owner"),
    ("nb-c", "nb-x", "u-owner"),
    ("nb-c", "nb-b", "u-owner"),
    ("nb-n", "nb-p", None),
    ("nb-n", "nb-e", None),
    ("nb-n", "nb-b", None),
)

# None = 空查看者的另一种形态(后台路径拿不到 actor 时传进来的可能是 None)。
MOUNT_WORLD_VIEWERS = MOUNT_WORLD_USERS + (None,)
_READERS_OF_B = frozenset({"u-owner", "u-bmember", "u-bgrantee", "u-bgroup", "u-all"})
_READERS_OF_X = frozenset({"u-owner", "u-xowner", "u-all"})
_PUBLIC = frozenset({"nb-p", "nb-e"})

# 旧(与查看者无关)片段今天按 MOUNT_ORDER 判出的有效列表——本任务不许改变它们。
# 公共库在前,其余按名字。
OLD_MOUNT_ORDERED = {
    "nb-a": ("nb-p", "nb-b", "nb-e"),
    "nb-c": ("nb-b", "nb-x"),
    "nb-n": ("nb-p", "nb-e"),
}
OLD_MOUNT_EFFECTIVE = {nb: frozenset(ids) for nb, ids in OLD_MOUNT_ORDERED.items()}
MOUNTER = {"nb-a": "u-owner", "nb-c": "u-owner"}
# 这些查看者在对应库上的结果必须与旧片段**逐行同序**(不只是同一个集合):挂载人,
# 以及读得到全部私有挂载的 u-all。
ORDER_PINNED_VIEWERS = {
    "nb-a": ("u-owner", "u-all"),
    "nb-c": ("u-owner", "u-all"),
    "nb-n": ("u-all",),
}


def expected_mounts_for_viewer(notebook_id: str, viewer: str | None) -> frozenset:
    """矩阵期望值。每一格的理由写在世界说明里,这里只有判定。"""
    if notebook_id == "nb-a":
        # 公共库与 everyone 对所有人(含空查看者);私有 nb-b 只对挂载人与它的读者;
        # nb-y(借入、未共享门关)与 nb-k(copying)对谁都不生效,挂载人也不例外。
        return _PUBLIC | ({"nb-b"} if viewer in _READERS_OF_B else set())
    if notebook_id == "nb-c":
        result = set()
        if viewer in _READERS_OF_X:
            result.add("nb-x")
        if viewer in _READERS_OF_B:
            result.add("nb-b")
        return frozenset(result)
    if notebook_id == "nb-n":
        # 挂载人未知:nb-b 连既有有效性都过不了(同 owner 支比的是 NULL),对它的
        # 主人 u-owner 也不生效;公共库与 everyone 对所有人。
        return _PUBLIC
    raise AssertionError(notebook_id)


# 查看者支单独在挂载人未知(a = nb-n)下的判定:只认「查看者自己能读 b」。
def expected_reach_with_unknown_mounter(base_id: str, viewer: str | None) -> bool:
    if base_id in _PUBLIC:
        return True
    if base_id == "nb-b":
        return viewer in _READERS_OF_B
    if base_id == "nb-x":
        return viewer in _READERS_OF_X
    raise AssertionError(base_id)


def seed_mount_world(
    execute: Callable[[str, Sequence[object]], object],
    placeholder: str,
    now: object,
) -> None:
    """把上面的世界落进真实表(两后端同一组语句,只换占位符)。"""

    def run(sql: str, params: Sequence[object]) -> None:
        execute(sql.replace("?", placeholder), params)

    for uid in MOUNT_WORLD_USERS:
        handle = uid or "u-blank"
        run(
            "INSERT INTO users "
            "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, f"{handle}@t", handle, handle, "x", "user", now, now),
        )
    run(
        "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("g-b", "g-b", "project", "", "u-owner", now, now),
    )
    run(
        "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
        "VALUES (?,?,?,?,?)",
        ("g-b", "u-bgroup", "member", now, "u-owner"),
    )
    for notebook_id, owner, tier, status in MOUNT_WORLD_NOTEBOOKS:
        run(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (notebook_id, notebook_id, "", "Semiconductor", status, owner, now, now, tier),
        )
    for notebook_id, user_id in MOUNT_WORLD_MEMBERS:
        run(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,?,?)",
            (notebook_id, user_id, "reader", now),
        )
    for grant_id, notebook_id, principal_type, principal_id in MOUNT_WORLD_GRANTS:
        run(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (grant_id, notebook_id, principal_type, principal_id, "viewer", "u-owner", now),
        )
    for notebook_id, base_id, created_by in MOUNT_WORLD_BASES:
        run(
            "INSERT INTO notebook_bases "
            "(notebook_id,base_notebook_id,created_at,created_by) VALUES (?,?,?,?)",
            (notebook_id, base_id, now, created_by),
        )


def _viewer_row(module) -> str:
    """从 MOUNT_VIEWER_JOIN 里原样取出查看者行,单测查看者支时不另写一份归一。"""
    match = re.search(r"CROSS JOIN (\(SELECT .*? AS uid\) v)", module.MOUNT_VIEWER_JOIN)
    assert match is not None
    return match.group(1)


def mount_viewer_contract_failures(
    query: Callable[[str, Sequence[object]], list],
    module,
) -> list[str]:
    """在已播种的世界上跑完整个矩阵,返回全部不符(空列表 = 通过)。

    `query(sql, params)` 返回每行第一列组成的列表。三种用法各跑一遍:join 骨架 +
    过滤后缀 + 排序、`IN (...)` 子查询、以及把布尔谓词当投影列——任何一种拼装方式
    漏掉查看者都会在这里现形。
    """
    failures: list[str] = []
    filtered = (
        "SELECT b.id "
        + module.MOUNT_VIEWER_JOIN
        + module.MOUNT_EFFECTIVE_FOR_VIEWER
        + module.MOUNT_ORDER
    )
    in_subquery = (
        "SELECT n.id FROM notebooks n WHERE n.id IN ("
        + module.MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY
        + ")"
    )
    projected = (
        "SELECT b.id, "
        + module.MOUNT_EFFECTIVE_FOR_VIEWER_EXPR
        + " AS ok "
        + module.MOUNT_VIEWER_JOIN
    )
    old = "SELECT b.id " + module.MOUNT_JOIN + module.MOUNT_VALID + module.MOUNT_ORDER

    for notebook_id, old_expected in OLD_MOUNT_EFFECTIVE.items():
        old_ordered = query(old, (notebook_id,))
        if tuple(old_ordered) != OLD_MOUNT_ORDERED[notebook_id]:
            failures.append(f"old {notebook_id}: {old_ordered}")
        for viewer in MOUNT_WORLD_VIEWERS:
            case = f"{notebook_id} viewer={viewer!r}"
            expected = expected_mounts_for_viewer(notebook_id, viewer)
            params = (viewer, notebook_id)
            got_filtered = query(filtered, params)
            if frozenset(got_filtered) != expected or len(got_filtered) != len(expected):
                failures.append(f"{case} filtered: {got_filtered} != {sorted(expected)}")
            got_in = frozenset(query(in_subquery, params))
            if got_in != expected:
                failures.append(f"{case} in-subquery: {sorted(got_in)}")
            got_projected = frozenset(
                row_id
                for row_id, ok in query(projected, params, all_columns=True)
                if ok
            )
            if got_projected != expected:
                failures.append(f"{case} projected: {sorted(got_projected)}")
            # 收窄只许收,不许扩;对挂载人本人逐字等于今天的有效集合。
            if not expected <= old_expected:
                failures.append(f"{case}: 期望值跑出了旧有效集合")
            if viewer in ORDER_PINNED_VIEWERS[notebook_id] and got_filtered != old_ordered:
                failures.append(
                    f"{case}: 有序结果 {got_filtered} 不再逐行等于旧片段 {old_ordered}"
                )
        # 错序绑定:e.notebook_id 比的是一个用户 id,结果为空,不串到别的库。
        swapped = query(filtered, (notebook_id, MOUNTER.get(notebook_id, "u-owner")))
        if swapped:
            failures.append(f"{notebook_id} swapped binding returned {swapped}")

    reach = (
        "SELECT 1 FROM notebooks a CROSS JOIN notebooks b CROSS JOIN "
        + _viewer_row(module)
        + " WHERE a.id = 'nb-n' AND b.id = "
        + ("?" if module is sqlite_mount_sql else "%s")
        + " AND "
        + module._VIEWER_REACHES_MOUNT_EXPR
    )
    for base_id in ("nb-b", "nb-p", "nb-e", "nb-x"):
        for viewer in MOUNT_WORLD_VIEWERS:
            got = bool(query(reach, (viewer, base_id)))
            want = expected_reach_with_unknown_mounter(base_id, viewer)
            if got != want:
                failures.append(
                    f"unknown mounter reach {base_id} viewer={viewer!r}: {got} != {want}"
                )
    return failures


@pytest.fixture
def sqlite_mount_world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    now = datetime.now(timezone.utc).isoformat()
    with repo._write() as db:
        seed_mount_world(db.execute, "?", now)
    return repo


def _sqlite_query(repo):
    def query(sql: str, params: Sequence[object], *, all_columns: bool = False) -> list:
        rows = repo._connect().execute(sql, tuple(params)).fetchall()
        if all_columns:
            return [tuple(row) for row in rows]
        return [row[0] for row in rows]

    return query


def test_sqlite_mount_viewer_matrix(sqlite_mount_world):
    """M3 行为矩阵(SQLite,真实表)。PostgreSQL 同一份矩阵见
    `tests/postgres/test_mount_sql_viewer_pg.py`。"""
    failures = mount_viewer_contract_failures(
        _sqlite_query(sqlite_mount_world), sqlite_mount_sql
    )
    assert failures == []


def test_sqlite_viewer_fragments_refuse_a_single_parameter(sqlite_mount_world):
    """漏改的调用点照旧只绑一个 notebook_id:当场报错,而不是静默错绑。"""
    connection = sqlite_mount_world._connect()
    for sql in (
        "SELECT b.id " + sqlite_mount_sql.MOUNT_VIEWER_JOIN
        + sqlite_mount_sql.MOUNT_EFFECTIVE_FOR_VIEWER,
        "SELECT 1 WHERE 'nb-b' IN ("
        + sqlite_mount_sql.MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY + ")",
    ):
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute(sql, ("nb-a",)).fetchall()
