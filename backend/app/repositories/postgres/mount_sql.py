"""Canonical PostgreSQL reference-library mount SQL fragments.

`sqlite/mount_sql.py` 的镜像。完整理由(四支可挂范围、P1「读权 ⇒ 可挂载」这条显式
行为变更、以及第 4 支「借入挂载」为什么要额外挂一道未共享门)写在 SQLite 那一份的
模块 docstring 里,两份必须同修。
"""

from app.repositories.postgres.access_sql import (
    NOTEBOOK_LIVE_SQL,
    everyone_grant_expr,
    member_exists_expr,
    restricted_grant_access_expr,
)

MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "WHERE e.notebook_id = %s AND b.id != e.notebook_id"
)

# 挂载方 owner 对被挂库的**受限**读权(只读成员 ∨ 点名授权边)。
_BORROWED_READ_EXPR = (
    "("
    + member_exists_expr("b.id", "a.created_by", "nm")
    + " OR "
    + restricted_grant_access_expr("b.id", "a.created_by")
    + ")"
)

# 未共享门:挂载方笔记本自身没有任何只读成员、也没有任何授权边。借来的东西不转借。
_MOUNTER_NOT_SHARED_EXPR = (
    "(NOT EXISTS (SELECT 1 FROM notebook_members xm WHERE xm.notebook_id = a.id)"
    " AND NOT EXISTS (SELECT 1 FROM notebook_grants xg WHERE xg.notebook_id = a.id))"
)

MOUNT_VALID_EXPR = (
    "(b." + NOTEBOOK_LIVE_SQL + " AND (b.tier = 'base' OR b.created_by = a.created_by"
    " OR " + everyone_grant_expr("b.id")
    + " OR (" + _BORROWED_READ_EXPR + " AND " + _MOUNTER_NOT_SHARED_EXPR + ")"
    "))"
)

# 借入边被「未共享门」关上的判别式(镜像 sqlite/mount_sql.py,理由写在那份)。
MOUNT_GATE_CLOSED_EXPR = (
    "(b." + NOTEBOOK_LIVE_SQL + " AND " + _BORROWED_READ_EXPR
    + " AND NOT " + _MOUNTER_NOT_SHARED_EXPR + ")"
)

MOUNT_VALID = " AND " + MOUNT_VALID_EXPR

# 可挂候选的来源投影(群组知识共享 P1-T4)。
# `sqlite/mount_sql.py::MOUNT_ORIGIN_COLUMN` 的镜像,完整理由写在那一份。
MOUNT_ORIGIN_COLUMN = (
    "CASE WHEN b.tier = 'base' THEN 'base'"
    " WHEN b.created_by = a.created_by THEN 'mine'"
    " ELSE 'shared' END AS origin"
)

MOUNT_ORDER = (
    " ORDER BY CASE WHEN b.tier = 'base' THEN 0 ELSE 1 END, "
    "b.name COLLATE \"C\", b.id COLLATE \"C\""
)

MOUNTED_BASE_IDS_SUBQUERY = "SELECT b.id " + MOUNT_JOIN + MOUNT_VALID
