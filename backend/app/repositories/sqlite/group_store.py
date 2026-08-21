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
from app.repositories.sqlite.access_sql import (
    ADMIN_GRANT_PROBE_SQL,
    admin_grant_probe_params,
    read_access_clause,
    read_access_params,
)
from app.repositories.sqlite.database import SqliteDatabase


# 一条授权边 + 它的群组主体名字(仅群组主体有值)。LEFT JOIN 的 ON 条件里带
# `principal_type IN (...)` 是**纵深防御**:今天的 id 都带前缀(`user-…` / `grp-…`),
# 一个 user id 撞不上 `groups.id`,所以不限定主体类型在今天也拿不到错的行;但那条
# 「不会撞」的保证来自 `_new_id` 的前缀约定,而不是这条 SQL 自己——`principal_id` 是
# 一列没有外键的多态引用,谁也拦不住将来某个写入口塞进裸 id。限定住主体类型之后,
# 这条 JOIN 的正确性不再依赖别处的命名习惯。它不判「谁能读」,只给已经存在的行贴名字。
_GRANT_SELECT = (
    "SELECT ng.*, g.name AS _group_name, g.kind AS _group_kind "
    "FROM notebook_grants ng "
    "LEFT JOIN groups g ON g.id=ng.principal_id "
    "AND ng.principal_type IN ('group','group_admins') "
)

# SQLite 的 UNIQUE 冲突只能从异常文本分类。判据钉到**具体那条约束**的列上
# (`UNIQUE constraint failed: notebook_grants.notebook_id, …`),与 PG 侧按
# `constraint_name == "uq_notebook_grants_principal"` 判是同一口径:只认「同库同主体
# 已有边」这一种冲突。裸 `"UNIQUE" in message` 会把主键冲突之类的别的约束也吞进来,
# 报成一句「已经共享过了」——那是把一个真 bug 说成用户操作重复。
_GRANT_UNIQUE_MARKERS = ("UNIQUE constraint failed", "notebook_grants.notebook_id")

# 同款判据,钉到 `uq_share_requests_one_pending` 的首列:同库同组已有一条**待审批**
# 申请。撞它不是失败而是**幂等**(返回既有 pending 行,见 `create_share_request`),
# 所以判据要窄——PK 冲突(`notebook_share_requests.id`)不能落进这个分支,否则真 bug
# 会被当成「重复提交」。
_SHARE_REQUEST_PENDING_UNIQUE_MARKERS = (
    "UNIQUE constraint failed",
    "notebook_share_requests.notebook_id",
)

# 一条共享申请 + 它的库名/组名/申请者用户名(仅供展示)。三个 LEFT JOIN 都恒能解析:
# `notebook_id`/`group_id` 是 ON DELETE CASCADE 外键——库或组一旦删掉,申请行随之消失,
# 所以「行还在」就意味着两个父行都在;`requested_by` 无 ON DELETE 子句但用户不会凭空
# 消失。LEFT JOIN 只是纵深防御,不改变行数。
_SHARE_REQUEST_SELECT = (
    "SELECT sr.*, nb.name AS _notebook_name, g.name AS _group_name, "
    "u.username AS _requested_by_username "
    "FROM notebook_share_requests sr "
    "LEFT JOIN notebooks nb ON nb.id = sr.notebook_id "
    "LEFT JOIN groups g ON g.id = sr.group_id "
    "LEFT JOIN users u ON u.id = sr.requested_by "
)


# 「我发起的待审批申请」专用的投影:两个展示标签**各自**按当前权限决定给不给
# (codex #519 R12 P2)。
#
# 为什么不能直接用 `_SHARE_REQUEST_SELECT`:那份无条件带出 `notebooks.name` 与
# `groups.name`。申请人在提交之后可能失去这本库的权限、或被移出目标组,而这条清单是
# **他失权之后仍然够得着**的(裁决 P2-7 的另一半,正是这么设计的)。于是它就成了一条
# **活的**信息通道:对方改名,清单持续把**新**名字送给他。他知道的只是**提交那一刻**的
# 名字——这是时间维度上的越权,与「跨库资产 no-store」「取消挂载即 404」「公开页每次
# 实时复核读权」是同一条线。
#
# 两个标签**分别**判,不是一起:笔记本那半按**读权**判,群组那半按**是否仍是成员**判。
# 一起判的话,只失去一半的人两个名字都会消失,多条待审批就都长成「知识库 → 群组」,
# 根本分不清该撤哪条。两半同时失去才两个都空,那是罕见情形。
#
# ⚠ 读权那半**必须**嵌 `access_sql.read_access_clause()`,绝不能在这里手抄一份判定
# ——那是 P0 建唯一定义点时立的规矩。群组成员资格那半留在本模块自己写:`access_sql` 的
# 模块 docstring 明确把「组成员关系的 CRUD 与列表查询」划在唯一定义点之外(本模块的
# `_role_on` 就是同一类读),它不是 notebook 授权谓词。
#
# 全部在**同一条**查询里用条件表达式做完,不逐行回查(N+1)。
_MY_PENDING_SHARE_REQUEST_SELECT = (
    "SELECT sr.*, "
    "CASE WHEN " + read_access_clause("nb") + " THEN nb.name ELSE '' END AS _notebook_name, "
    "CASE WHEN EXISTS (SELECT 1 FROM group_members gmv "
    "WHERE gmv.group_id = sr.group_id AND gmv.user_id = ?) "
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
            "owner_id": row["owner_id"],
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
            "created_at": row["created_at"],
        }

    @staticmethod
    def _share_request_row(row) -> dict:
        # SQLite 的 `decided_at` 已是 TEXT(None 或 ISO);断言直接吃**原始**值,
        # 好让「pending 行被手滑写成 '' 」当场被顶回(v50 迁移点名的 poison 前置防线)。
        status = row["status"]
        decided_at = row["decided_at"]
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
                "(id,name,kind,description,created_by,owner_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (group_id, name, kind, description, created_by, created_by, stamp, stamp),
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

    def transfer_group_owner(
        self,
        group_id: str,
        *,
        new_owner_id: str,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> dict:
        """Transfer owner and promote the target in one serialized write."""
        with self.database.write() as db:
            self._require_group_on(db, group_id)
            row = db.execute(
                "SELECT owner_id FROM groups WHERE id=?", (group_id,)
            ).fetchone()
            if not actor_is_system_admin and row["owner_id"] != actor_id:
                raise GroupOwnerRequiredError(group_id)
            if self._role_on(db, group_id, new_owner_id) is None:
                raise GroupOwnerTransferTargetError(new_owner_id)
            db.execute(
                "UPDATE group_members SET role='admin' "
                "WHERE group_id=? AND user_id=?",
                (group_id, new_owner_id),
            )
            db.execute(
                "UPDATE groups SET owner_id=?, updated_at=? WHERE id=?",
                (new_owner_id, self.now(), group_id),
            )
            projected = db.execute(
                "SELECT * FROM groups WHERE id=?", (group_id,)
            ).fetchone()
            member_count = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=?",
                    (group_id,),
                ).fetchone()["c"]
            )
            actor_role = self._role_on(db, group_id, actor_id)
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

        `group_members` 由 FK 级联带走(连接恒开 `PRAGMA foreign_keys = ON`);
        `notebook_grants` 的 `principal_id` 无 FK,必须显式删。

        ⚠ 这里**不**像 PG 侧那样先对 `groups` 行取锁,是刻意的:`SqliteDatabase.write()`
        是进程级写锁,同一时刻只有一个写事务在跑,并发的 `create_grant` 插不进「清边」
        与「删组」之间。PG 没有那把锁,所以那边必须在事务开头显式 `FOR UPDATE`,否则
        会留下孤儿授权边(理由完整写在 `postgres/group_store.py::delete_group`)。
        Owner 路径仍在这个串行写事务内复核生效中的 owner；这不是为了加锁，
        而是关闭路由前置鉴权与删除之间的所有权转让窗口。没有传 actor 的内部维护
        调用保留原来的无鉴权 store 契约。
        """
        with self.database.write() as db:
            if actor_id is not None:
                row = db.execute(
                    "SELECT owner_id FROM groups WHERE id=?", (group_id,)
                ).fetchone()
                if row is None:
                    return False
                if not actor_is_system_admin and row["owner_id"] != actor_id:
                    raise GroupOwnerRequiredError(group_id)
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

    @staticmethod
    def _require_group_on(db: sqlite3.Connection, group_id: str) -> None:
        """在**当前写事务里**复核这个组还在,不在就 `GroupNotFoundError`。

        PG 侧对应的是 `_lock_group_on` 的 `FOR UPDATE`。SQLite 不需要行锁(进程级写
        锁已经把所有写事务串起来了),但**存在性仍要在事务内查**:路由层那次前置
        检查与这里之间,一个并发的删组请求可以整个跑完。少了这条,`INSERT INTO
        group_members` 会撞 `group_id` 的外键约束抛 `sqlite3.IntegrityError`——用户
        拿到的是 500,而正确答案是 404。
        """
        if db.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone() is None:
            raise GroupNotFoundError(group_id)

    @staticmethod
    def _require_notebook_on(db: sqlite3.Connection, notebook_id: str) -> None:
        """在**当前写事务里**复核这本笔记本还在,不在就 `NotebookNotFoundError`。

        `_require_group_on` 的**同类兄弟**,只是换了另一个外键父行:一次插入有几个外键,
        就有几个父行可能在守卫通过之后被并发删掉,只堵其中一个不算堵住(codex #519 R7)。
        少了这条,`INSERT INTO notebook_share_requests` 会撞 `notebook_id` 的外键抛
        `sqlite3.IntegrityError`——用户拿到的是 500,而正确答案是 404。

        与 `_require_group_on` 一样,SQLite 侧**不需要**行锁:`SqliteDatabase.write()` 是
        进程级写锁,同一时刻只有一个写事务在跑,并发的删库事务插不进「复核」与「插入」
        之间;但**存在性仍要在事务内查**,因为能力守卫那次查询与本事务之间,一个完整的
        删库请求可以整个跑完。PG 侧对应的是 `_lock_notebook_on` 的 `FOR KEY SHARE`。

        ⚠ 这里查的是**存在性**,不是权限。权限那条轴是 `_require_notebook_manage_on`,
        判据完全不同(裁决 P2-8:只有写 `notebook_grants` 这类授予他人访问权的路径才做
        事务内权限复检)。别把两者合并——库已经不存在时,「他还有没有管理权」不是一个
        有意义的问题,而这条要回答的恰恰是那个不成立的前提。
        """
        if (
            db.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
            is None
        ):
            raise NotebookNotFoundError(notebook_id)

    def upsert_member(
        self, group_id: str, user_id: str, *, role: str, added_by: str
    ) -> str:
        """加人 / 改角色。返回 ``"added"`` 或 ``"updated"``。

        把最后一名组管理员降级 → `LastGroupAdminError`;组已被并发删掉 →
        `GroupNotFoundError`。两条判定都在写事务内,并发的两次降级不可能都通过。
        """
        with self.database.write() as db:
            self._require_group_on(db, group_id)
            current = self._role_on(db, group_id, user_id)
            owner = db.execute(
                "SELECT owner_id FROM groups WHERE id=?", (group_id,)
            ).fetchone()["owner_id"]
            if user_id == owner and current is not None and role != "admin":
                raise GroupOwnerProtectedError(group_id)
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
            self._require_group_on(db, group_id)
            current = self._role_on(db, group_id, user_id)
            if current is None:
                return False
            owner = db.execute(
                "SELECT owner_id FROM groups WHERE id=?", (group_id,)
            ).fetchone()["owner_id"]
            if user_id == owner:
                raise GroupOwnerProtectedError(group_id)
            if current == "admin" and self._admin_count_on(db, group_id) <= 1:
                raise LastGroupAdminError(group_id)
            db.execute(
                "DELETE FROM group_members WHERE group_id=? AND user_id=?",
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
        with self.database.write() as db:
            self._require_group_on(db, group_id)
            if (
                not actor_is_system_admin
                and self._role_on(db, group_id, actor_id) != "admin"
            ):
                raise GroupAdminRequiredError(group_id)
            row = db.execute(
                "SELECT invite_token,invite_created_at FROM groups WHERE id=?",
                (group_id,),
            ).fetchone()
        token = row["invite_token"] or ""
        return {
            "active": bool(token),
            "token": token,
            "created_at": row["invite_created_at"] if token else None,
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
        """Publish/reuse the capability under the same serialized group write."""
        with self.database.write() as db:
            self._require_group_on(db, group_id)
            if (
                not actor_is_system_admin
                and self._role_on(db, group_id, actor_id) != "admin"
            ):
                raise GroupAdminRequiredError(group_id)
            current = db.execute(
                "SELECT invite_token,invite_created_at FROM groups WHERE id=?",
                (group_id,),
            ).fetchone()
            chosen = (current["invite_token"] or "") if not rotate else ""
            if chosen:
                return {
                    "active": True,
                    "token": chosen,
                    "created_at": current["invite_created_at"],
                }
            stamp = self.now()
            db.execute(
                "UPDATE groups SET invite_token=?,invite_created_at=?,"
                "invite_created_by=?,updated_at=? WHERE id=?",
                (token, stamp, actor_id, stamp, group_id),
            )
        return {"active": True, "token": token, "created_at": stamp}

    def revoke_invite(
        self,
        group_id: str,
        *,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> bool:
        with self.database.write() as db:
            self._require_group_on(db, group_id)
            if (
                not actor_is_system_admin
                and self._role_on(db, group_id, actor_id) != "admin"
            ):
                raise GroupAdminRequiredError(group_id)
            db.execute(
                "UPDATE groups SET invite_token=NULL,invite_created_at=NULL,"
                "invite_created_by=NULL,updated_at=? WHERE id=?",
                (self.now(), group_id),
            )
        return True

    def join_by_invite(self, token: str, *, user_id: str) -> "dict | None":
        """Resolve and join in one write so revocation cannot race the insert."""
        with self.database.write() as db:
            row = db.execute(
                "SELECT * FROM groups WHERE invite_token=?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            current = self._role_on(db, row["id"], user_id)
            if current is None:
                db.execute(
                    "INSERT INTO group_members "
                    "(group_id,user_id,role,added_at,added_by) "
                    "VALUES (?,?,'member',?,?)",
                    (row["id"], user_id, self.now(), user_id),
                )
                current = "member"
            member_count = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=?",
                    (row["id"],),
                ).fetchone()["c"]
            )
            return self._group_row(
                row, my_role=current, member_count=member_count
            )

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
        admin_user_id: str,
    ) -> dict:
        """新建一条群组授权边,并在**同一个写事务**里复核双重条件的群组那一半。

        三种失败:同库同主体已有边 → `GroupGrantAlreadyExists`;组已不存在 →
        `GroupNotFoundError`;`admin_user_id` 已不是它的组管理员 →
        `GroupAdminRequiredError`。

        为什么复核必须在事务里:路由层查过一次「组在不在、你是不是它的管理员」,
        但那次查询与这次插入之间,组可以被删、发起者可以被移出或降级——而这条边一旦
        落库就**立刻**给整组人读权。前置查询只用来给出友好文案,授权判定以这一次为准。

        重复边刻意**不**做幂等复用而是明确报错:两条边的 `role` 可以不同,静默返回既有
        行会让「我改成了管理」与「库里其实还是只读」这两件事长得一模一样。

        **笔记本那个外键父行也复核一次**(`_require_notebook_on`,codex #519 R7 存疑项收口)。
        ⚠ 这一侧的动机与 PG 不同,别读成同一件事:SQLite **本来就不会 500**——库被并发删掉
        时,`_require_notebook_manage_on` 的 owner 半查不到行、授权边半的行也被外键级联带走,
        于是它抛 `NotebookManageRequiredError`,根本走不到 INSERT(已实测)。补这一条是为了
        **两个后端的响应对等**:PG 侧的 `_lock_notebook_on` 让那种情形答 404「笔记本不存在」,
        这边不补就会答 403「你已不再拥有这本笔记本的管理权」——一句在库已经不存在时纯属误导
        的话,还让同一个场景在两个后端上长得不一样。
        """
        grant_id = self.new_id("gnt")
        try:
            with self.database.write() as db:
                self._require_group_on(db, principal_id)
                if self._role_on(db, principal_id, admin_user_id) != "admin":
                    raise GroupAdminRequiredError(principal_id)
                # 笔记本维度两条,**存在性在前、权限在后**(与 PG 侧逐条对应):库都不在
                # 了,「他还有没有管理权」不是一个有意义的问题。这一侧不修 500(见 docstring:
                # 这边本来就不会 500),修的是**答什么**——不补就会答一句「你已不再拥有这本
                # 笔记本的管理权」,而真相是库没了。
                self._require_notebook_on(db, notebook_id)
                # **笔记本侧**的那一半同样要在事务内复核(codex #519 R6 P1):
                # 能力守卫放行之后、本事务开始之前,库主可以撤掉发起人的管理边;
                # 少了这一次复核,失权者仍能发出一条**新的**授权边(甚至给另一个
                # 组),把访问权继续散出去。判据见 `api/deps.py` 那条裁决:凡是写
                # `notebook_grants` 的路径都必须事务内复检并锁住发起人的权限。
                self._require_notebook_manage_on(
                    db, notebook_id, admin_user_id
                )
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
            message = str(exc)
            if all(marker in message for marker in _GRANT_UNIQUE_MARKERS):
                raise GroupGrantAlreadyExists(notebook_id, principal_id) from exc
            raise
        with self.database.connect() as db:
            row = db.execute(_GRANT_SELECT + "WHERE ng.id=?", (grant_id,)).fetchone()
        return self._grant_row(row)

    def delete_grant(self, notebook_id: str, grant_id: str) -> bool:
        """按 id 删一条边,**同时**要求它属于这个 notebook。

        两列一起验:撤销端点的权限挂在 notebook 上,只按 grant_id 删等于让「我有一本
        自己的库的管理权」变成「我能删任何库上的授权边」。
        """
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM notebook_grants WHERE id=? AND notebook_id=?",
                (grant_id, notebook_id),
            )
        return cursor.rowcount > 0

    def list_group_shared_notebooks(
        self, group_id: str, *, include_admin_only: bool = True
    ) -> list[dict]:
        """Return the caller-visible group inventory; fold two edges per notebook."""
        principal_clause = (
            "ng.principal_type IN ('group','group_admins')"
            if include_admin_only
            else "ng.principal_type='group'"
        )
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT ng.notebook_id AS notebook_id, ng.role AS role, "
                "nb.name AS name, u.username AS owner_username "
                "FROM notebook_grants ng "
                "JOIN notebooks nb ON nb.id=ng.notebook_id "
                "LEFT JOIN users u ON u.id=nb.created_by "
                f"WHERE {principal_clause} AND ng.principal_id=? "
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

    # -------------------------------------------------------- 共享申请(P2-T3)
    def _pending_share_request(
        self, notebook_id: str, group_id: str
    ) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(
                _SHARE_REQUEST_SELECT
                + "WHERE sr.notebook_id=? AND sr.group_id=? AND sr.status='pending'",
                (notebook_id, group_id),
            ).fetchone()
        return self._share_request_row(row) if row is not None else None

    def create_share_request(
        self, notebook_id: str, *, group_id: str, requested_by: str
    ) -> dict:
        """新建一条共享申请。撞 `uq_share_requests_one_pending` **返回既有 pending 行**
        (幂等,不报错——刷新页面重复提交是常见操作)。

        `status` 恒 `'pending'`、`decided_at` **留 NULL**(不写列,取默认;绝不写 `''`)。
        写事务里先复核**两个外键父行**都还在——`_require_group_on`(组)与
        `_require_notebook_on`(笔记本)。少了任意一条,那个父行被并发删掉时插入会撞对应的
        外键抛 `IntegrityError`(用户拿到 500),而正确答案都是 404。这不是「顺手多查一次」:
        一次插入有几个外键,守卫与写事务之间就有几个父行可能消失,只堵组那一个等于把笔记本
        那一个的 500 留在原地(codex #519 R7 P2)。同一写锁下两条都复核过后,唯一还可能的
        `IntegrityError` 就是那条 pending 唯一索引冲突。

        ⚠ **成员资格也必须在这个事务里复核**(codex #519 R2 P2-1):路由查过一次
        「`requested_by` 在不在这个组里」,但「被移出组」可以发生在那次查询与插入之间。
        少了这一条,一个刚被移出组的人仍能落一条**非成员**的待审批申请,组管理员的审核
        队列里会出现一个已经不属于本组的人。判据是「有成员行」而不是「是组管理员」——
        申请这条路本来就是给普通成员走的。与「最后一名组管理员保护」「`create_grant` 的
        双重条件」同一手法:承重的复核在写事务内,路由那次只用来给友好文案。
        """
        request_id = self.new_id("shr")
        # 最多两轮:第一轮撞唯一索引后,若那条 pending 恰好在恢复期间被决定/撤回,谓词
        # 已不再覆盖它,第二轮插得进去。两轮都撞且都消失(病理性反复)给稳定冲突,不 500。
        for _attempt in range(2):
            try:
                with self.database.write() as db:
                    # 顺序与 PG 侧保持一致(组 → 笔记本):那边这个顺序承载锁序论证,
                    # 这边没有行锁、顺序无关,但两份实现读起来必须是同一段代码。
                    self._require_group_on(db, group_id)
                    self._require_notebook_on(db, notebook_id)
                    self._require_plain_membership_on(db, group_id, requested_by)
                    db.execute(
                        "INSERT INTO notebook_share_requests "
                        "(id,notebook_id,group_id,requested_by,status,created_at) "
                        "VALUES (?,?,?,?, 'pending', ?)",
                        (request_id, notebook_id, group_id, requested_by, self.now()),
                    )
                    # 终态投影在**同一写事务内**读回(与 PG 侧同一结构):放到事务外的独立
                    # 连接里读,一个并发删组可以在提交与这次读之间 CASCADE 删掉本行,`row`
                    # 变 None → `_share_request_row(None)` 崩。事务内这行必然存在。
                    row = db.execute(
                        _SHARE_REQUEST_SELECT + "WHERE sr.id=?", (request_id,)
                    ).fetchone()
                return self._share_request_row(row)
            except sqlite3.IntegrityError as exc:
                if not all(
                    marker in str(exc)
                    for marker in _SHARE_REQUEST_PENDING_UNIQUE_MARKERS
                ):
                    raise
                existing = self._pending_share_request(notebook_id, group_id)
                if existing is None:
                    # 恢复期间那条 pending 被决定/撤回了 —— 重试一次插入,绝不让原始的
                    # 唯一违例冒成 500(codex #519 R3)。
                    continue
                if existing["requested_by"] != requested_by:
                    # 幂等**只对本人成立**:把别人的申请行返回给他,会让界面报成功而自查
                    # 列表里没有、也撤不掉(codex #519 R3)。
                    raise ShareRequestAlreadyPendingError(notebook_id, group_id) from exc
                return existing
        raise ShareRequestAlreadyPendingError(notebook_id, group_id)

    def list_pending_share_requests(self, group_id: str) -> list[dict]:
        """组管理员的审核队列:该组全部**待审批**申请。`status='pending'` 精确匹配。"""
        with self.database.connect() as db:
            rows = db.execute(
                _SHARE_REQUEST_SELECT
                + "WHERE sr.group_id=? AND sr.status='pending' "
                + "ORDER BY sr.created_at ASC, sr.id ASC",
                (group_id,),
            ).fetchall()
        return [self._share_request_row(row) for row in rows]

    def list_my_share_requests(
        self, notebook_id: str, *, requested_by: str
    ) -> list[dict]:
        """请求者本人对某本库发起过的全部申请(弹窗回显待审批 / 已驳回),最新在前。"""
        with self.database.connect() as db:
            rows = db.execute(
                _SHARE_REQUEST_SELECT
                + "WHERE sr.notebook_id=? AND sr.requested_by=? "
                + "ORDER BY sr.created_at DESC, sr.id ASC",
                (notebook_id, requested_by),
            ).fetchall()
        return [self._share_request_row(row) for row in rows]

    def list_pending_share_requests_by_requester(
        self, requested_by: str
    ) -> list[dict]:
        """**我发起的、仍待审批的**全部申请 —— 跨笔记本,不带任何 notebook 维度收窄。

        存在的理由是裁决 P2-7 的另一半(codex #519 R11 P1):撤回刻意只认「这条申请是你
        提的」、不要求笔记本管理权,因为 P2-6 让批准会拒绝已失权的申请人——两边都要管理权
        的话,这类申请**既批不了也撤不掉**,永远卡在组管理员队列里。但按笔记本列申请的
        `list_my_share_requests` 挂在 manage 门后面,申请人一失权就连申请 id 都拿不回来,
        那个口子等于没开。这条查询是给他的:**唯一的谓词是 `requested_by`**,与 DELETE
        的授权轴逐字相同。

        只回 `status='pending'`(**正向精确匹配**,红线):已决定的申请撤不回来,列出来
        既没有可做的动作,又平白多披露一份历史。

        ⚠ 两个展示标签**各自**按当前权限决定给不给(codex #519 R12 P2),理由与写法见
        `_MY_PENDING_SHARE_REQUEST_SELECT` 上面那段:他知道的是**提交那一刻**的名字,
        失权之后这条清单不能继续把改名后的**新**名字送给他。
        """
        with self.database.connect() as db:
            rows = db.execute(
                _MY_PENDING_SHARE_REQUEST_SELECT
                + "WHERE sr.requested_by=? AND sr.status='pending' "
                + "ORDER BY sr.created_at DESC, sr.id ASC",
                # 参数顺序跟着 SQL 文本走:两个 CASE 在 SELECT 列表里,所以它们的占位符
                # 排在 WHERE 之前。读权那半消费几个由 `read_access_params` 自己说了算。
                (*read_access_params(requested_by), requested_by, requested_by),
            ).fetchall()
        return [self._share_request_row(row) for row in rows]

    def _require_plain_membership_on(
        self, db: sqlite3.Connection, group_id: str, user_id: str
    ) -> None:
        """在**当前写事务里**复核这个人是目标组的**普通成员**(codex #519 R8 P2)。

        两种不合格各有各的语义,绝不能合并成一句「不许」:

        * **不是成员** → `GroupMembershipRequiredError`(路由 → 404,与群组维度的
          「看不见」口径一致——他不该知道这个组存不存在);
        * **是组管理员** → `GroupAdminShouldShareDirectlyError`(路由 → 403 + 一句可操作
          的说明)。他手上有更直接的路径(`POST /notebooks/{id}/grants`),而契约明写
          「组管理员分享进自己管理的组永远走既有 grants 端点、不经这张表」。不挡的话他
          能建出一条 pending 申请再**自己批自己**;而遮蔽成 404 对他毫无意义,组的存在性
          对一个管理员根本不是秘密。

        放行是**正向精确匹配** `role == 'member'`:组管理员与任何未知取值(含正向 shadow
        停车写进去的哨兵串)都落进第二支,方向 fail closed。写成 `!= 'admin'` 就反了——
        哨兵行会被当成普通成员放行。
        """
        role = self._role_on(db, group_id, user_id)
        if role is None:
            raise GroupMembershipRequiredError(group_id)
        if role != "member":
            raise GroupAdminShouldShareDirectlyError(group_id)

    def _require_share_decider_on(
        self,
        db: sqlite3.Connection,
        group_id: str,
        decided_by: str,
        is_system_admin: bool,
    ) -> None:
        """在**当前写事务里**复核这个人此刻仍有权审批本组的共享申请。

        路由的 `_require_group_admin` 与本事务之间永远有一个窗口:一个并发的「把他降级/
        移出组」请求可以整个跑完在中间。少了这一条,一个**刚被降级的人**仍能批准申请、
        把整组的读权放出去(驳回同窗口)。这与「最后一名组管理员保护」「`create_grant` 的
        双重条件」是同一条纪律:授权判定以写事务内这一次为准。

        ``is_system_admin`` 由路由传入(store 不读 `users.role`、不做身份解析),承载
        `_require_group_admin` 的系统管理员运维旁路:他不必是组成员也能审批。
        """
        if is_system_admin:
            return
        if self._role_on(db, group_id, decided_by) != "admin":
            raise GroupAdminRequiredError(group_id)

    def _require_notebook_manage_on(
        self, db: sqlite3.Connection, notebook_id: str, user_id: str
    ) -> None:
        """在**当前写事务里**复核这个人此刻仍对这本库有管理权(R4 裁决 + R6 通用化)。

        两个调用点、两种语义,共用这一份谓词(见各自的包装):
        `approve_share_request` 复核的是**申请人**(陈旧的申请不得兑现成活授权边),
        `create_grant` 复核的是**发起人**(失权者不得继续把访问权散出去)。判据见
        `api/deps.py` 那条裁决:凡是写 `notebook_grants` 的路径都要有这一次复检。

        授权在**生效时刻**实时判定、绝不缓存——这是本仓库反复钉的原则(挂载边不是授权
        凭证、撤销即时生效;公开报告页也按同一条裁过:创建时合法 ≠ 持续有效)。申请提交
        与批准之间可以隔任意长时间,库主完全可能在中间撤掉申请人的管理边;批准把那次陈旧
        的检查兑现成一条**活的**授权边,整组人立刻读得到,而库主从未同意。批准这一刻没有
        任何一方在验这件事:组管理员验的是「我的组要不要这个库」。

        谓词一律取自 `access_sql`——「谁能管这个 notebook」的唯一定义点在那里,store 消费
        它是既有惯例(见 `sharing_store.user_can_admin_notebook`)。

        写成**两段式**(owner 半 + 授权边半)而不是单跑 `NOTEBOOK_ADMIN_SQL`,是为了与 PG
        侧结构对齐:那边必须拆开才能给授权边行挂 `FOR SHARE`(行锁够不到 `EXISTS` 子查询
        里的行),否则并发撤销能与批准交错(codex #519 R5)。

        ⚠ SQLite 侧**刻意没有**对应的行锁:`SqliteDatabase.write()` 是进程级写锁,同一时刻
        只有一个写事务在跑,撤销授权边与本次批准根本插不进彼此中间——与
        `delete_group` / `create_share_request` / `approve` 组锁同款的后端分叉。两段拆分在
        这一侧因此纯粹是结构对等(外加保住 owner 半那条便宜的短路)。

        ⚠ 与 PG 侧同一条:owner 半论的是「**身份**不会变」,不是「那**一行**还在」。库被并发
        删掉时两半都查不到 → 这里抛 `NotebookManageRequiredError`(不会像 PG 那样撞外键 500,
        进程写锁保证删库不会插在本事务中间),但那句话在库已经不存在时是误导。所以两个调用方
        里的 `create_grant` 在本方法之前先跑 `_require_notebook_on`,让答案变成 404
        「笔记本不存在」,与 PG 侧对齐(codex #519 R7 存疑项收口);另一个调用方
        `approve_share_request` 不需要——它先按 `id + group_id + status='pending'` 取申请行,
        库被删时那行已被外键级联带走,查不到就返回 `None`(路由 404),压根到不了这里。
        """
        if (
            db.execute(
                "SELECT 1 FROM notebooks WHERE id=? AND created_by=?",
                (notebook_id, user_id),
            ).fetchone()
            is not None
        ):
            return
        if (
            db.execute(
                ADMIN_GRANT_PROBE_SQL,
                admin_grant_probe_params(notebook_id, user_id),
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
        """批准:**同一写事务**里复核仍 pending + 审批人仍有资格 → 写 `(group, viewer)`
        授权边 → 状态置 approved。申请不存在或已被决定返回 `None`(路由 → 404);审批人
        已被降级/移出组抛 `GroupAdminRequiredError`(路由 → 403)。

        `status='pending'` 是**精确匹配**(红线:绝不 `!= ...`)。授权边用
        `ON CONFLICT DO NOTHING` 写:已共享(同库同组已有边)时静默复用,继续把申请标
        approved——不能因为「已经共享过」就让批准失败,那会留下一条永远批不掉的申请。
        写授权边与改状态在同一事务:批准这件事要么整体生效,要么整体不生效。

        ⚠ 这里**刻意没有** PG 侧那道 `_lock_group_on(mode="SHARE")`:`SqliteDatabase.write()`
        是进程级写锁,approve 与并发的 `delete_group` 不可能交错,所以插入 `notebook_grants`
        边与删组之间插不进第三个事务、造不出孤儿边(PG 没有那把锁,必须显式 `FOR SHARE`,
        理由完整写在 `postgres/group_store.py::approve_share_request`)。与
        `delete_group` / `create_share_request` 的同款后端分叉一致。
        """
        stamp = self.now()
        with self.database.write() as db:
            row = db.execute(
                "SELECT notebook_id, requested_by FROM notebook_share_requests "
                "WHERE id=? AND group_id=? AND status='pending'",
                (request_id, group_id),
            ).fetchone()
            if row is None:
                return None
            self._require_share_decider_on(
                db, group_id, decided_by, decided_by_is_system_admin
            )
            notebook_id = row["notebook_id"]
            # 申请人复检:失权 → 语义是「这条**申请**不能兑现」(409),不是「你没权限」,
            # 所以把通用的管理权错误翻译成 share-request 专用的那一个(R4 裁决)。
            try:
                self._require_notebook_manage_on(
                    db, notebook_id, row["requested_by"]
                )
            except NotebookManageRequiredError as exc:
                raise ShareRequesterUnauthorizedError(notebook_id) from exc
            db.execute(
                "INSERT INTO notebook_grants "
                "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                "VALUES (?,?, 'group', ?, 'viewer', ?, ?) "
                "ON CONFLICT(notebook_id, principal_type, principal_id) DO NOTHING",
                (self.new_id("gnt"), notebook_id, group_id, decided_by, stamp),
            )
            db.execute(
                "UPDATE notebook_share_requests "
                "SET status='approved', decided_by=?, decided_at=? WHERE id=?",
                (decided_by, stamp, request_id),
            )
            # 终态投影在事务内读回(理由同 create:事务外读会被并发删撞成 None)。
            out = db.execute(
                _SHARE_REQUEST_SELECT + "WHERE sr.id=?", (request_id,)
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
        """驳回:状态置 rejected + `decided_by`/`decided_at`。不写任何授权边。申请不存在
        或已被决定返回 `None`;审批人已被降级/移出组抛 `GroupAdminRequiredError`。
        `status='pending'` 精确匹配。

        ⚠ 审批资格复核**先于** UPDATE 发出:驳回虽然不发授权边,但它同样是一个终态决定
        (把别人的申请判死、并让那条 pending 从队列消失),被降级的人不该还能做。
        """
        stamp = self.now()
        with self.database.write() as db:
            self._require_share_decider_on(
                db, group_id, decided_by, decided_by_is_system_admin
            )
            cursor = db.execute(
                "UPDATE notebook_share_requests "
                "SET status='rejected', decided_by=?, decided_at=? "
                "WHERE id=? AND group_id=? AND status='pending'",
                (decided_by, stamp, request_id, group_id),
            )
            if cursor.rowcount == 0:
                return None
            # 终态投影在事务内读回(理由同 create:事务外读会被并发删撞成 None)。
            out = db.execute(
                _SHARE_REQUEST_SELECT + "WHERE sr.id=?", (request_id,)
            ).fetchone()
        return self._share_request_row(out)

    def delete_share_request(
        self, notebook_id: str, request_id: str, requester_id: str
    ) -> str:
        """撤回:仅**申请者本人**、且仅 `status='pending'` 可删(不写 `decided_*`)。

        返回 `"deleted"` / `"not_found"`;已决定抛 `ShareRequestNotPendingError`(→ 409)。
        判据按**精确**状态匹配 `status == 'pending'`,绝不用否定式。

        WHERE 带**三**列,每一列都在挡一种越权:
        * `notebook_id` —— 只按 request_id 删等于让「我有一本库的管理权」变成「我能撤任何
          库上的任何申请」;
        * `requested_by` —— 撤回是**申请者自己的动作**(裁决 P2-2:它不是决定,所以不写
          `decided_by`/`decided_at`)。少了这一列,同一本库上的**另一位**管理员就能撤掉
          别人提交的申请:能力守卫只证明「这个人对这本库有管理权」,证明不了「这条申请是
          他提的」(codex #519 R1 P1)。判据取 `requested_by` 而不是「有没有管理权」,
          因为申请人可能在提交后失去管理权,那时他仍应能撤回自己那条。
        * 不属于自己的申请与根本不存在的申请**同样返回 `not_found`** —— 不泄露「这本库上
          存在一条别人的待审批申请」。
        """
        with self.database.write() as db:
            row = db.execute(
                "SELECT status FROM notebook_share_requests "
                "WHERE id=? AND notebook_id=? AND requested_by=?",
                (request_id, notebook_id, requester_id),
            ).fetchone()
            if row is None:
                return "not_found"
            # 放行判据是**正向**精确匹配 `== 'pending'`,不是 `!= 'pending'`(红线):
            # 已决定(approved/rejected)以及任何非 pending 值都落进 else、一律不可撤回。
            # 否定式会把 shadow 停车行的哨兵 status 误判成「已决定」,正向式让它天然 fail
            # safe。
            if row["status"] == "pending":
                db.execute(
                    "DELETE FROM notebook_share_requests "
                    "WHERE id=? AND notebook_id=? AND requested_by=?",
                    (request_id, notebook_id, requester_id),
                )
                return "deleted"
            raise ShareRequestNotPendingError(request_id)
