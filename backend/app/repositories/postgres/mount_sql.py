"""Canonical PostgreSQL reference-library mount SQL fragments."""

MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "WHERE e.notebook_id = %s AND b.id != e.notebook_id"
)

MOUNT_VALID_EXPR = (
    "(b.status != 'copying' AND (b.tier = 'base' OR b.created_by = a.created_by))"
)

MOUNT_VALID = " AND " + MOUNT_VALID_EXPR

MOUNT_ORDER = (
    " ORDER BY CASE WHEN b.tier = 'base' THEN 0 ELSE 1 END, "
    "b.name COLLATE \"C\", b.id COLLATE \"C\""
)

MOUNTED_BASE_IDS_SUBQUERY = "SELECT b.id " + MOUNT_JOIN + MOUNT_VALID
