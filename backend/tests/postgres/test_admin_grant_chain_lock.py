"""管理级授权边**整条生效链**加锁探测的等价性契约(codex #519 R8 P1)。

`_require_notebook_manage_on` 在写事务里认管理权时,不能只锁那条 `notebook_grants` 边
——让 `group` / `group_admins` 边生效的是一行 `group_members`,它藏在 `EXISTS (...)` 里
根本锁不着,于是并发的移出组/降级可以提交在探测快照之后、持久授权边落库之前。

收口的手法是把成员行经**内连接**提到顶层(`FOR SHARE` 锁得住内连接的两侧,锁不住外连接
的可空侧),代价是判定被拆成两条语句:`user` 臂的链只有一环(没有成员行),而带锁的
`UNION` 在 PostgreSQL 里是语法错误。

**拆开就有分叉风险**,这份矩阵就是为它存在的:两条语句**合起来**必须与唯一定义点
`ADMIN_GRANT_PROBE_SQL` 逐格等价。少一条臂、`role='admin'` 收窄写漏、`everyone` 混进来,
都会让「谁能管这本库」在写路径与读路径上不是同一个答案——而那种分叉不报任何错。

矩阵每一格还**同时断言期望值**:只比对「两种写法一致」的话,两边一起坏掉(比如都漏了
`group_admins` 臂)照样全绿。
"""
from __future__ import annotations

import pytest

from app.repositories.postgres.access_sql import (
    ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
    ADMIN_GRANT_PROBE_SQL,
    ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL,
    admin_grant_group_chain_params,
    admin_grant_probe_params,
    admin_grant_user_arm_params,
)
from app.repositories.postgres.migrator import PostgresMigrator


_NOW = "2026-07-23T00:00:00+00:00"
_NOTEBOOK = "nb-chain"

#: 用户 -> 期望的「有管理级授权边」。owner 那一半不在本谓词范围内(它在
#: `_require_notebook_manage_on` 的第一段),所以这里的库主也只按边算。
_EXPECTED = {
    # user 主体 + role='admin' → 有
    "u-user-admin": True,
    # user 主体但边是 viewer → 无(管理级收窄的就是这一格)
    "u-user-viewer": False,
    # group 主体的边:**整组**都算,不要求组内 role
    "u-group-plain": True,
    "u-group-admin": True,
    # group_admins 主体的边:只有组内 role='admin' 算
    "u-ga-admin": True,
    "u-ga-plain": False,
    # everyone 边**绝不**授予管理权(裁决 P2-1 收窄),哪怕 role='admin'
    "u-everyone": False,
    # 正向 shadow 停车行的哨兵 principal_type:四值精确匹配 → 谁也匹配不上
    "u-sentinel": False,
    # 与本库无关的人
    "u-stranger": False,
}


def _seed(connection) -> None:
    users = sorted(_EXPECTED) + ["u-owner"]
    for index, user_id in enumerate(users):
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
            (user_id, f"{user_id}@example.test", user_id, _NOW, _NOW, f"c{index:08d}"),
        )
    connection.execute(
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at,tier) "
        "VALUES (%s,'Chain','','','ready','u-owner',%s,%s,'personal')",
        (_NOTEBOOK, _NOW, _NOW),
    )
    for group_id in ("grp-viewers", "grp-admins", "grp-sentinel"):
        connection.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES (%s,%s,'project','','u-owner',%s,%s)",
            (group_id, group_id, _NOW, _NOW),
        )
    memberships = (
        ("grp-viewers", "u-group-plain", "member"),
        ("grp-viewers", "u-group-admin", "admin"),
        ("grp-admins", "u-ga-admin", "admin"),
        ("grp-admins", "u-ga-plain", "member"),
        ("grp-sentinel", "u-sentinel", "admin"),
    )
    for group_id, user_id, role in memberships:
        connection.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
            "VALUES (%s,%s,%s,%s,'u-owner')",
            (group_id, user_id, role, _NOW),
        )
    grants = (
        ("gnt-user-admin", "user", "u-user-admin", "admin"),
        ("gnt-user-viewer", "user", "u-user-viewer", "viewer"),
        ("gnt-group", "group", "grp-viewers", "admin"),
        ("gnt-group-admins", "group_admins", "grp-admins", "admin"),
        ("gnt-everyone", "everyone", "", "admin"),
        # 停车行:哨兵 principal_type,四值白名单之外
        ("gnt-sentinel", "__parked_sentinel__", "grp-sentinel", "admin"),
    )
    for grant_id, principal_type, principal_id, role in grants:
        connection.execute(
            "INSERT INTO notebook_grants"
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (%s,%s,%s,%s,%s,'u-owner',%s)",
            (grant_id, _NOTEBOOK, principal_type, principal_id, role, _NOW),
        )


def _canonical(connection, user_id: str) -> bool:
    """唯一定义点(不加锁)的答案。"""
    return (
        connection.execute(
            ADMIN_GRANT_PROBE_SQL, admin_grant_probe_params(_NOTEBOOK, user_id)
        ).fetchone()
        is not None
    )


def _chained(connection, user_id: str) -> bool:
    """两条**整链加锁**语句合起来的答案 —— 与 `_require_notebook_manage_on` 逐字同序。"""
    if (
        connection.execute(
            ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL,
            admin_grant_user_arm_params(_NOTEBOOK, user_id),
        ).fetchone()
        is not None
    ):
        return True
    return (
        connection.execute(
            ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
            admin_grant_group_chain_params(_NOTEBOOK, user_id),
        ).fetchone()
        is not None
    )


@pytest.mark.postgres_integration
def test_the_locked_chain_pair_equals_the_single_definition_point(postgres_database):
    """逐格比对:两条加锁语句合起来 ≡ `ADMIN_GRANT_PROBE_SQL`,且都等于期望值。"""
    assert PostgresMigrator(postgres_database).migrate() == 43
    with postgres_database.write() as connection:
        _seed(connection)

    mismatches: list[str] = []
    wrong: list[str] = []
    # 加锁语句必须在事务里跑(`FOR SHARE` 在自动提交下无意义),用写事务顺带证明
    # 它们在真实调用姿势下也合法。
    with postgres_database.write() as connection:
        for user_id, expected in sorted(_EXPECTED.items()):
            canonical = _canonical(connection, user_id)
            chained = _chained(connection, user_id)
            if canonical != chained:
                mismatches.append(f"{user_id}: 唯一定义点={canonical} 加锁链={chained}")
            if canonical != expected:
                wrong.append(f"{user_id}: 期望={expected} 实得={canonical}")

    assert mismatches == [], (
        f"加锁链与唯一定义点分叉:{mismatches}。"
        "写事务里认管理权用的是加锁链,读路径用的是唯一定义点——两者不等就是"
        "「谁能管这本库」在两条路径上给出不同答案,而这种分叉不报任何错。"
    )
    assert wrong == [], (
        f"唯一定义点本身答错了:{wrong}。"
        "(这条断言让上面那条不至于在两边一起坏掉时空转。)"
    )


#: 用 `NOWAIT` 探测某一行现在拿不拿得到排他锁 —— 拿不到就说明别的事务正持有它。
#: ⚠ **不能查 `pg_locks`**:PostgreSQL 的行级锁写在元组头(xmax)里而不是共享锁表,
#: `pg_locks` 只在**等待期间**短暂出现一条 `tuple` 记录,持有期间什么都看不到。
_PROBES = {
    "notebook_grants": (
        "SELECT 1 FROM notebook_grants WHERE id='gnt-group-admins' FOR UPDATE NOWAIT"
    ),
    "group_members": (
        "SELECT 1 FROM group_members "
        "WHERE group_id='grp-admins' AND user_id='u-ga-admin' FOR UPDATE NOWAIT"
    ),
}


@pytest.mark.postgres_integration
def test_the_group_chain_statement_locks_both_links(postgres_database):
    """`FOR SHARE OF ng, ngm` 必须真的锁住**两环**,不只是授权边行。

    编排:A 线程在写事务里跑完 group 链语句后**不提交**,主线程另起连接对两条行各发一次
    `FOR UPDATE NOWAIT` —— 拿不到锁(`LockNotAvailable`)才算被锁住。

    摘成 `FOR SHARE OF ng` 之后 `group_members` 那一格会当场变成「拿得到」,而那正是
    R8 P1 那个洞:并发的移出组/降级得以提交在探测与 INSERT 之间。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from psycopg import errors

    assert PostgresMigrator(postgres_database).migrate() == 43
    with postgres_database.write() as connection:
        _seed(connection)

    chain_locked = threading.Event()
    allow_release = threading.Event()

    def hold_the_chain_locks():
        with postgres_database.write() as connection:
            assert _chained(connection, "u-ga-admin") is True
            chain_locked.set()
            assert allow_release.wait(timeout=10)
        return "released"

    def probe(sql: str) -> bool:
        """True = 拿不到锁(即那一行**被**持有)。"""
        try:
            with postgres_database.write() as connection:
                connection.execute(sql)
            return False
        except errors.LockNotAvailable:
            return True

    unlocked: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_the_chain_locks)
        try:
            assert chain_locked.wait(timeout=10)
            for table, sql in sorted(_PROBES.items()):
                if not executor.submit(probe, sql).result(timeout=10):
                    unlocked.append(table)
        finally:
            allow_release.set()
        assert holder.result(timeout=15) == "released"

    assert unlocked == [], (
        f"生效链没锁住这几张表的行:{unlocked} —— 两环都要锁。"
        "只锁授权边行时,并发的移出组/降级可以提交在探测与 INSERT 之间,"
        "一个管理权刚被撤销的人照样发得出新的访问权(codex #519 R8 P1)。"
    )
