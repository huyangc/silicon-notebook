"""PostgreSQL 行持久化:群组、组成员、笔记本授权边(群组知识共享 P1-T3)。

`app/repositories/sqlite/group_store.py` 的逐方法镜像:同名方法、同边界、同返回
形状。完整理由(为什么建组与最后一名组管理员保护必须在同一事务、为什么删组要显式
清 `notebook_grants`、为什么本模块不含任何授权判定谓词)写在 SQLite 那一份的模块
docstring 里,两份必须同修。

PG 侧独有的三件事:

* `timestamptz` 列读回来是 `datetime`,一律经 `iso_timestamp` 归一成 ISO 字符串——
  API 模型收的是 `str`,不归一会让同一个字段在两个后端上是两种类型。
* 排序键显式 `COLLATE "C"`:非 C collation 的库里 `ORDER BY id` 与 SQLite 的字节序
  不同,群组清单/成员清单的顺序会随后端漂。
* **成员变更显式对 `groups` 行加 `FOR UPDATE`**。SQLite 靠进程级写锁把所有写事务
  串起来,PG 没有那把锁——`read committed` 下两个并发的「把最后一名组管理员降级」
  各自都会读到 `COUNT(*)=1` 然后都提交,组就没人管了。锁**聚合根**(groups 行)而
  不是逐条成员行:要拦的是「同一个组的成员集合被并发改写」,而被降级的那两行本来
  就可以是不同的两行,锁它们互相看不见。
"""
from __future__ import annotations

from typing import Any, Callable

from psycopg import errors

from app.repositories.group_rows import (
    assert_share_request_decided_at,
    fold_shared_notebooks,
    resolve_grant_principal,
)
from app.repositories.ports import (
    GroupAdminRequiredError,
    GroupAdminShouldShareDirectlyError,
    GroupGrantAlreadyExists,
    GroupMembershipRequiredError,
    GroupNotFoundError,
    GroupOwnerProtectedError,
    GroupOwnerRequiredError,
    GroupOwnerTransferTargetError,
    LastGroupAdminError,
    ShareRequestAlreadyPendingError,
    NotebookManageRequiredError,
    NotebookNotFoundError,
    ShareRequestNotPendingError,
    ShareRequesterUnauthorizedError,
)
from app.repositories.postgres.access_sql import (
    ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
    ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL,
    NOTEBOOK_LIVE_SQL,
    admin_grant_group_chain_params,
    admin_grant_user_arm_params,
    read_access_clause,
    read_access_params,
)
from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase


# 见 SQLite 那份同名常量:LEFT JOIN 的 ON 条件带 `principal_type IN (...)` 是纵深
# 防御——今天的 id 前缀已经保证撞不上,但那条保证不属于这条 SQL。
_GRANT_SELECT = (
    "SELECT ng.*, g.name AS _group_name, g.kind AS _group_kind "
    "FROM notebook_grants ng "
    "LEFT JOIN groups g ON g.id=ng.principal_id "
    "AND ng.principal_type IN ('group','group_admins') "
)

# 见 SQLite 那份同名常量。三个 LEFT JOIN 恒能解析(CASCADE 外键保证父行随子行同在)。
_SHARE_REQUEST_SELECT = (
    "SELECT sr.*, nb.name AS _notebook_name, g.name AS _group_name, "
    "u.username AS _requested_by_username "
    "FROM notebook_share_requests sr "
    "LEFT JOIN notebooks nb ON nb.id = sr.notebook_id "
    "LEFT JOIN groups g ON g.id = sr.group_id "
    "LEFT JOIN users u ON u.id = sr.requested_by "
)


# 见 SQLite 那份同名常量的完整论证(codex #519 R12 P2):「我发起的待审批申请」是申请人
# **失权之后仍然够得着**的清单,所以两个展示标签必须各自按**当前**权限决定给不给,否则它
# 就成了一条持续输出改名后新名字的活通道。读权那半嵌 `access_sql.read_access_clause()`
# (唯一定义点),群组成员那半留在本模块自己写(它不是 notebook 授权谓词)。
_MY_PENDING_SHARE_REQUEST_SELECT = (
    "SELECT sr.*, "
    "CASE WHEN " + read_access_clause("nb") + " THEN nb.name ELSE '' END AS _notebook_name, "
    "CASE WHEN EXISTS (SELECT 1 FROM group_members gmv "
    "WHERE gmv.group_id = sr.group_id AND gmv.user_id = %s) "
    "THEN g.name ELSE '' END AS _group_name, "
    "u.username AS _requested_by_username "
    "FROM notebook_share_requests sr "
    "LEFT JOIN notebooks nb ON nb.id = sr.notebook_id "
    "LEFT JOIN groups g ON g.id = sr.group_id "
    "LEFT JOIN users u ON u.id = sr.requested_by "
)


class GroupStore:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = normalized_clock(now)

    # ------------------------------------------------------------- 行投影
    @staticmethod
    def _group_row(row, *, my_role: str = "", member_count: int = 0) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "description": row["description"],
            "owner_id": row["owner_id"],
            "my_role": my_role,
            "member_count": member_count,
            "created_at": iso_timestamp(row["created_at"]),
        }

    @staticmethod
    def _member_row(row) -> dict:
        return {
            "id": row["user_id"],
            "username": row["username"] or row["user_id"],
            "display_name": row["display_name"] or "",
            "role": row["role"],
            "added_at": iso_timestamp(row["added_at"]),
        }

    @staticmethod
    def _grant_row(row) -> dict:
        name, kind = resolve_grant_principal(
            row["principal_type"], row["_group_name"], row["_group_kind"]
        )
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "principal_type": row["principal_type"],
            "principal_id": row["principal_id"],
            "role": row["role"],
            "principal_name": name,
            "principal_kind": kind,
            "created_at": iso_timestamp(row["created_at"]),
        }

    @staticmethod
    def _share_request_row(row) -> dict:
        # `decided_at` 是 timestamptz(datetime 或 None):先归一成 ISO/None,**再**断言
        # ——SQLite 侧断言吃原始 TEXT,PG 侧断言吃归一后的值,两边归一后语义一致
        # (pending → None,已决定 → 非空 ISO)。PG 的 timestamptz 收不下空串(DB 直接
        # 类型报错),所以这里 `''` 根本到不了,断言仍作 None-ness 复核。
        status = row["status"]
        decided_at = iso_timestamp(row["decided_at"]) or None
        assert_share_request_decided_at(status, decided_at)
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "notebook_name": row["_notebook_name"] or "",
            "group_id": row["group_id"],
            "group_name": row["_group_name"] or "",
            "requested_by": row["requested_by"],
            "requested_by_username": row["_requested_by_username"] or row["requested_by"],
            "status": status,
            "decided_by": row["decided_by"],
            "decided_at": decided_at,
            "created_at": iso_timestamp(row["created_at"]),
        }

    # -------------------------------------------------------------- 群组
    def create_group(
        self, *, name: str, kind: str, description: str, created_by: str
    ) -> dict:
        group_id = self.new_id("grp")
        stamp = self.now()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO groups "
                "(id,name,kind,description,created_by,owner_id,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (group_id, name, kind, description, created_by, created_by, stamp, stamp),
            )
            connection.execute(
                "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
                "VALUES (%s,%s,'admin',%s,%s)",
                (group_id, created_by, stamp, created_by),
            )
        group = self.get_group(group_id, user_id=created_by)
        assert group is not None  # 同一进程刚提交
        return group

    def get_group(self, group_id: str, *, user_id: str = "") -> "dict | None":
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM groups WHERE id=%s", (group_id,)
            ).fetchone()
            if row is None:
                return None
            count = int(
                connection.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=%s",
                    (group_id,),
                ).fetchone()["c"]
            )
            role = self._role_on(connection, group_id, user_id) if user_id else None
        return self._group_row(row, my_role=role or "", member_count=count)

    def user_group_role(self, group_id: str, user_id: str) -> "str | None":
        with self.database.connect() as connection:
            return self._role_on(connection, group_id, user_id)

    @staticmethod
    def _role_on(connection: Any, group_id: str, user_id: str) -> "str | None":
        row = connection.execute(
            "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
            (group_id, user_id),
        ).fetchone()
        return row["role"] if row else None

    def list_groups_for_user(self, user_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT g.*, gm.role AS _my_role, "
                "(SELECT COUNT(*) FROM group_members c WHERE c.group_id=g.id) AS _members "
                "FROM group_members gm JOIN groups g ON g.id=gm.group_id "
                'WHERE gm.user_id=%s ORDER BY g.created_at ASC, g.id COLLATE "C" ASC',
                (user_id,),
            ).fetchall()
        return [
            self._group_row(
                row, my_role=row["_my_role"], member_count=int(row["_members"])
            )
            for row in rows
        ]

    def list_all_groups(self, *, user_id: str = "") -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT g.*, "
                "(SELECT role FROM group_members m "
                "WHERE m.group_id=g.id AND m.user_id=%s) AS _my_role, "
                "(SELECT COUNT(*) FROM group_members c WHERE c.group_id=g.id) AS _members "
                'FROM groups g ORDER BY g.created_at ASC, g.id COLLATE "C" ASC',
                (user_id,),
            ).fetchall()
        return [
            self._group_row(
                row, my_role=row["_my_role"] or "", member_count=int(row["_members"])
            )
            for row in rows
        ]

    def list_members(self, group_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT gm.user_id AS user_id, gm.role AS role, gm.added_at AS added_at, "
                "u.username AS username, u.display_name AS display_name "
                "FROM group_members gm LEFT JOIN users u ON u.id=gm.user_id "
                "WHERE gm.group_id=%s "
                'ORDER BY gm.added_at ASC, gm.user_id COLLATE "C" ASC',
                (group_id,),
            ).fetchall()
        return [self._member_row(row) for row in rows]

    def update_group(
        self, group_id: str, *, name: "str | None" = None, description: "str | None" = None
    ) -> bool:
        assignments: list[str] = []
        params: list[object] = []
        if name is not None:
            assignments.append("name=%s")
            params.append(name)
        if description is not None:
            assignments.append("description=%s")
            params.append(description)
        if not assignments:
            with self.database.connect() as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM groups WHERE id=%s", (group_id,)
                    ).fetchone()
                    is not None
                )
        assignments.append("updated_at=%s")
        params.extend([self.now(), group_id])
        with self.database.write() as connection:
            cursor = connection.execute(
                f"UPDATE groups SET {', '.join(assignments)} WHERE id=%s", params
            )
        return int(cursor.rowcount or 0) > 0

    def transfer_group_owner(
        self,
        group_id: str,
        *,
        new_owner_id: str,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> dict:
        """Transfer owner and promote the target under the group root lock."""
        with self.database.write() as connection:
            self._lock_group_on(connection, group_id)
            row = connection.execute(
                "SELECT owner_id FROM groups WHERE id=%s", (group_id,)
            ).fetchone()
            if not actor_is_system_admin and row["owner_id"] != actor_id:
                raise GroupOwnerRequiredError(group_id)
            if self._role_on(connection, group_id, new_owner_id) is None:
                raise GroupOwnerTransferTargetError(new_owner_id)
            connection.execute(
                "UPDATE group_members SET role='admin' "
                "WHERE group_id=%s AND user_id=%s",
                (group_id, new_owner_id),
            )
            connection.execute(
                "UPDATE groups SET owner_id=%s, updated_at=%s WHERE id=%s",
                (new_owner_id, self.now(), group_id),
            )
            projected = connection.execute(
                "SELECT * FROM groups WHERE id=%s", (group_id,)
            ).fetchone()
            member_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=%s",
                    (group_id,),
                ).fetchone()["c"]
            )
            actor_role = self._role_on(connection, group_id, actor_id)
            return self._group_row(
                projected,
                my_role=actor_role or "",
                member_count=member_count,
            )

    def delete_group(
        self,
        group_id: str,
        *,
        actor_id: str | None = None,
        actor_is_system_admin: bool = False,
    ) -> bool:
        """删组 + 清掉指向本组的全部授权边,一个写事务(已定裁决 3)。

        ⚠ **锁必须在最前面**:`_lock_group_on` 先把 `groups` 行排他锁住,再清边、再删
        组行。反过来(先清边、后删组)在 PG 上会留下**孤儿授权边**——`create_grant`
        持的是同一行的 `FOR SHARE`,它可以在「清边」与「删组」之间提交一条新边:
        清理已经走过去了,而 `notebook_grants.principal_id` 没有外键,`DELETE FROM
        groups` 带不走它。结果是组没了、边还在,而那种边只能靠 `merge_dbs` 的孤儿清扫
        或库主在共享清单里看见 `principal_kind="missing"` 才被发现。
        把锁提到事务开头,并发的 `create_grant` 要么在删组之前整个完成(它那条边随后
        被清理带走),要么等到删组提交后发现组已不在而抛 `GroupNotFoundError`。

        SQLite 侧**刻意没有对等改动**:`SqliteDatabase.write()` 是进程级写锁,同一时刻
        只有一个写事务在跑,「清边」与「删组」之间根本插不进另一个事务。那边加一次
        `SELECT ... ` 只会多一次查询而不多一分保证。

        存在性沿用 `bool` 返回而不是让 `GroupNotFoundError` 冒出去:两个后端的
        `delete_group` 契约必须一致(SQLite 侧删一个不存在的组返回 `False`),路由层
        据此给 404。
        """
        with self.database.write() as connection:
            try:
                self._lock_group_on(connection, group_id)
            except GroupNotFoundError:
                return False
            if actor_id is not None and not actor_is_system_admin:
                owner = connection.execute(
                    "SELECT owner_id FROM groups WHERE id=%s", (group_id,)
                ).fetchone()["owner_id"]
                if owner != actor_id:
                    raise GroupOwnerRequiredError(group_id)
            connection.execute(
                "DELETE FROM notebook_grants "
                "WHERE principal_type IN ('group','group_admins') AND principal_id=%s",
                (group_id,),
            )
            cursor = connection.execute(
                "DELETE FROM groups WHERE id=%s", (group_id,)
            )
        return int(cursor.rowcount or 0) > 0

    # -------------------------------------------------------------- 成员
    @staticmethod
    def _lock_group_on(connection: Any, group_id: str, *, mode: str = "UPDATE") -> None:
        """锁住聚合根,顺便复核它还在;不在就 `GroupNotFoundError`。

        锁的理由见模块 docstring 的第三条。**返回值不可省**这件事是单独一条:并发
        的删组请求可以整个跑完在路由层那次前置检查与本事务之间,`FOR UPDATE` 只是让
        本事务**等到**那次删除提交,等完之后行就真的没了。忽略这个事实继续往下走,
        `INSERT INTO group_members` 会撞外键抛 `ForeignKeyViolation`——用户拿到 500,
        而正确答案是 404。所以这里直接抛,调用方无从忽略。

        ``mode`` 只有两档:改成员用 `FOR UPDATE`(排他,把同组的成员变更串起来);
        发授权边用 `FOR SHARE`(只要保证「复核期间这个组不会被删」,不必把并发的
        发边互相排开)。两档都与 `DELETE FROM groups` 互斥,那正是要防的那件事。
        """
        if (
            connection.execute(
                f"SELECT id FROM groups WHERE id=%s FOR {mode}", (group_id,)
            ).fetchone()
            is None
        ):
            raise GroupNotFoundError(group_id)

    @staticmethod
    def _lock_notebook_on(connection: Any, notebook_id: str) -> None:
        """锁住笔记本行,顺便复核它还在;不在就 `NotebookNotFoundError`(codex #519 R7)。

        `_lock_group_on` 的**同类兄弟**,只是换了另一个外键父行:一次插入有几个外键,就有
        几个父行可能在能力守卫通过之后被并发删掉,只堵组那一个不算堵住。少了它,
        `INSERT INTO notebook_share_requests` 撞 `notebook_id` 外键抛 `ForeignKeyViolation`
        ——用户拿到 500,而正确答案是 404。SQLite 侧对应的是 `_require_notebook_on`。

        ⚠ **锁模式是 `FOR KEY SHARE`,不是 `FOR SHARE`,这一格不能随手改宽。**

        `FOR KEY SHARE` 恰好就是 PostgreSQL 自己在几条语句之后执行那次 INSERT 时,为满足
        外键而对**同一行**取的锁。所以这句显式锁**不新增任何冲突边**——它只是把本事务
        本来就要取的那把锁提前几条语句取到手,好让我们有机会当场返回 404,而不是等 INSERT
        炸出一个未处理异常。改成 `FOR SHARE` 就不一样了:它额外与 `FOR NO KEY UPDATE`
        冲突,也就是与**任何一次普通的** `UPDATE notebooks SET …`(改名、改状态、推
        `updated_at`),以及建源容量闸的 `FOR NO KEY UPDATE`
        (`source_store._lock_notebook_row_for_capacity`)互相阻塞——凭空把本路径拖进
        一整类它不需要的阻塞关系。而防住并发删库只需要与 `FOR UPDATE`
        (`DELETE FROM notebooks` 取的正是它)冲突,`FOR KEY SHARE` 已经做到。

        **锁序论证**(为什么在 `create_share_request` 里排在 `_lock_group_on` 之后是安全的):

        1. 全仓**只有本文件**会锁 `groups` 行(`FOR UPDATE`/`FOR SHARE` 的全量枚举),而
           这里的七个调用点(`delete_group` / `upsert_member` / `remove_member` /
           `create_grant` / `create_share_request` / `approve_share_request` /
           `reject_share_request`)**无一例外**把 `_lock_group_on` 写成写事务的**第一条**
           语句。于是 `groups` 是一把「根锁」:
           没有任何事务能在**持有别的锁**的状态下去等 `groups`,成环的必要条件不成立。
        2. 因此新锁只可能与 `groups` **之后**的资源成环。而 `create_share_request` 在
           `groups → notebooks` 这个顺序上**今天就已经是这样了**——INSERT 的外键检查正是
           在持有 `groups` 锁的情况下去取 `notebooks` 的 `FOR KEY SHARE`。本次改动没有引入
           新的资源对,只是把同一对的获取时刻提前。
        3. 反方向(先 `notebooks` 后 `groups`)不存在:删库(`DELETE FROM notebooks`)持
           `notebooks` 的行锁后级联删 `notebook_grants` / `notebook_share_requests` 等**子**
           行,删子行从不需要父行(`groups`)的锁;`memory_store` 的三段式是
           `notebooks FOR SHARE → notebook_members/notebook_grants FOR SHARE`,同样不碰
           `groups`。
        """
        if (
            connection.execute(
                "SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE", (notebook_id,)
            ).fetchone()
            is None
        ):
            raise NotebookNotFoundError(notebook_id)

    @staticmethod
    def _admin_count_on(connection: Any, group_id: str) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS c FROM group_members "
                "WHERE group_id=%s AND role='admin'",
                (group_id,),
            ).fetchone()["c"]
        )

    def upsert_member(
        self, group_id: str, user_id: str, *, role: str, added_by: str
    ) -> str:
        """加人 / 改角色。组已被并发删掉 → `GroupNotFoundError`;把最后一名组管理员
        降级 → `LastGroupAdminError`。两条判定都在写事务内。"""
        with self.database.write() as connection:
            self._lock_group_on(connection, group_id)
            current = self._role_on(connection, group_id, user_id)
            owner = connection.execute(
                "SELECT owner_id FROM groups WHERE id=%s", (group_id,)
            ).fetchone()["owner_id"]
            if user_id == owner and current is not None and role != "admin":
                raise GroupOwnerProtectedError(group_id)
            if (
                current == "admin"
                and role != "admin"
                and self._admin_count_on(connection, group_id) <= 1
            ):
                raise LastGroupAdminError(group_id)
            if current is None:
                connection.execute(
                    "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (group_id, user_id, role, self.now(), added_by),
                )
                return "added"
            connection.execute(
                "UPDATE group_members SET role=%s WHERE group_id=%s AND user_id=%s",
                (role, group_id, user_id),
            )
        return "updated"

    def remove_member(self, group_id: str, user_id: str) -> bool:
        """移除成员(自助退出走同一条路径)。移除最后一名组管理员 → 报错。"""
        with self.database.write() as connection:
            self._lock_group_on(connection, group_id)
            current = self._role_on(connection, group_id, user_id)
            if current is None:
                return False
            owner = connection.execute(
                "SELECT owner_id FROM groups WHERE id=%s", (group_id,)
            ).fetchone()["owner_id"]
            if user_id == owner:
                raise GroupOwnerProtectedError(group_id)
            if current == "admin" and self._admin_count_on(connection, group_id) <= 1:
                raise LastGroupAdminError(group_id)
            connection.execute(
                "DELETE FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id),
            )
        return True

    # ---------------------------------------------------------- 邀请链接
    def get_invite_state(
        self,
        group_id: str,
        *,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> dict:
        with self.database.write() as connection:
            self._lock_group_on(connection, group_id)
            if (
                not actor_is_system_admin
                and self._role_on(connection, group_id, actor_id) != "admin"
            ):
                raise GroupAdminRequiredError(group_id)
            row = connection.execute(
                "SELECT invite_token,invite_created_at FROM groups WHERE id=%s",
                (group_id,),
            ).fetchone()
        token = row["invite_token"] or ""
        return {
            "active": bool(token),
            "token": token,
            "created_at": (
                iso_timestamp(row["invite_created_at"]) or None
            ) if token else None,
        }

    def issue_invite(
        self,
        group_id: str,
        *,
        token: str,
        actor_id: str,
        actor_is_system_admin: bool = False,
        rotate: bool = False,
    ) -> dict:
        with self.database.write() as connection:
            self._lock_group_on(connection, group_id)
            if (
                not actor_is_system_admin
                and self._role_on(connection, group_id, actor_id) != "admin"
            ):
                raise GroupAdminRequiredError(group_id)
            current = connection.execute(
                "SELECT invite_token,invite_created_at FROM groups WHERE id=%s",
                (group_id,),
            ).fetchone()
            chosen = (current["invite_token"] or "") if not rotate else ""
            if chosen:
                return {
                    "active": True,
                    "token": chosen,
                    "created_at": iso_timestamp(current["invite_created_at"]) or None,
                }
            stamp = self.now()
            connection.execute(
                "UPDATE groups SET invite_token=%s,invite_created_at=%s,"
                "invite_created_by=%s,updated_at=%s WHERE id=%s",
                (token, stamp, actor_id, stamp, group_id),
            )
        return {
            "active": True,
            "token": token,
            "created_at": iso_timestamp(stamp) or str(stamp),
        }

    def revoke_invite(
        self,
        group_id: str,
        *,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> bool:
        with self.database.write() as connection:
            self._lock_group_on(connection, group_id)
            if (
                not actor_is_system_admin
                and self._role_on(connection, group_id, actor_id) != "admin"
            ):
                raise GroupAdminRequiredError(group_id)
            connection.execute(
                "UPDATE groups SET invite_token=NULL,invite_created_at=NULL,"
                "invite_created_by=NULL,updated_at=%s WHERE id=%s",
                (self.now(), group_id),
            )
        return True

    def join_by_invite(self, token: str, *, user_id: str) -> "dict | None":
        """Lock the group row before inserting membership or observing revoke."""
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM groups WHERE invite_token=%s FOR UPDATE",
                (token,),
            ).fetchone()
            if row is None:
                return None
            current = self._role_on(connection, row["id"], user_id)
            if current is None:
                connection.execute(
                    "INSERT INTO group_members "
                    "(group_id,user_id,role,added_at,added_by) "
                    "VALUES (%s,%s,'member',%s,%s)",
                    (row["id"], user_id, self.now(), user_id),
                )
                current = "member"
            member_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=%s",
                    (row["id"],),
                ).fetchone()["c"]
            )
            return self._group_row(
                row, my_role=current, member_count=member_count
            )

    def find_user_by_username(self, username: str) -> "dict | None":
        return self._user_lookup(
            "SELECT id, username, display_name FROM users WHERE username=%s", username
        )

    def find_user_by_id(self, user_id: str) -> "dict | None":
        return self._user_lookup(
            "SELECT id, username, display_name FROM users WHERE id=%s", user_id
        )

    def _user_lookup(self, sql: str, value: str) -> "dict | None":
        with self.database.connect() as connection:
            row = connection.execute(sql, (value,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"] or row["id"],
            "display_name": row["display_name"] or "",
        }

    # ------------------------------------------------------------ 授权边
    def list_grants(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                _GRANT_SELECT
                + "WHERE ng.notebook_id=%s "
                + 'ORDER BY ng.created_at ASC, ng.id COLLATE "C" ASC',
                (notebook_id,),
            ).fetchall()
        return [self._grant_row(row) for row in rows]

    def create_grant(
        self,
        notebook_id: str,
        *,
        principal_type: str,
        principal_id: str,
        role: str,
        created_by: str,
        admin_user_id: str,
    ) -> dict:
        """`sqlite/group_store.py::create_grant` 的镜像:插入 + **同事务**复核双重
        条件的群组那一半(组还在、发起者仍是它的组管理员)。

        PG 侧多一把 `FOR SHARE`(而不是 SQLite 的裸存在性查询):它让复核与插入之间
        这个组删不掉。用 `FOR SHARE` 而非 `FOR UPDATE` 是因为要防的只有「同时被删」,
        并发的两次发边互不冲突(真撞上了由 UNIQUE 约束负责)。

        **笔记本那个外键父行同样要复核**(`_lock_notebook_on`,codex #519 R7 存疑项收口):
        `_require_notebook_manage_on` 的 owner 分支是一条**无锁** SELECT 且当场短路,库主
        并发删库若恰好提交在它与 INSERT 之间,`notebook_grants.notebook_id` 外键当场违例
        (`ForeignKeyViolation`),而本方法只 catch `UniqueViolation` → 500。非 owner 分支
        本来就安全(`FOR SHARE OF ng` 锁住授权边行,删库要 CASCADE 掉它,必须先拿同一把锁),
        所以洞只有 owner 这一格——但复核**不按分支收窄**:existence 是先决条件,谁发起都一样。
        """
        grant_id = self.new_id("gnt")
        try:
            with self.database.write() as connection:
                self._lock_group_on(connection, principal_id, mode="SHARE")
                if self._role_on(connection, principal_id, admin_user_id) != "admin":
                    raise GroupAdminRequiredError(principal_id)
                # 笔记本维度两条,**存在性在前、权限在后**(与 `create_share_request`
                # 同一顺序):库都不在了,「他还有没有管理权」不是一个有意义的问题。
                # 刻意插在这里而不是紧跟 `_lock_group_on`——群组维度的错误优先级因此
                # 逐字不变(不是组管理员仍先拿它自己的 403)。锁序不受影响:`groups`
                # 两种写法下都已在手,`groups → notebooks` 与 `create_share_request`
                # 一致(论证见 `_lock_notebook_on`)。
                self._lock_notebook_on(connection, notebook_id)
                # **笔记本侧**的那一半同样要在事务内复核(codex #519 R6 P1):
                # 能力守卫放行之后、本事务开始之前,库主可以撤掉发起人的管理边;
                # 少了这一次复核,失权者仍能发出一条**新的**授权边(甚至给另一个
                # 组),把访问权继续散出去。判据见 `api/deps.py` 那条裁决:凡是写
                # `notebook_grants` 的路径都必须事务内复检并锁住发起人的权限。
                self._require_notebook_manage_on(
                    connection, notebook_id, admin_user_id
                )
                connection.execute(
                    "INSERT INTO notebook_grants "
                    "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        grant_id,
                        notebook_id,
                        principal_type,
                        principal_id,
                        role,
                        created_by,
                        self.now(),
                    ),
                )
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "uq_notebook_grants_principal":
                raise GroupGrantAlreadyExists(notebook_id, principal_id) from exc
            raise
        with self.database.connect() as connection:
            row = connection.execute(
                _GRANT_SELECT + "WHERE ng.id=%s", (grant_id,)
            ).fetchone()
        return self._grant_row(row)

    def delete_grant(self, notebook_id: str, grant_id: str) -> bool:
        with self.database.write() as connection:
            cursor = connection.execute(
                "DELETE FROM notebook_grants WHERE id=%s AND notebook_id=%s",
                (grant_id, notebook_id),
            )
        return int(cursor.rowcount or 0) > 0

    def list_group_shared_notebooks(
        self, group_id: str, *, include_admin_only: bool = True
    ) -> list[dict]:
        principal_clause = (
            "ng.principal_type IN ('group','group_admins')"
            if include_admin_only
            else "ng.principal_type='group'"
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT ng.notebook_id AS notebook_id, ng.role AS role, "
                "nb.name AS name, u.username AS owner_username "
                "FROM notebook_grants ng "
                "JOIN notebooks nb ON nb.id=ng.notebook_id "
                "LEFT JOIN users u ON u.id=nb.created_by "
                f"WHERE {principal_clause} "
                f"AND ng.principal_id=%s AND nb.{NOTEBOOK_LIVE_SQL} "
                'ORDER BY nb.created_at ASC, nb.id COLLATE "C" ASC, '
                'ng.id COLLATE "C" ASC',
                (group_id,),
            ).fetchall()
        return fold_shared_notebooks(rows)

    def delete_group_grants_for_notebook(self, group_id: str, notebook_id: str) -> int:
        with self.database.write() as connection:
            cursor = connection.execute(
                "DELETE FROM notebook_grants "
                "WHERE notebook_id=%s AND principal_id=%s "
                "AND principal_type IN ('group','group_admins')",
                (notebook_id, group_id),
            )
        return int(cursor.rowcount or 0)

    # -------------------------------------------------------- 共享申请(P2-T3)
    def _pending_share_request(
        self, notebook_id: str, group_id: str
    ) -> "dict | None":
        with self.database.connect() as connection:
            row = connection.execute(
                _SHARE_REQUEST_SELECT
                + "WHERE sr.notebook_id=%s AND sr.group_id=%s AND sr.status='pending'",
                (notebook_id, group_id),
            ).fetchone()
        return self._share_request_row(row) if row is not None else None

    def create_share_request(
        self, notebook_id: str, *, group_id: str, requested_by: str
    ) -> dict:
        """`sqlite/group_store.py::create_share_request` 的镜像:插入 + 撞 pending 唯一
        索引返回既有行(幂等)。`decided_at` 不写列、留 NULL(绝不写 `''`——PG 的
        timestamptz 收到空串会类型报错,poison 整条 shadow 正向复制通道)。

        PG 侧多两把行锁:`_lock_group_on(mode="SHARE")` 复核组还在(与 `create_grant` 同一
        手法),`_lock_notebook_on` 复核笔记本还在。**两个外键父行都要复核**——只堵组那一个
        等于把 `notebook_id` 的 `ForeignKeyViolation` 留成 500(codex #519 R7 P2)。两把锁的
        先后不可颠倒,完整锁序论证写在 `_lock_notebook_on` 的 docstring 里(一句话版:
        `groups` 是全仓唯一的「根锁」,`groups → notebooks` 正是今天 INSERT 外键检查已经在
        走的顺序)。唯一索引冲突在 `write()` 块**之外**捕获(UniqueViolation 会中止 PG 事务,
        必须先整体回滚再另起只读事务取既有 pending 行)。

        成员资格同样在事务内复核(`GroupMembershipRequiredError`),理由与 SQLite 侧逐字
        相同:路由那次查询与插入之间,`requested_by` 可以被移出组(codex #519 R2 P2-1)。
        """
        request_id = self.new_id("shr")
        # 见 SQLite 侧同名方法:最多两轮,按申请者收窄的幂等 + 恢复期间被决定就重试插入。
        for _attempt in range(2):
            try:
                with self.database.write() as connection:
                    # 锁序 group → notebook,不可颠倒(论证见 `_lock_notebook_on`)。
                    self._lock_group_on(connection, group_id, mode="SHARE")
                    self._lock_notebook_on(connection, notebook_id)
                    self._require_plain_membership_on(
                        connection, group_id, requested_by
                    )
                    connection.execute(
                        "INSERT INTO notebook_share_requests "
                        "(id,notebook_id,group_id,requested_by,status,created_at) "
                        "VALUES (%s,%s,%s,%s, 'pending', %s)",
                        (request_id, notebook_id, group_id, requested_by, self.now()),
                    )
                    # 终态投影在事务内读回(理由同 approve:事务外读会被并发删撞成 None)。
                    row = connection.execute(
                        _SHARE_REQUEST_SELECT + "WHERE sr.id=%s", (request_id,)
                    ).fetchone()
                return self._share_request_row(row)
            except errors.UniqueViolation as exc:
                if exc.diag.constraint_name != "uq_share_requests_one_pending":
                    raise
                # UniqueViolation 会中止 PG 事务,所以回读必须在 `write()` 块**之外**、
                # 整体回滚之后另起一个只读事务。
                existing = self._pending_share_request(notebook_id, group_id)
                if existing is None:
                    continue  # 恢复期间那条 pending 被决定/撤回 —— 重试一次插入。
                if existing["requested_by"] != requested_by:
                    raise ShareRequestAlreadyPendingError(notebook_id, group_id) from exc
                return existing
        raise ShareRequestAlreadyPendingError(notebook_id, group_id)

    def list_pending_share_requests(self, group_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                _SHARE_REQUEST_SELECT
                + "WHERE sr.group_id=%s AND sr.status='pending' "
                + 'ORDER BY sr.created_at ASC, sr.id COLLATE "C" ASC',
                (group_id,),
            ).fetchall()
        return [self._share_request_row(row) for row in rows]

    def list_my_share_requests(
        self, notebook_id: str, *, requested_by: str
    ) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                _SHARE_REQUEST_SELECT
                + "WHERE sr.notebook_id=%s AND sr.requested_by=%s "
                + 'ORDER BY sr.created_at DESC, sr.id COLLATE "C" ASC',
                (notebook_id, requested_by),
            ).fetchall()
        return [self._share_request_row(row) for row in rows]

    def list_pending_share_requests_by_requester(
        self, requested_by: str
    ) -> list[dict]:
        """`sqlite/group_store.py::list_pending_share_requests_by_requester` 的镜像:
        我发起的、仍待审批的全部申请,唯一谓词是 `requested_by`(与撤回的授权轴逐字相同)。
        完整理由(裁决 P2-7 的另一半)写在 SQLite 那一份。两个展示标签各自按当前权限
        决定给不给,见 `_MY_PENDING_SHARE_REQUEST_SELECT`(codex #519 R12 P2)。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                _MY_PENDING_SHARE_REQUEST_SELECT
                + "WHERE sr.requested_by=%s AND sr.status='pending' "
                + 'ORDER BY sr.created_at DESC, sr.id COLLATE "C" ASC',
                # 参数顺序跟着 SQL 文本走(两个 CASE 在 SELECT 列表里,排在 WHERE 之前)。
                (*read_access_params(requested_by), requested_by, requested_by),
            ).fetchall()
        return [self._share_request_row(row) for row in rows]

    def _require_plain_membership_on(
        self, connection: Any, group_id: str, user_id: str
    ) -> None:
        """`sqlite/group_store.py::_require_plain_membership_on` 的镜像:目标组的**普通
        成员**才能提交共享申请;完整理由(为什么两种不合格必须给不同的错误、为什么放行
        判据写成正向 `== 'member'`)写在 SQLite 那一份。

        ⚠ 这里**不额外加锁**:调用点 `create_share_request` 已经在本事务开头对
        `groups` 行取了 `FOR SHARE`,而成员资格的每一次变更(`upsert_member` /
        `remove_member` / `delete_group`)都先取同一行的 `FOR UPDATE`——两者互斥,
        本次读到的角色在本事务提交前不会被改写。这与 `_require_notebook_manage_on`
        必须自己锁整条链的处境不同:那里没有任何一把已经在手的锁能覆盖成员行。
        """
        role = self._role_on(connection, group_id, user_id)
        if role is None:
            raise GroupMembershipRequiredError(group_id)
        if role != "member":
            raise GroupAdminShouldShareDirectlyError(group_id)

    def _require_share_decider_on(
        self,
        connection: Any,
        group_id: str,
        decided_by: str,
        is_system_admin: bool,
    ) -> None:
        """`sqlite/group_store.py::_require_share_decider_on` 的镜像:在当前写事务里复核
        这个人此刻仍有权审批本组的共享申请;完整理由写在 SQLite 那一份。

        PG 侧调用点排在 `FOR UPDATE` 锁住申请行**之后**:那把锁已经把并发的同一条申请的
        审批串起来了,资格复核紧随其后读到的成员行就是本事务要据以决定的那一份。
        """
        if is_system_admin:
            return
        if self._role_on(connection, group_id, decided_by) != "admin":
            raise GroupAdminRequiredError(group_id)

    def _require_notebook_manage_on(
        self, connection: Any, notebook_id: str, user_id: str
    ) -> None:
        """`sqlite/group_store.py::_require_notebook_manage_on` 的镜像,但这里
        是**两段式带锁**写法(codex #519 R5)——与 `memory_store._lock_memory_aggregate_on`
        同一手法。

        ⚠ 为什么不能直接跑 `NOTEBOOK_ADMIN_SQL`:那是一条**不加锁**的 SELECT,PG 在
        READ COMMITTED 下让它看到语句开始时的快照。库主并发 `DELETE` 掉申请人的 admin 边
        并提交,本事务仍读到撤销前的行,随后 INSERT 那条 `(group, viewer)` 持久边并提交
        ——R4 要防的越权照样发生,只是窗口更窄。而给它加 `FOR SHARE` 也救不了:授权边行
        藏在 `EXISTS (...)` 子查询里,行锁够不着,只会锁住 `notebooks` 那一行(access_sql
        模块 docstring 里 `FOR SHARE OF ng` 那段讲的正是这件事)。

        所以拆成两段,各自锁自己那半:

        1. **owner 半**:`notebooks.created_by`。刻意**不加锁**——产品没有转让 owner 的
           功能(`notebooks.created_by` 只在建库与深拷贝时写入),owner 身份不可能在本
           事务执行期间被撤销,锁它只会多一次无谓的行锁争用。这个前提写在这里,将来真
           加了转让功能就得回来给它补 `FOR SHARE`。

           ⚠ 这条论的是「**身份**不会变」,**不是**「那**一行**还在」——两者是不同维度:
           `DELETE FROM notebooks` 让整行消失,与「谁是 owner」毫无关系,所以上面那个前提
           再成立也盖不住它。行还在这件事由**两个调用方各自**负责,手段不同(codex #519
           R7 存疑项收口):`create_grant` 在本方法之前显式 `_lock_notebook_on`;
           `approve_share_request` 靠它对申请行的 `FOR UPDATE` ——删库要 CASCADE 掉那一行,
           必须先拿到同一把锁,于是删不进来。少了那一层,
           owner 分支这条无锁 SELECT 与随后的 INSERT 之间可以插进一次**已提交**的删库,
           `notebook_grants.notebook_id` 外键当场违例 → 未处理异常 → 500。
        2. **授权边半**:顶层查 `notebook_grants` 并锁住命中的边行。锁住之后,并发的
           撤销会**阻塞**到本事务提交,撤销随后生效——序列化顺序是「批准在前、撤销在
           后」,语义正确:库主看到的是「我撤销时它刚好已经批了」,而不是「我明明撤销了
           它还是批了」。

           ⚠ **这一半必须锁整条生效链,不能只锁边行**(codex #519 R8 P1)。管理权来自
           `group` / `group_admins` 边时,让那条边生效的是一行 `group_members`;R5 当时
           只锁了边行,而成员行藏在 `EXISTS (...)` 里根本锁不着,于是并发的移出组/降级
           可以提交在探测快照之后、INSERT 之前——一个管理权**刚刚被撤销**的人照样发出
           了新的访问权。「边还在」与「让边生效的成员资格还在」是同一条链的两个环节,
           堵一端等于没堵。解法与当年把授权边**提到顶层**是同一招:内连接让成员行也
           进入顶层 rangetable,`FOR SHARE OF ng, ngm` 一条语句锁住两环。

           拆成 user 臂与 group 链两条语句,是因为 user 臂的链只有一环(主体就是这个人
           自己,没有成员行),而带锁的 `UNION` 在 PG 里是语法错误。两条**合起来**必须
           与唯一定义点 `ADMIN_GRANT_PROBE_SQL` 逐格等价,由
           `tests/postgres/test_admin_grant_chain_lock.py` 的数据驱动矩阵钉住。
        """
        if (
            connection.execute(
                "SELECT 1 FROM notebooks WHERE id=%s AND created_by=%s",
                (notebook_id, user_id),
            ).fetchone()
            is not None
        ):
            return
        if (
            connection.execute(
                ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL,
                admin_grant_user_arm_params(notebook_id, user_id),
            ).fetchone()
            is not None
        ):
            return
        if (
            connection.execute(
                ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
                admin_grant_group_chain_params(notebook_id, user_id),
            ).fetchone()
            is None
        ):
            raise NotebookManageRequiredError(notebook_id)

    def approve_share_request(
        self,
        group_id: str,
        request_id: str,
        *,
        decided_by: str,
        decided_by_is_system_admin: bool = False,
    ) -> "dict | None":
        """`sqlite` 镜像:同事务里复核仍 pending → 写 `(group, viewer)` 边(已共享则
        `ON CONFLICT DO NOTHING` 幂等)→ 状态置 approved。

        对**申请行** `FOR UPDATE`(防并发双审):第二个并发批准在第一个提交后重估
        `status='pending'` 谓词(EvalPlanQual),此时已是 approved、不再匹配 → 返回
        `None`。`status='pending'` 是精确匹配(红线)。

        ⚠ **写事务开头必须先锁 groups 行**(`_lock_group_on(mode="SHARE")`),与
        `create_grant` / `create_share_request` 同一手法:approve 是**真正写
        `notebook_grants` 边**的地方,而 `notebook_grants.principal_id` 是多态无 FK 列,
        `DELETE FROM groups` 的 CASCADE 带不走它(见 `delete_group` docstring)。不先锁组,
        一个并发的 `delete_group` 可以在「它清完本组的边」与「A 插入新边」之间穿过去 ——
        它清边发生在 A 插入之前、删组发生在 A 提交之后,于是 A 那条边指向一个已不存在的组
        = **孤儿边**(真 PG 实测已复现)。`FOR SHARE` 让 `delete_group` 的 `FOR UPDATE`
        等到本事务提交后才拿到组行,那时它的清边步骤会把 A 刚写的边一并带走;若组已先被删,
        本事务在这里 `GroupNotFoundError` → 落 `None`(路由 404),而不是让 500 冒出去。
        锁序 group→request,与 `delete_group` 的 group→cascade 一致,无死锁。

        SQLite 侧**刻意没有对等改动**:`SqliteDatabase.write()` 是进程级写锁,approve 与
        delete_group 不可能交错(理由同 `create_share_request` / `delete_group`)。
        """
        stamp = self.now()
        with self.database.write() as connection:
            try:
                self._lock_group_on(connection, group_id, mode="SHARE")
            except GroupNotFoundError:
                return None
            row = connection.execute(
                "SELECT notebook_id, requested_by FROM notebook_share_requests "
                "WHERE id=%s AND group_id=%s AND status='pending' FOR UPDATE",
                (request_id, group_id),
            ).fetchone()
            if row is None:
                return None
            self._require_share_decider_on(
                connection, group_id, decided_by, decided_by_is_system_admin
            )
            notebook_id = row["notebook_id"]
            # 申请人复检:失权 → 语义是「这条**申请**不能兑现」(409),不是「你没权限」,
            # 所以把通用的管理权错误翻译成 share-request 专用的那一个(R4 裁决)。
            try:
                self._require_notebook_manage_on(
                    connection, notebook_id, row["requested_by"]
                )
            except NotebookManageRequiredError as exc:
                raise ShareRequesterUnauthorizedError(notebook_id) from exc
            connection.execute(
                "INSERT INTO notebook_grants "
                "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                "VALUES (%s,%s, 'group', %s, 'viewer', %s, %s) "
                "ON CONFLICT ON CONSTRAINT uq_notebook_grants_principal DO NOTHING",
                (self.new_id("gnt"), notebook_id, group_id, decided_by, stamp),
            )
            connection.execute(
                "UPDATE notebook_share_requests "
                "SET status='approved', decided_by=%s, decided_at=%s WHERE id=%s",
                (decided_by, stamp, request_id),
            )
            # 终态投影必须在**同一写事务内**读回:放到事务外的独立连接里读,一个并发的
            # `delete_group` 可以在提交与这次读之间 CASCADE 删掉本行,`out` 变 None →
            # `_share_request_row(None)` 崩(真 PG 并发实测复现)。事务内这行必然存在。
            out = connection.execute(
                _SHARE_REQUEST_SELECT + "WHERE sr.id=%s", (request_id,)
            ).fetchone()
        return self._share_request_row(out)

    def reject_share_request(
        self,
        group_id: str,
        request_id: str,
        *,
        decided_by: str,
        decided_by_is_system_admin: bool = False,
    ) -> "dict | None":
        """见 SQLite 侧同名方法:审批资格复核**先于** UPDATE 发出——驳回同样是终态决定,
        被降级的人不该还能做。

        ⚠ 锁序与 `approve_share_request` **必须一致**:先 `groups` 行 `FOR SHARE`,再复核
        资格。少了这把锁,资格复核自己仍有 TOCTOU 窗口——一个并发的降级事务可以在复核读到
        `admin` 之后、`UPDATE` 之前提交,于是被降级的人照样把别人的申请判死(codex #519
        R3)。approve 拿了锁而 reject 没拿,等于同一条纪律只兑现了一半。组已被并发删掉 →
        `GroupNotFoundError` → 落 `None`(路由 404),与 approve 同款收敛。
        """
        stamp = self.now()
        with self.database.write() as connection:
            try:
                self._lock_group_on(connection, group_id, mode="SHARE")
            except GroupNotFoundError:
                return None
            self._require_share_decider_on(
                connection, group_id, decided_by, decided_by_is_system_admin
            )
            cursor = connection.execute(
                "UPDATE notebook_share_requests "
                "SET status='rejected', decided_by=%s, decided_at=%s "
                "WHERE id=%s AND group_id=%s AND status='pending'",
                (decided_by, stamp, request_id, group_id),
            )
            if int(cursor.rowcount or 0) == 0:
                return None
            # 终态投影在事务内读回(理由同 approve:事务外读会被并发删撞成 None)。
            out = connection.execute(
                _SHARE_REQUEST_SELECT + "WHERE sr.id=%s", (request_id,)
            ).fetchone()
        return self._share_request_row(out)

    def delete_share_request(
        self, notebook_id: str, request_id: str, requester_id: str
    ) -> str:
        """见 SQLite 侧同名方法的完整论证(三列 WHERE 各挡一种越权)。"""
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT status FROM notebook_share_requests "
                "WHERE id=%s AND notebook_id=%s AND requested_by=%s FOR UPDATE",
                (request_id, notebook_id, requester_id),
            ).fetchone()
            if row is None:
                return "not_found"
            # 见 SQLite 侧同名方法:放行是**正向** `== 'pending'`,不用 `!= 'pending'`;
            # `requested_by` 一起验,撤回只属于申请者本人(codex #519 R1 P1)。
            if row["status"] == "pending":
                connection.execute(
                    "DELETE FROM notebook_share_requests "
                    "WHERE id=%s AND notebook_id=%s AND requested_by=%s",
                    (request_id, notebook_id, requester_id),
                )
                return "deleted"
            raise ShareRequestNotPendingError(request_id)
