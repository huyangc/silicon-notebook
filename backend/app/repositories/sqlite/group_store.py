"""SQLite 行持久化:群组、组成员、笔记本授权边(群组知识共享 P1-T3)。

**只管行**。谁能建哪一类组、谁能改成员、双重条件的授权边创建策略,全部留在
`app/api/group_routes.py`;本模块只负责「把这几张表读对写对」,外加两件必须在
**同一个写事务**里完成、因而没法留给路由的事:

1. **建组即建创建者的组管理员成员行**——分成两个事务就会存在一个「有组无管理员」
   的窗口,那种组谁也管不了(改名/加人/删组全部要组管理员)。
2. **最后一名组管理员保护**——检查与写入必须在同一事务里,否则两个并发的「把最后
   一个管理员降级/移除」请求可以各自看到 count==1 然后都成功,留下一个无人可管的组。
   SQLite 侧由进程级写锁天然串行(见 `SqliteDatabase.write` 的说明);PG 侧另在
   同一事务里对 `groups` 行加 `FOR UPDATE`,两边语义对齐。

删组同样是一个事务:`group_members` 靠 FK 级联(连接恒开 `PRAGMA foreign_keys=ON`)
消失,而 `notebook_grants` 指向本组的行**必须显式删掉**——`principal_id` 是刻意
不带 FK 的多态列(见 v49 迁移的说明),级联够不着它。谓词侧不清理也不会越权(join
不到组成员就判假,见 `test_deleting_the_group_cascades_membership_and_revokes_access`),
但共享管理列表会永远挂着一条指向已不存在的组的边。

⚠ 本模块**不含**任何授权判定谓词:「谁能读这个 notebook」的唯一定义点是
`access_sql.py`,这里只做 CRUD 与按主体 id 的直查。唯一一处形似判定的是
`granted_notebook_rows`(在 `query_store.py`,不在这里)——那是**列表投影**而不是
判定,与 `joined_notebook_rows` 同一类,理由写在它自己的 docstring 里。

**双后端同修**:`postgres/group_store.py` 是本文件的镜像,方法名、边界与返回形状
必须逐条对应。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from app.repositories.group_rows import fold_shared_notebooks
from app.repositories.ports import GroupGrantAlreadyExists, LastGroupAdminError
from app.repositories.sqlite.database import SqliteDatabase


# 一条授权边 + 它的群组主体名字(仅群组主体有值)。LEFT JOIN 的 ON 条件里必须带
# `principal_type IN (...)`:`principal_id` 是多态列,不限定主体类型就会拿一个 user
# id 去撞 `groups.id`。它不判「谁能读」——只给已经存在的行贴个名字。
_GRANT_SELECT = (
    "SELECT ng.*, g.name AS _group_name, g.kind AS _group_kind "
    "FROM notebook_grants ng "
    "LEFT JOIN groups g ON g.id=ng.principal_id "
    "AND ng.principal_type IN ('group','group_admins') "
)


class GroupStore:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

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
            "created_at": row["created_at"],
        }

    @staticmethod
    def _member_row(row) -> dict:
        return {
            "id": row["user_id"],
            "username": row["username"] or row["user_id"],
            "display_name": row["display_name"] or "",
            "role": row["role"],
            "added_at": row["added_at"],
        }

    @staticmethod
    def _grant_row(row) -> dict:
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "principal_type": row["principal_type"],
            "principal_id": row["principal_id"],
            "role": row["role"],
            "principal_name": row["_group_name"] or "",
            "principal_kind": row["_group_kind"] or "",
            "created_at": row["created_at"],
        }

    # -------------------------------------------------------------- 群组
    def create_group(
        self, *, name: str, kind: str, description: str, created_by: str
    ) -> dict:
        """建组 + 把创建者写成组管理员,一个写事务。

        id 用 `new_id`(前缀 + 完整 128 位 uuid4 十六进制)。跨部署随机不撞车是
        `scripts/merge_dbs.py` 的 GLOBAL_UNION 语义要求的(已定裁决 1c):合库时
        群组行按 id 取并集,两个部署各自生成的 id 撞上就会把两个不相干的组合成
        一个,连同它们的成员与授权边。
        """
        group_id = self.new_id("grp")
        stamp = self.now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO groups "
                "(id,name,kind,description,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (group_id, name, kind, description, created_by, stamp, stamp),
            )
            db.execute(
                "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
                "VALUES (?,?,'admin',?,?)",
                (group_id, created_by, stamp, created_by),
            )
        group = self.get_group(group_id, user_id=created_by)
        assert group is not None  # 同一进程刚提交
        return group

    def get_group(self, group_id: str, *, user_id: str = "") -> "dict | None":
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
            if row is None:
                return None
            count = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=?",
                    (group_id,),
                ).fetchone()["c"]
            )
            role = self._role_on(db, group_id, user_id) if user_id else None
        return self._group_row(row, my_role=role or "", member_count=count)

    def user_group_role(self, group_id: str, user_id: str) -> "str | None":
        """该用户在该组里的角色,不是成员则 None。组内权限判定的唯一入口。"""
        with self.database.connect() as db:
            return self._role_on(db, group_id, user_id)

    @staticmethod
    def _role_on(db: sqlite3.Connection, group_id: str, user_id: str) -> "str | None":
        row = db.execute(
            "SELECT role FROM group_members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        return row["role"] if row else None

    def list_groups_for_user(self, user_id: str) -> list[dict]:
        """我所在的组。成员数用一次相关子查询算,不做逐组 N+1。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT g.*, gm.role AS _my_role, "
                "(SELECT COUNT(*) FROM group_members c WHERE c.group_id=g.id) AS _members "
                "FROM group_members gm JOIN groups g ON g.id=gm.group_id "
                "WHERE gm.user_id=? ORDER BY g.created_at ASC, g.id ASC",
                (user_id,),
            ).fetchall()
        return [
            self._group_row(
                row, my_role=row["_my_role"], member_count=int(row["_members"])
            )
            for row in rows
        ]

    def list_all_groups(self, *, user_id: str = "") -> list[dict]:
        """全部群组(系统管理员的全局管理面)。`my_role` 仍按请求者本人算。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT g.*, "
                "(SELECT role FROM group_members m "
                "WHERE m.group_id=g.id AND m.user_id=?) AS _my_role, "
                "(SELECT COUNT(*) FROM group_members c WHERE c.group_id=g.id) AS _members "
                "FROM groups g ORDER BY g.created_at ASC, g.id ASC",
                (user_id,),
            ).fetchall()
        return [
            self._group_row(
                row, my_role=row["_my_role"] or "", member_count=int(row["_members"])
            )
            for row in rows
        ]

    def list_members(self, group_id: str) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT gm.user_id AS user_id, gm.role AS role, gm.added_at AS added_at, "
                "u.username AS username, u.display_name AS display_name "
                "FROM group_members gm LEFT JOIN users u ON u.id=gm.user_id "
                "WHERE gm.group_id=? ORDER BY gm.added_at ASC, gm.user_id ASC",
                (group_id,),
            ).fetchall()
        return [self._member_row(row) for row in rows]

    def update_group(
        self, group_id: str, *, name: "str | None" = None, description: "str | None" = None
    ) -> bool:
        """改名/改说明。两个字段都省略时是一次合法 no-op(仍要如实回答组在不在)。"""
        assignments: list[str] = []
        params: list[object] = []
        if name is not None:
            assignments.append("name=?")
            params.append(name)
        if description is not None:
            assignments.append("description=?")
            params.append(description)
        if not assignments:
            with self.database.connect() as db:
                return (
                    db.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone()
                    is not None
                )
        assignments.append("updated_at=?")
        params.extend([self.now(), group_id])
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE groups SET {', '.join(assignments)} WHERE id=?", params
            )
        return cursor.rowcount > 0

    def delete_group(self, group_id: str) -> bool:
        """删组 + 清掉指向本组的全部授权边,一个写事务(已定裁决 3)。

        `group_members` 由 FK 级联带走(连接恒开 `PRAGMA foreign_keys = ON`);
        `notebook_grants` 的 `principal_id` 无 FK,必须显式删。
        """
        with self.database.write() as db:
            db.execute(
                "DELETE FROM notebook_grants "
                "WHERE principal_type IN ('group','group_admins') AND principal_id=?",
                (group_id,),
            )
            cursor = db.execute("DELETE FROM groups WHERE id=?", (group_id,))
        return cursor.rowcount > 0

    # -------------------------------------------------------------- 成员
    @staticmethod
    def _admin_count_on(db: sqlite3.Connection, group_id: str) -> int:
        return int(
            db.execute(
                "SELECT COUNT(*) AS c FROM group_members "
                "WHERE group_id=? AND role='admin'",
                (group_id,),
            ).fetchone()["c"]
        )

    def upsert_member(
        self, group_id: str, user_id: str, *, role: str, added_by: str
    ) -> str:
        """加人 / 改角色。返回 ``"added"`` 或 ``"updated"``。

        把最后一名组管理员降级 → `LastGroupAdminError`。判定与写入同一事务,并发的
        两次降级不可能都通过。
        """
        with self.database.write() as db:
            current = self._role_on(db, group_id, user_id)
            if (
                current == "admin"
                and role != "admin"
                and self._admin_count_on(db, group_id) <= 1
            ):
                raise LastGroupAdminError(group_id)
            if current is None:
                db.execute(
                    "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
                    "VALUES (?,?,?,?,?)",
                    (group_id, user_id, role, self.now(), added_by),
                )
                return "added"
            db.execute(
                "UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",
                (role, group_id, user_id),
            )
        return "updated"

    def remove_member(self, group_id: str, user_id: str) -> bool:
        """移除成员(自助退出走同一条路径)。移除最后一名组管理员 → 报错。"""
        with self.database.write() as db:
            current = self._role_on(db, group_id, user_id)
            if current is None:
                return False
            if current == "admin" and self._admin_count_on(db, group_id) <= 1:
                raise LastGroupAdminError(group_id)
            db.execute(
                "DELETE FROM group_members WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
        return True

    def find_user_by_username(self, username: str) -> "dict | None":
        """按用户名**精确**查一个用户。

        住在群组 store 而不是身份 store:唯一消费者是「组管理员加人」这条流程
        (本模块的成员清单本来就要读 `users`),而身份 store 的 Protocol 同时被
        facade 的冻结兼容面登记,为一个只服务群组的读查询去动那张表不划算。
        """
        return self._user_lookup("SELECT id, username, display_name FROM users WHERE username=?", username)

    def find_user_by_id(self, user_id: str) -> "dict | None":
        """按 id 查一个用户。

        加人前必须先问这一句:`group_members.user_id` 有 FK → `users.id`,直接插一个
        不存在的 id 会抛 `IntegrityError`,而那是 500 —— 用户看到的是「服务器出错」
        而不是「没有这个用户」。
        """
        return self._user_lookup("SELECT id, username, display_name FROM users WHERE id=?", user_id)

    def _user_lookup(self, sql: str, value: str) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(sql, (value,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"] or row["id"],
            "display_name": row["display_name"] or "",
        }

    # ------------------------------------------------------------ 授权边
    def list_grants(self, notebook_id: str) -> list[dict]:
        """该库全部授权边(四类主体如实返回,群组主体顺带解析出组名/分类)。"""
        with self.database.connect() as db:
            rows = db.execute(
                _GRANT_SELECT
                + "WHERE ng.notebook_id=? ORDER BY ng.created_at ASC, ng.id ASC",
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
    ) -> dict:
        """新建一条授权边。同库同主体已有边 → `GroupGrantAlreadyExists`。

        重复刻意**不**做幂等复用而是明确报错:两条边的 `role` 可以不同,静默返回既有
        行会让「我改成了管理」与「库里其实还是只读」这两件事长得一模一样。
        """
        grant_id = self.new_id("gnt")
        try:
            with self.database.write() as db:
                db.execute(
                    "INSERT INTO notebook_grants "
                    "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
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
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise GroupGrantAlreadyExists(notebook_id, principal_id) from exc
            raise
        with self.database.connect() as db:
            row = db.execute(_GRANT_SELECT + "WHERE ng.id=?", (grant_id,)).fetchone()
        return self._grant_row(row)

    def grant_row(self, notebook_id: str, grant_id: str) -> "dict | None":
        """按 id 取一条边,**同时**要求它属于这个 notebook。

        撤销端点的权限是挂在 notebook 上的,所以 id 必须与 notebook 一起验——
        只按 grant_id 查/删,等于让「我有一本自己的库的管理权」变成「我能删任何库上
        的授权边」。
        """
        with self.database.connect() as db:
            row = db.execute(
                _GRANT_SELECT + "WHERE ng.id=? AND ng.notebook_id=?",
                (grant_id, notebook_id),
            ).fetchone()
        return self._grant_row(row) if row else None

    def delete_grant(self, notebook_id: str, grant_id: str) -> bool:
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM notebook_grants WHERE id=? AND notebook_id=?",
                (grant_id, notebook_id),
            )
        return cursor.rowcount > 0

    def list_group_shared_notebooks(self, group_id: str) -> list[dict]:
        """共享给本组的笔记本清单(组管理员视角)。同库两条边折成一项。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT ng.notebook_id AS notebook_id, ng.role AS role, "
                "nb.name AS name, u.username AS owner_username "
                "FROM notebook_grants ng "
                "JOIN notebooks nb ON nb.id=ng.notebook_id "
                "LEFT JOIN users u ON u.id=nb.created_by "
                "WHERE ng.principal_type IN ('group','group_admins') AND ng.principal_id=? "
                "AND nb.status != 'copying' "
                "ORDER BY nb.created_at ASC, nb.id ASC, ng.id ASC",
                (group_id,),
            ).fetchall()
        return fold_shared_notebooks(rows)

    def delete_group_grants_for_notebook(self, group_id: str, notebook_id: str) -> int:
        """撤销不对称的组维度入口:删掉这本库上指向本组的**全部**边。"""
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM notebook_grants "
                "WHERE notebook_id=? AND principal_id=? "
                "AND principal_type IN ('group','group_admins')",
                (notebook_id, group_id),
            )
        return cursor.rowcount
