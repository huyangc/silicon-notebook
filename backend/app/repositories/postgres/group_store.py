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

from app.repositories.group_rows import fold_shared_notebooks, resolve_grant_principal
from app.repositories.ports import (
    GroupAdminRequiredError,
    GroupGrantAlreadyExists,
    GroupNotFoundError,
    LastGroupAdminError,
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

    # -------------------------------------------------------------- 群组
    def create_group(
        self, *, name: str, kind: str, description: str, created_by: str
    ) -> dict:
        group_id = self.new_id("grp")
        stamp = self.now()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO groups "
                "(id,name,kind,description,created_by,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (group_id, name, kind, description, created_by, stamp, stamp),
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

    def delete_group(self, group_id: str) -> bool:
        with self.database.write() as connection:
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
            if current == "admin" and self._admin_count_on(connection, group_id) <= 1:
                raise LastGroupAdminError(group_id)
            connection.execute(
                "DELETE FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id),
            )
        return True

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
        """
        grant_id = self.new_id("gnt")
        try:
            with self.database.write() as connection:
                self._lock_group_on(connection, principal_id, mode="SHARE")
                if self._role_on(connection, principal_id, admin_user_id) != "admin":
                    raise GroupAdminRequiredError(principal_id)
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

    def list_group_shared_notebooks(self, group_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT ng.notebook_id AS notebook_id, ng.role AS role, "
                "nb.name AS name, u.username AS owner_username "
                "FROM notebook_grants ng "
                "JOIN notebooks nb ON nb.id=ng.notebook_id "
                "LEFT JOIN users u ON u.id=nb.created_by "
                "WHERE ng.principal_type IN ('group','group_admins') "
                "AND ng.principal_id=%s AND nb.status != 'copying' "
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
