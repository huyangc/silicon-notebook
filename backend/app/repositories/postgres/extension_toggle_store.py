from __future__ import annotations

from app.repositories.postgres._store_utils import iso_timestamp, utc_now
from app.repositories.postgres.database import PostgresDatabase


#: Single definition point for the admin-recheck row lock, shared with
#: ``test_extension_toggle_store_conformance.py``'s lock-probe test so the
#: test can never drift from what this store actually executes (mirrors
#: ``access_sql.py``'s exported ``ADMIN_GRANT_*_SQL`` constants, used the
#: same way by ``test_admin_grant_chain_lock.py``).
ACTOR_ADMIN_ROLE_LOCK_SQL = "SELECT role FROM users WHERE id=%s FOR UPDATE"


def _row(row) -> dict:
    return {
        "plugin_id": str(row["plugin_id"]),
        "enabled": bool(row["enabled"]),
        "updated_by": str(row["updated_by"]),
        "updated_at": iso_timestamp(row["updated_at"]),
    }


class ExtensionToggleStore:
    """PostgreSQL 侧部署插件运行时开关 + 审计;语义与 SQLite 的
    ``ExtensionToggleStore`` 逐字对齐——无行 = 启用。"""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def extension_runtime_disabled_ids(self) -> frozenset[str]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT plugin_id FROM extension_runtime_toggles WHERE enabled=false"
            ).fetchall()
        return frozenset(str(row["plugin_id"]) for row in rows)

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
        """授权在写事务内按 actor 现时角色复检(镜像
        ``identity_store.set_user_role``:``FOR UPDATE`` 锁 actor 行,非 admin
        → ``PermissionError``,不写入)。

        ``plugin_id`` 只做最小护栏——空串/纯空白直接拒绝。「必须在已装载的
        deployment 插件集合内」这条更强的校验留给路由层(它才知道 registry
        冻结后实际装载了哪些插件;这个 store 不认识 registry)。
        """
        if not plugin_id.strip():
            raise ValueError("empty plugin_id")
        with self.database.write() as db:
            actor = db.execute(
                ACTOR_ADMIN_ROLE_LOCK_SQL, (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            # 取时必须在 FOR UPDATE 拿到 actor 行锁之后:在锁外取时,一个先取
            # 时、后拿锁的请求会用更旧的时间戳盖掉更新的写,让 updated_at 倒退。
            now = utc_now()
            row = db.execute(
                "INSERT INTO extension_runtime_toggles"
                "(plugin_id,enabled,updated_by,updated_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(plugin_id) DO UPDATE SET "
                "enabled=excluded.enabled,updated_by=excluded.updated_by,"
                "updated_at=excluded.updated_at "
                "RETURNING plugin_id,enabled,updated_by,updated_at",
                (plugin_id, enabled, actor_id, now),
            ).fetchone()
            return _row(row)
