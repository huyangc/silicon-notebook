from __future__ import annotations

from app.repositories.auth_store import AuthStore
from app.repositories.identity_errors import AuthStoreError
from app.repositories.postgres._store_utils import iso_timestamp, utc_now
from app.repositories.postgres.database import PostgresDatabase


#: Single definition point for the admin-recheck row lock, shared with
#: ``test_extension_toggle_store_conformance.py``'s lock-probe test so the
#: test can never drift from what this store actually executes (mirrors
#: ``access_sql.py``'s exported ``ADMIN_GRANT_*_SQL`` constants, used the
#: same way by ``test_admin_grant_chain_lock.py``).
ACTOR_ADMIN_ROLE_LOCK_SQL = "SELECT role,status FROM users WHERE id=%s FOR UPDATE"


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

    def __init__(self, database: PostgresDatabase, auth: AuthStore) -> None:
        self.database = database
        self.auth = auth

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
        self, plugin_id: str, enabled: bool, actor_id: str,
        *, require_sso_admin: bool = False,
    ) -> dict:
        """授权在写事务内按 actor 现时角色复检(镜像
        ``identity_store.set_user_role``:``FOR UPDATE`` 锁 actor 行,非 admin 或已停用
        → ``PermissionError``,不写入)。

        事务先取认证全局锁(``AuthStore.lock``,与改角色/停用账号同一把 advisory
        锁,加锁顺序也同为「认证锁 → users 行锁」)。``require_sso_admin`` 时
        (启用提供 ``auth.provider`` 的插件)在锁内复查「有内置管理员以外的在用
        管理员」,没有就 ``AuthStoreError("no_sso_admin")``、不写入:并发的降级/
        停用要么已提交、被这次复查看到,要么排在这次提交之后。

        ``plugin_id`` 只做最小护栏——空串/纯空白直接拒绝。「必须在已装载的
        deployment 插件集合内」这条更强的校验留给路由层(它才知道 registry
        冻结后实际装载了哪些插件;这个 store 不认识 registry)。
        """
        if not plugin_id.strip():
            raise ValueError("empty plugin_id")
        with self.database.write() as db:
            self.auth.lock(db)
            actor = db.execute(
                ACTOR_ADMIN_ROLE_LOCK_SQL, (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin" or actor["status"] != "active":
                raise PermissionError("admin role required")
            if require_sso_admin and not self.auth.sso_admin_exists(db):
                raise AuthStoreError("no_sso_admin")
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
