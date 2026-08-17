"""Canonical PostgreSQL reference-library mount SQL fragments.

`sqlite/mount_sql.py` 的镜像。完整理由(含 P1「读权 ⇒ 可挂载」这条显式行为变更、
以及为什么同 owner 那一支刻意保留在读权谓词之外)写在 SQLite 那一份的模块 docstring
里,两份必须同修。
"""

from app.repositories.postgres.access_sql import read_access_clause

MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "WHERE e.notebook_id = %s AND b.id != e.notebook_id"
)

MOUNT_VALID_EXPR = (
    "(b.status != 'copying' AND (b.tier = 'base' OR b.created_by = a.created_by"
    " OR " + read_access_clause("b", user_ref="a.created_by") + "))"
)

MOUNT_VALID = " AND " + MOUNT_VALID_EXPR

MOUNT_ORDER = (
    " ORDER BY CASE WHEN b.tier = 'base' THEN 0 ELSE 1 END, "
    "b.name COLLATE \"C\", b.id COLLATE \"C\""
)

MOUNTED_BASE_IDS_SUBQUERY = "SELECT b.id " + MOUNT_JOIN + MOUNT_VALID
