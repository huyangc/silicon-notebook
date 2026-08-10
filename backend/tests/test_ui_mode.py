"""用户级界面模式偏好("auto"|"advanced")——user_profiles.ui_mode。

覆盖:
  * 全新库 `_user_profile` 的 ui_mode 无 profile 行/NULL 时回退 "auto"。
  * IdentityStore.set_user_ui_mode 写入后 current_user()/GET /me 能读回。
  * PATCH /me/ui-mode:合法值更新、非法值 422、self-serve(不需要 admin)。
  * SQLite v44 -> v45 升级路径:旧库补建 ui_mode 列且不丢已有行。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.request_context import set_request_user
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
# 1. 全新库回退 "auto"
# --------------------------------------------------------------------------- #
def test_fresh_repository_defaults_ui_mode_to_auto(tmp_path):
    ident = _identity(_repo(tmp_path))
    profile = ident.current_user()
    assert profile.ui_mode == "auto"


def test_user_profile_falls_back_to_auto_when_profile_row_missing():
    from app.repositories.sqlite.identity_store import IdentityStore

    user_row = {"id": "u1", "email": "u1@x", "display_name": "U1", "role": "user"}
    assert IdentityStore._user_profile(user_row, None).ui_mode == "auto"


# --------------------------------------------------------------------------- #
# 2. 写方法 + current_user() 读回
# --------------------------------------------------------------------------- #
def test_set_user_ui_mode_round_trips(tmp_path):
    repo = _repo(tmp_path)
    ident = _identity(repo)
    created = ident.create_user("z00000001", "pw")

    updated = ident.set_user_ui_mode(created.id, "advanced")
    assert updated.ui_mode == "advanced"

    reread = ident.authenticate_user("z00000001", "pw")
    assert reread.ui_mode == "advanced"

    back = ident.set_user_ui_mode(created.id, "auto")
    assert back.ui_mode == "auto"
    assert ident.authenticate_user("z00000001", "pw").ui_mode == "auto"


def test_set_user_ui_mode_rejects_invalid_value(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000002", "pw")
    with pytest.raises(ValueError):
        ident.set_user_ui_mode(created.id, "bogus")


# --------------------------------------------------------------------------- #
# 3. API: PATCH /me/ui-mode
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


def _register_and_login(client: TestClient, username: str = "z00123456") -> str:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    r = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    return r.json()["token"]


def test_get_me_defaults_ui_mode_auto(client):
    token = _register_and_login(client)
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["ui_mode"] == "auto"


def test_patch_ui_mode_updates_and_persists(client):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.patch("/api/me/ui-mode", json={"ui_mode": "advanced"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["ui_mode"] == "advanced"

    me = client.get("/api/me", headers=headers)
    assert me.json()["ui_mode"] == "advanced"

    back = client.patch("/api/me/ui-mode", json={"ui_mode": "auto"}, headers=headers)
    assert back.status_code == 200
    assert back.json()["ui_mode"] == "auto"


def test_patch_ui_mode_rejects_invalid_value_422(client):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch("/api/me/ui-mode", json={"ui_mode": "bogus"}, headers=headers)
    assert r.status_code == 422


def test_patch_ui_mode_requires_auth(client):
    r = client.patch("/api/me/ui-mode", json={"ui_mode": "advanced"})
    assert r.status_code == 401


def test_patch_ui_mode_is_self_serve_not_admin_gated(client):
    """非 admin 普通用户也能改自己的 ui_mode(与 set_user_role 等 admin-only 端点不同)。"""
    token = _register_and_login(client, username="z00000003")
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["role"] == "user"
    r = client.patch(
        "/api/me/ui-mode",
        json={"ui_mode": "advanced"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["ui_mode"] == "advanced"


# --------------------------------------------------------------------------- #
# 4. SQLite v44 -> v45 升级路径
# --------------------------------------------------------------------------- #
def test_sqlite_migration_v44_to_v45_adds_ui_mode_column(tmp_path, monkeypatch):
    from app.repositories.sqlite import migrations as migrations_module

    # 先把 SCHEMA_VERSION 钉在 44,建一个"旧库"(ui_mode 列缺失)。
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 44)
    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None, event_log_enabled=False, llm_log_enabled=False,
        )
    )
    ident = _identity(repo)
    created = ident.create_user("z00000004", "pw")

    with repo._connect() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(user_profiles)").fetchall()}
        assert "ui_mode" not in cols
        assert db.execute("PRAGMA user_version").fetchone()[0] == 44

    # 恢复真实 SCHEMA_VERSION(45),对同一个库文件重新跑迁移。
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 45)
    applied = repo._migrator.migrate()
    assert applied == [45]

    with repo._connect() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(user_profiles)").fetchall()}
        assert "ui_mode" in cols
        assert db.execute("PRAGMA user_version").fetchone()[0] == 45

    # 升级前创建的用户仍在,且回退 "auto"(升级不会为存量行填充非 NULL 默认值)。
    reread = ident.authenticate_user("z00000004", "pw")
    assert reread.id == created.id
    assert reread.ui_mode == "auto"
