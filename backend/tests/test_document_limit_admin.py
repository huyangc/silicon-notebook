"""每笔记本文档数量上限——IMPL-2 后端:管理员配置面(全局默认 + per-user 覆盖)。

覆盖:
  * IdentityStore 写方法:范围校验(1..100000)、写事务内 admin 复检、目标用户不存在
    KeyError、设/清覆盖后生效值解析(清除回落全局默认 app_setting 优先、否则 config)
  * 三个管理员端点:GET/PATCH 全局默认、PATCH per-user 覆盖 —— 行为、非 admin 403、
    范围校验 400 + X-User-Message、缺用户 404
  * list_user_usage / GET /admin/users 每行带 upload_limit + upload_limit_overridden,
    随覆盖/全局默认联动
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.request_context import set_request_user
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


# --------------------------------------------------------------------------- #
# 直连 IdentityStore 写方法(不经 HTTP)
# --------------------------------------------------------------------------- #
def _repo(tmp_path, *, limit: int = 20) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            storage_dir=str(tmp_path / "storage"),
            user_upload_document_limit=limit,
            _env_file=None, event_log_enabled=False, llm_log_enabled=False,
        )
    )


def _identity(repo: SQLiteRepository):
    return repo._runtime.identity


def test_set_global_default_writes_and_reads_back(tmp_path):
    ident = _identity(_repo(tmp_path, limit=20))
    assert ident.set_global_document_limit_default("user-local", 55) == {"limit": 55}
    assert ident.global_document_limit_default() == 55


@pytest.mark.parametrize("bad", [0, -1, 100001, 999999])
def test_set_global_default_rejects_out_of_range(tmp_path, bad):
    ident = _identity(_repo(tmp_path))
    with pytest.raises(ValueError):
        ident.set_global_document_limit_default("user-local", bad)


@pytest.mark.parametrize("ok", [1, 100000])
def test_set_global_default_accepts_boundary(tmp_path, ok):
    # 接受边界:恰好 1 与 100000 应通过(移动变异防线:钉住 <= 别被误改成 <)。
    ident = _identity(_repo(tmp_path))
    assert ident.set_global_document_limit_default("user-local", ok) == {"limit": ok}
    assert ident.global_document_limit_default() == ok


def test_set_global_default_requires_admin_actor(tmp_path):
    repo = _repo(tmp_path)
    u = repo.create_user("a00000001", "pw")  # role=user
    with pytest.raises(PermissionError):
        _identity(repo).set_global_document_limit_default(u.id, 30)


def test_set_user_override_then_clear_resolves_effective(tmp_path):
    repo = _repo(tmp_path, limit=20)
    ident = _identity(repo)
    u = repo.create_user("a00000001", "pw")
    res = ident.set_user_document_limit_override("user-local", u.id, 5)
    assert res == {
        "id": u.id, "username": "a00000001",
        "upload_limit": 5, "upload_limit_overridden": True,
    }
    assert ident.user_document_limit_override(u.id) == 5
    # 清覆盖 → NULL,生效值回落全局默认(此处无 app_setting → config 20)
    res2 = ident.set_user_document_limit_override("user-local", u.id, None)
    assert res2["upload_limit"] == 20 and res2["upload_limit_overridden"] is False
    assert ident.user_document_limit_override(u.id) is None


def test_set_user_override_clear_falls_back_to_global_app_setting(tmp_path):
    """清覆盖后的生效值优先取 app_settings 的全局默认(而非 config 回退)。"""
    repo = _repo(tmp_path, limit=20)
    ident = _identity(repo)
    u = repo.create_user("a00000001", "pw")
    ident.set_global_document_limit_default("user-local", 8)
    res = ident.set_user_document_limit_override("user-local", u.id, None)
    assert res["upload_limit"] == 8 and res["upload_limit_overridden"] is False


@pytest.mark.parametrize("bad", [0, -3, 100001])
def test_set_user_override_rejects_out_of_range(tmp_path, bad):
    repo = _repo(tmp_path)
    u = repo.create_user("a00000001", "pw")
    with pytest.raises(ValueError):
        _identity(repo).set_user_document_limit_override("user-local", u.id, bad)


@pytest.mark.parametrize("ok", [1, 100000])
def test_set_user_override_accepts_boundary(tmp_path, ok):
    # 接受边界:恰好 1 与 100000 应通过(钉住 <=)。
    repo = _repo(tmp_path)
    u = repo.create_user("a00000001", "pw")
    res = _identity(repo).set_user_document_limit_override("user-local", u.id, ok)
    assert res["upload_limit"] == ok and res["upload_limit_overridden"] is True


def test_set_user_override_missing_user_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        _identity(_repo(tmp_path)).set_user_document_limit_override(
            "user-local", "user-missing", 5
        )


def test_set_user_override_requires_admin_actor(tmp_path):
    repo = _repo(tmp_path)
    actor = repo.create_user("a00000001", "pw")   # non-admin
    target = repo.create_user("b00000002", "pw")
    with pytest.raises(PermissionError):
        _identity(repo).set_user_document_limit_override(actor.id, target.id, 5)


def test_list_user_usage_carries_limit_fields(tmp_path):
    repo = _repo(tmp_path, limit=20)
    ident = _identity(repo)
    u = repo.create_user("a00000001", "pw")
    rows = {r["id"]: r for r in repo.list_user_usage()}
    assert rows[u.id]["upload_limit"] == 20              # 无覆盖 → config 默认
    assert rows[u.id]["upload_limit_overridden"] is False
    # 设覆盖 3 → 只该用户变,其余仍全局默认
    ident.set_user_document_limit_override("user-local", u.id, 3)
    rows = {r["id"]: r for r in repo.list_user_usage()}
    assert rows[u.id]["upload_limit"] == 3
    assert rows[u.id]["upload_limit_overridden"] is True
    assert rows["user-local"]["upload_limit"] == 20
    assert rows["user-local"]["upload_limit_overridden"] is False


def test_list_user_usage_reflects_global_default_for_non_overridden(tmp_path):
    repo = _repo(tmp_path, limit=20)
    ident = _identity(repo)
    repo.create_user("a00000001", "pw")
    ident.set_global_document_limit_default("user-local", 9)   # app_setting 覆盖
    rows = {r["username"]: r for r in repo.list_user_usage()}
    assert rows["a00000001"]["upload_limit"] == 9
    assert rows["a00000001"]["upload_limit_overridden"] is False


# --------------------------------------------------------------------------- #
# HTTP 端到端:三个管理员端点 + 用量列表新字段
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/http.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_UPLOAD_DOCUMENT_LIMIT", "20")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _admin(client) -> dict:
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _user(client, username="z00123456") -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _user_id(client, admin, username) -> str:
    rows = client.get("/api/admin/users", headers=admin).json()
    return next(r["id"] for r in rows if r["username"] == username)


def test_get_and_patch_global_default(client):
    admin = _admin(client)
    assert client.get(
        "/api/admin/settings/upload-limit-default", headers=admin
    ).json() == {"limit": 20}
    r = client.patch(
        "/api/admin/settings/upload-limit-default", json={"limit": 45}, headers=admin
    )
    assert r.status_code == 200 and r.json() == {"limit": 45}
    assert client.get(
        "/api/admin/settings/upload-limit-default", headers=admin
    ).json() == {"limit": 45}


def test_patch_global_default_out_of_range_400_user_message(client):
    admin = _admin(client)
    r = client.patch(
        "/api/admin/settings/upload-limit-default", json={"limit": 0}, headers=admin
    )
    assert r.status_code == 400
    assert r.headers.get("X-User-Message") == "1"
    assert "文档" in r.json()["detail"]


def test_patch_user_override_set_then_clear(client):
    admin = _admin(client)
    _user(client, "z00123456")
    uid = _user_id(client, admin, "z00123456")
    r = client.patch(
        f"/api/admin/users/{uid}/upload-limit", json={"limit": 4}, headers=admin
    )
    assert r.status_code == 200
    assert r.json() == {
        "id": uid, "username": "z00123456",
        "upload_limit": 4, "upload_limit_overridden": True,
    }
    rows = {r2["id"]: r2 for r2 in client.get("/api/admin/users", headers=admin).json()}
    assert rows[uid]["upload_limit"] == 4 and rows[uid]["upload_limit_overridden"] is True
    # 清覆盖 → 回落全局默认 20
    r = client.patch(
        f"/api/admin/users/{uid}/upload-limit", json={"limit": None}, headers=admin
    )
    assert r.status_code == 200
    assert r.json() == {
        "id": uid, "username": "z00123456",
        "upload_limit": 20, "upload_limit_overridden": False,
    }


def test_patch_user_override_missing_user_404(client):
    admin = _admin(client)
    r = client.patch(
        "/api/admin/users/user-nope/upload-limit", json={"limit": 5}, headers=admin
    )
    assert r.status_code == 404


def test_patch_user_override_out_of_range_400_user_message(client):
    admin = _admin(client)
    _user(client, "z00123456")
    uid = _user_id(client, admin, "z00123456")
    r = client.patch(
        f"/api/admin/users/{uid}/upload-limit", json={"limit": 999999}, headers=admin
    )
    assert r.status_code == 400
    assert r.headers.get("X-User-Message") == "1"
    assert "文档" in r.json()["detail"]


def test_non_admin_forbidden_on_all_config_endpoints(client):
    user = _user(client)
    assert client.get(
        "/api/admin/settings/upload-limit-default", headers=user
    ).status_code == 403
    assert client.patch(
        "/api/admin/settings/upload-limit-default", json={"limit": 30}, headers=user
    ).status_code == 403
    assert client.patch(
        "/api/admin/users/user-local/upload-limit", json={"limit": 5}, headers=user
    ).status_code == 403


def test_global_default_patch_reflected_in_user_usage(client):
    admin = _admin(client)
    _user(client, "z00123456")
    client.patch(
        "/api/admin/settings/upload-limit-default", json={"limit": 7}, headers=admin
    )
    rows = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    assert rows["z00123456"]["upload_limit"] == 7
    assert rows["z00123456"]["upload_limit_overridden"] is False
