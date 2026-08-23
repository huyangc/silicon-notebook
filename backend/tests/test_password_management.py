"""用户自助修改密码 + 管理员重置用户密码。

覆盖:
  * IdentityStore.change_user_password:成功改密、旧密码校验失败、新密码空、
    用户不存在;会话吊销保留当前 token,其余吊销。
  * IdentityStore.admin_reset_user_password:非 admin actor 拒绝、目标不存在、
    成功后旧密码失效/新密码可登录/目标全部会话吊销;非内置管理员重置自己也允许。
  * 内置管理员 user-local 两条路径都拒绝(BuiltinAdminPasswordError→409):它的
    密码每次启动被 seed 按 settings.admin_password 重写,在线改了会被静默回滚。
  * PATCH /me/password:正确旧密码改密成功且当前会话保留、旧密码错 400(精确
    文案「当前密码不正确」,钉住 except 顺序)、新密码空白 400、未认证 401。
  * POST /admin/users/{id}/reset-password:非 admin 403、admin 成功、目标不存在
    404、空密码 400;auth_optional 匿名回退(无 token 即种子管理员)403。
  * extra="forbid" 对未知字段的 422 校验。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.repositories.identity_errors import (
    BuiltinAdminPasswordError,
    PasswordMismatchError,
)
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


def _repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None, event_log_enabled=False, llm_log_enabled=False,
        )
    )


def _identity(repo: SQLiteRepository):
    return repo._runtime.identity


# --------------------------------------------------------------------------- #
# 1. Store 级: change_user_password
# --------------------------------------------------------------------------- #
def test_change_user_password_success(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000001", "old-pw")

    ident.change_user_password(created.id, "old-pw", "new-pw")

    assert ident.authenticate_user("z00000001", "old-pw") is None
    assert ident.authenticate_user("z00000001", "new-pw") is not None


def test_change_user_password_wrong_old_password(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000002", "old-pw")

    with pytest.raises(PasswordMismatchError):
        ident.change_user_password(created.id, "wrong-old", "new-pw")

    # 未改动
    assert ident.authenticate_user("z00000002", "old-pw") is not None


def test_change_user_password_rejects_empty_new_password(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000003", "old-pw")

    with pytest.raises(ValueError):
        ident.change_user_password(created.id, "old-pw", "   ")


def test_change_user_password_missing_user(tmp_path):
    ident = _identity(_repo(tmp_path))
    with pytest.raises(KeyError):
        ident.change_user_password("user-does-not-exist", "old-pw", "new-pw")


def test_change_user_password_keeps_current_session_revokes_others(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000004", "old-pw")
    token_a = ident.create_session(created.id)
    token_b = ident.create_session(created.id)

    ident.change_user_password(created.id, "old-pw", "new-pw", keep_token=token_a)

    assert ident.resolve_session(token_a) is not None
    assert ident.resolve_session(token_b) is None


def test_change_user_password_without_keep_token_revokes_all(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000005", "old-pw")
    token_a = ident.create_session(created.id)
    token_b = ident.create_session(created.id)

    ident.change_user_password(created.id, "old-pw", "new-pw")

    assert ident.resolve_session(token_a) is None
    assert ident.resolve_session(token_b) is None


def test_register_user_with_session_is_atomic_and_sweepable(tmp_path):
    """codex R2 P2:注册+首个会话单写事务。功能面:返回可解析的 token、重名拒绝;
    语义面:注册后立刻重置,注册发出的会话必须被吊销 DELETE 扫到(原子化后重置
    不可能落在「建用户」与「发会话」之间)。"""
    ident = _identity(_repo(tmp_path))

    profile, token = ident.register_user_with_session("z00000014", "pw")
    assert profile.username == "z00000014"
    assert ident.resolve_session(token) is not None
    with pytest.raises(ValueError):
        ident.register_user_with_session("z00000014", "pw2")

    ident.admin_reset_user_password("user-local", profile.id, "reset-pw")
    assert ident.resolve_session(token) is None
    assert ident.authenticate_user("z00000014", "reset-pw") is not None


def test_login_with_password_round_trips(tmp_path):
    ident = _identity(_repo(tmp_path))
    ident.create_user("z00000012", "pw")

    assert ident.login_with_password("z00000012", "wrong") is None
    assert ident.login_with_password("z00999999", "pw") is None
    result = ident.login_with_password("z00000012", "pw")
    assert result is not None
    user, token = result
    assert user.username == "z00000012"
    assert ident.resolve_session(token) is not None


def test_login_verify_happens_inside_the_write_lock(tmp_path, monkeypatch):
    """codex R1 P1 的确定性钉法:login_with_password 的密码验证必须发生在数据库
    写事务内(验证时已持有 write_lock)。这就是「旧密码登录的会话逃不过改密吊销」
    的机制——验证与建会话同锁串行,改密要么排在其后(它的 DELETE 带走刚插的会话),
    要么排在其前(旧密码直接验证失败),不存在「读连接下验证通过、吊销提交后再插
    会话」的第三种交错。旧的拆分实现(authenticate_user 读连接验证 + 事后
    create_session)在验证点不持写锁,本断言必红;不用线程交错测试是因为那种写法
    对拆分实现的交错结果不确定(DELETE 也可能恰好落在 INSERT 之后而侥幸变绿)。"""
    from app.domain import auth_utils  # B3: repos now lazy-import from domain

    ident = _identity(_repo(tmp_path))
    ident.create_user("z00000013", "pw")

    real_verify = auth_utils.verify_password
    held_write_lock: list[bool] = []

    def probing_verify(*args, **kwargs):
        lock = ident.database.write_lock
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        held_write_lock.append(not acquired)
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(auth_utils, "verify_password", probing_verify)
    result = ident.login_with_password("z00000013", "pw")
    assert result is not None
    assert held_write_lock == [True]

    # 串行语义的端到端半:改密提交后,旧密码登录必须失败(验证读到的是新哈希)。
    ident.change_user_password(result[0].id, "pw", "rotated-pw")
    assert ident.login_with_password("z00000013", "pw") is None
    assert ident.login_with_password("z00000013", "rotated-pw") is not None


# --------------------------------------------------------------------------- #
# 2. Store 级: admin_reset_user_password
# --------------------------------------------------------------------------- #
def test_admin_reset_user_password_requires_admin_actor(tmp_path):
    ident = _identity(_repo(tmp_path))
    normal = ident.create_user("z00000006", "pw")
    target = ident.create_user("z00000007", "old-pw")

    with pytest.raises(PermissionError):
        ident.admin_reset_user_password(normal.id, target.id, "new-pw")


def test_admin_reset_user_password_missing_target(tmp_path):
    ident = _identity(_repo(tmp_path))
    # user-local 是种子内置管理员
    with pytest.raises(KeyError):
        ident.admin_reset_user_password("user-local", "user-does-not-exist", "new-pw")


def test_admin_reset_user_password_success_revokes_all_target_sessions(tmp_path):
    ident = _identity(_repo(tmp_path))
    target = ident.create_user("z00000008", "old-pw")
    token_a = ident.create_session(target.id)
    token_b = ident.create_session(target.id)

    result = ident.admin_reset_user_password("user-local", target.id, "new-pw")
    assert result["id"] == target.id
    assert result["username"] == "z00000008"

    assert ident.authenticate_user("z00000008", "old-pw") is None
    assert ident.authenticate_user("z00000008", "new-pw") is not None
    assert ident.resolve_session(token_a) is None
    assert ident.resolve_session(token_b) is None


def test_admin_reset_user_password_rejects_empty_password(tmp_path):
    ident = _identity(_repo(tmp_path))
    target = ident.create_user("z00000009", "old-pw")
    with pytest.raises(ValueError):
        ident.admin_reset_user_password("user-local", target.id, "")


def test_non_builtin_admin_can_reset_own_password(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000010", "pw")
    ident.set_user_role("user-local", created.id, "admin")

    result = ident.admin_reset_user_password(created.id, created.id, "brand-new-pw")
    assert result["id"] == created.id
    assert ident.authenticate_user("z00000010", "brand-new-pw") is not None


def test_builtin_admin_password_is_locked_on_both_paths(tmp_path):
    """user-local 的密码每次启动被 seed 重写(settings.admin_password),在线改密
    会在下次重启被静默回滚——两条改密路径都必须显式拒绝而不是假装成功。"""
    ident = _identity(_repo(tmp_path))
    with pytest.raises(BuiltinAdminPasswordError):
        ident.change_user_password("user-local", "admin", "new-pw")
    with pytest.raises(BuiltinAdminPasswordError):
        ident.admin_reset_user_password("user-local", "user-local", "new-pw")
    # 密码未被动过:种子默认 admin/admin 仍可登录
    assert ident.authenticate_user("admin", "admin") is not None


# --------------------------------------------------------------------------- #
# 3. HTTP 端点
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _register_and_login(client: TestClient, username: str = "z00123456", password: str = "pw") -> str:
    client.post("/api/auth/register", json={"username": username, "password": password})
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r.json()["token"]


def _auth_admin(client: TestClient) -> str:
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]


def test_patch_me_password_success_keeps_current_session(client):
    token = _register_and_login(client, "z00111111", "old-pw")
    headers = {"Authorization": f"Bearer {token}"}

    r = client.patch(
        "/api/me/password",
        json={"old_password": "old-pw", "new_password": "new-pw"},
        headers=headers,
    )
    assert r.status_code == 204, r.text

    # 旧密码登录失败
    old_login = client.post(
        "/api/auth/login", json={"username": "z00111111", "password": "old-pw"}
    )
    assert old_login.status_code == 401

    # 新密码登录成功
    new_login = client.post(
        "/api/auth/login", json={"username": "z00111111", "password": "new-pw"}
    )
    assert new_login.status_code == 200

    # 原 token 仍可用(当前会话保留)
    me = client.get("/api/me", headers=headers)
    assert me.status_code == 200


def test_patch_me_password_wrong_old_password_400(client):
    token = _register_and_login(client, "z00111112", "old-pw")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        "/api/me/password",
        json={"old_password": "totally-wrong", "new_password": "new-pw"},
        headers=headers,
    )
    assert r.status_code == 400
    # 精确断言:两条 400 文案都含「密码」,只有逐字比较才能钉住 except 顺序
    # (PasswordMismatchError 必须先于其它 ValueError 分支被捕获)。
    assert r.json()["detail"] == "当前密码不正确"


def test_patch_me_password_empty_new_password_400(client):
    token = _register_and_login(client, "z00111113", "old-pw")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        "/api/me/password",
        json={"old_password": "old-pw", "new_password": "   "},
        headers=headers,
    )
    assert r.status_code == 400


def test_patch_me_password_requires_auth(client):
    r = client.patch(
        "/api/me/password",
        json={"old_password": "a", "new_password": "b"},
    )
    assert r.status_code == 401


def test_admin_reset_password_endpoint_requires_admin(client):
    token = _register_and_login(client, "z00111114", "pw")
    other = _register_and_login(client, "z00111115", "pw")
    # z00111114 尝试重置别人密码,非 admin 应 403
    r = client.patch(
        "/api/me/ui-mode",
        json={"ui_mode": "auto"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200  # sanity: token 有效

    # 找到目标 user id
    me_other = client.get(
        "/api/me", headers={"Authorization": f"Bearer {other}"}
    ).json()

    r = client.post(
        f"/api/admin/users/{me_other['id']}/reset-password",
        json={"new_password": "new-pw"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_admin_reset_password_endpoint_success(client):
    target_token = _register_and_login(client, "z00111116", "old-pw")
    target_id = client.get(
        "/api/me", headers={"Authorization": f"Bearer {target_token}"}
    ).json()["id"]

    admin_token = _auth_admin(client)
    r = client.post(
        f"/api/admin/users/{target_id}/reset-password",
        json={"new_password": "brand-new-pw"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "z00111116"

    # 目标旧 token 失效
    me = client.get(
        "/api/me", headers={"Authorization": f"Bearer {target_token}"}
    )
    assert me.status_code == 401

    # 新密码可登录
    new_login = client.post(
        "/api/auth/login", json={"username": "z00111116", "password": "brand-new-pw"}
    )
    assert new_login.status_code == 200


def test_admin_reset_password_endpoint_missing_user_404(client):
    admin_token = _auth_admin(client)
    r = client.post(
        "/api/admin/users/user-does-not-exist/reset-password",
        json={"new_password": "new-pw"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


def test_admin_reset_password_endpoint_empty_password_400(client):
    target_token = _register_and_login(client, "z00111117", "old-pw")
    target_id = client.get(
        "/api/me", headers={"Authorization": f"Bearer {target_token}"}
    ).json()["id"]
    admin_token = _auth_admin(client)
    r = client.post(
        f"/api/admin/users/{target_id}/reset-password",
        json={"new_password": ""},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400


def test_builtin_admin_change_password_endpoint_409(client):
    admin_token = _auth_admin(client)
    r = client.patch(
        "/api/me/password",
        json={"old_password": "admin", "new_password": "new-pw"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 409
    assert "环境变量" in r.json()["detail"]


def test_builtin_admin_reset_password_endpoint_409(client):
    admin_token = _auth_admin(client)
    r = client.post(
        "/api/admin/users/user-local/reset-password",
        json={"new_password": "new-pw"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# 3b. auth_optional 匿名回退闸
# --------------------------------------------------------------------------- #
@pytest.fixture
def client_auth_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def test_anonymous_fallback_cannot_reset_password(client_auth_optional):
    """auth_optional=true 时无 token 请求回退为种子管理员;重置密码是锁死型
    操作,必须由真实登录的管理员会话发起——匿名回退 403,不落到 seeded-admin。"""
    c = client_auth_optional
    c.post("/api/auth/register", json={"username": "z00111120", "password": "pw"})
    login = c.post("/api/auth/login", json={"username": "z00111120", "password": "pw"})
    target_id = login.json()["user"]["id"]

    r = c.post(
        f"/api/admin/users/{target_id}/reset-password",
        json={"new_password": "hijacked"},
    )
    assert r.status_code == 403
    # 目标密码未被动过
    again = c.post("/api/auth/login", json={"username": "z00111120", "password": "pw"})
    assert again.status_code == 200


def test_anonymous_fallback_change_password_hits_builtin_guard(client_auth_optional):
    """匿名回退的 /me/password 目标是 user-local,被内置管理员守卫挡下(409),
    不会吊销管理员在线会话、也不会改动其密码。"""
    r = client_auth_optional.patch(
        "/api/me/password",
        json={"old_password": "admin", "new_password": "hijacked"},
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# 4. extra="forbid"
# --------------------------------------------------------------------------- #
def test_patch_me_password_rejects_unknown_field_422(client):
    token = _register_and_login(client, "z00111118", "old-pw")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        "/api/me/password",
        json={"old_password": "old-pw", "new_password": "new-pw", "extra_field": "x"},
        headers=headers,
    )
    assert r.status_code == 422


def test_admin_reset_password_rejects_unknown_field_422(client):
    target_token = _register_and_login(client, "z00111119", "old-pw")
    target_id = client.get(
        "/api/me", headers={"Authorization": f"Bearer {target_token}"}
    ).json()["id"]
    admin_token = _auth_admin(client)
    r = client.post(
        f"/api/admin/users/{target_id}/reset-password",
        json={"new_password": "new-pw", "extra_field": "x"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
