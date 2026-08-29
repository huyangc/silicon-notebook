from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.sqlite.database import SqliteDatabase


def _now() -> str:
    """Aware UTC ISO string — deliberately the same construction as the
    PostgreSQL store's ``utc_now()`` + ``iso_timestamp()`` pair (see
    ``app.repositories.postgres._store_utils``), not this repository's more
    common naive-local ``_now()`` (e.g. ``identity_store.py``). This table is
    brand new with zero production rows, so there is no compatibility
    payload to preserve; picking the naive-local shape here would mean an
    admin's browser (``new Date()`` parses a naive string in ITS OWN local
    timezone) shows a wall-clock time that silently drifts from what the
    server actually wrote whenever the two differ. Both backends therefore
    return byte-for-byte the same shape: an offset-aware ``+00:00`` UTC ISO
    string with zero microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row(row) -> dict:
    return {
        "plugin_id": row["plugin_id"],
        "enabled": bool(row["enabled"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


class ExtensionToggleStore:
    """部署插件运行时开关 + 审计(谁、何时)。无行 = 启用——这条设计是这张表
    不改变老部署行为的唯一原因:全新库、或从未有管理员碰过这张表的老库,
    ``extension_runtime_disabled_ids`` 恒为空集。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def extension_runtime_disabled_ids(self) -> frozenset[str]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT plugin_id FROM extension_runtime_toggles WHERE enabled = 0"
            ).fetchall()
        return frozenset(row["plugin_id"] for row in rows)

    def list_extension_runtime_toggles(self) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT plugin_id, enabled, updated_by, updated_at "
                "FROM extension_runtime_toggles ORDER BY plugin_id"
            ).fetchall()
        return [_row(row) for row in rows]

    def set_extension_runtime_enabled(
        self, plugin_id: str, enabled: bool, actor_id: str
    ) -> dict:
        """Upsert 该插件的运行时开关;授权在写事务内按 actor 现时角色复检
        (镜像 ``identity_store.set_user_role``:非 admin → ``PermissionError``,
        不写入),避免读到已被降权的旧角色。行原地更新(``updated_at`` 前进),
        不会因为反复开关而堆出历史行——这张表只存「当前」状态,审计的是最近
        一次操作而非操作序列。

        ``plugin_id`` 只做最小护栏——空串/纯空白直接拒绝。「必须在已装载的
        deployment 插件集合内」这条更强的校验留给路由层(它才知道 registry
        冻结后实际装载了哪些插件;这个 store 不认识 registry)。
        """
        if not plugin_id.strip():
            raise ValueError("empty plugin_id")
        now = _now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            actor = db.execute(
                "SELECT role FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            row = db.execute(
                "INSERT INTO extension_runtime_toggles"
                "(plugin_id, enabled, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(plugin_id) DO UPDATE SET "
                "enabled = excluded.enabled, updated_by = excluded.updated_by, "
                "updated_at = excluded.updated_at "
                "RETURNING plugin_id, enabled, updated_by, updated_at",
                (plugin_id, 1 if enabled else 0, actor_id, now),
            ).fetchone()
            return _row(row)
