"""Agentic Memory P3(T6)——``PATCH /me/search-profile`` 与 ``GET
/system/config`` 的 ``user_search_profile_enabled`` 字段。

覆盖:
  * ``GET /system/config`` 下发 ``user_search_profile_enabled``,跟随
    ``USER_SEARCH_PROFILE_ENABLED`` 部署开关。
  * ``PATCH /me/search-profile`` 四态:成功写入并被 ``GET /me`` 读回、
    未出现在请求体里的字段不受影响而传 null 的字段被清空、非法值 422、
    总闸关闭时 409、未认证 401、self-serve(非 admin 也能改自己的)。
  * ``IdentityStore.set_user_search_profile`` 的读-改-写并发回归:两个线程
    分别以 ``origin="user"``/``origin="job"`` 同时写不同字段,两个字段都要
    留下来(不是「同一份读到的旧文档互相覆盖」的 lost-update)。
"""
from __future__ import annotations

import threading

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
# 1. GET /system/config: user_search_profile_enabled
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


def test_system_config_defaults_search_profile_enabled_true(client):
    token = _register_and_login(client)
    r = client.get("/api/system/config", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["user_search_profile_enabled"] is True


def test_system_config_reflects_disabled_search_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_SEARCH_PROFILE_ENABLED", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    disabled_client = TestClient(create_app())

    token = _register_and_login(disabled_client)
    r = disabled_client.get(
        "/api/system/config", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert r.json()["user_search_profile_enabled"] is False


# --------------------------------------------------------------------------- #
# 2. PATCH /me/search-profile — 四态
# --------------------------------------------------------------------------- #
def test_patch_search_profile_writes_and_is_read_back_by_get_me(client):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.patch(
        "/api/me/search-profile",
        json={"answer_language": "zh", "domain_terms": ["PPA", "IRR"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()["search_profile"]
    assert body["fields"]["answer_language"]["value"] == "zh"
    assert body["fields"]["answer_language"]["origin"] == "user"
    assert body["fields"]["domain_terms"]["value"] == ["PPA", "IRR"]

    me = client.get("/api/me", headers=headers)
    assert me.json()["search_profile"]["fields"]["answer_language"]["value"] == "zh"


def test_omitted_field_is_untouched_while_null_field_is_cleared(client):
    token = _register_and_login(client, username="z00000010")
    headers = {"Authorization": f"Bearer {token}"}

    client.patch(
        "/api/me/search-profile",
        json={"answer_language": "zh", "answer_shape": "prose"},
        headers=headers,
    )

    # answer_detail omitted entirely -> both existing fields survive.
    r = client.patch(
        "/api/me/search-profile",
        json={"answer_detail": "concise"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    fields = r.json()["search_profile"]["fields"]
    assert fields["answer_language"]["value"] == "zh"
    assert fields["answer_shape"]["value"] == "prose"
    assert fields["answer_detail"]["value"] == "concise"

    # answer_shape explicitly cleared (sent as null) -> deleted, others stay.
    r2 = client.patch(
        "/api/me/search-profile",
        json={"answer_shape": None},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    fields2 = r2.json()["search_profile"]["fields"]
    assert "answer_shape" not in fields2
    assert fields2["answer_language"]["value"] == "zh"
    assert fields2["answer_detail"]["value"] == "concise"


def test_patch_search_profile_rejects_invalid_enum_value_422(client):
    token = _register_and_login(client, username="z00000011")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        "/api/me/search-profile",
        json={"answer_language": "fr"},
        headers=headers,
    )
    assert r.status_code == 422


def test_patch_search_profile_rejects_too_many_domain_terms_422(client):
    token = _register_and_login(client, username="z00000012")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        "/api/me/search-profile",
        json={"domain_terms": [f"t{i}" for i in range(11)]},
        headers=headers,
    )
    assert r.status_code == 422


def test_patch_search_profile_rejects_unknown_field_422(client):
    token = _register_and_login(client, username="z00000013")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.patch(
        "/api/me/search-profile",
        json={"not_a_real_field": "x"},
        headers=headers,
    )
    assert r.status_code == 422


def test_patch_search_profile_is_409_when_globally_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_SEARCH_PROFILE_ENABLED", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    disabled_client = TestClient(create_app())

    token = _register_and_login(disabled_client)
    r = disabled_client.patch(
        "/api/me/search-profile",
        json={"answer_language": "zh"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409, r.text
    # user_error() must be the origin of this 409 -- deps.USER_MESSAGE_HEADER
    # marks detail as ready-to-display Chinese user copy (AGENTS.md "User-facing copy"
    # 文案" red line), not a raw str(exc) a caller forgot to translate.
    assert r.headers.get("X-User-Message") == "1"


def test_patch_search_profile_requires_auth(client):
    r = client.patch("/api/me/search-profile", json={"answer_language": "zh"})
    assert r.status_code == 401


def test_patch_search_profile_is_self_serve_not_admin_gated(client):
    token = _register_and_login(client, username="z00000014")
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["role"] == "user"
    r = client.patch(
        "/api/me/search-profile",
        json={"answer_detail": "detailed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["search_profile"]["fields"]["answer_detail"]["value"] == "detailed"


# --------------------------------------------------------------------------- #
# 3. IdentityStore.set_user_search_profile — 读-改-写并发回归
# --------------------------------------------------------------------------- #
def test_concurrent_user_and_job_writes_both_survive(tmp_path):
    """用户编辑(origin="user")与后台归纳(origin="job")并发写不同字段时,
    两个字段都必须留下——不是「一方读到的旧文档整体覆盖另一方刚提交的
    字段」的 lost-update。读-改-写钉在同一个 write() 事务里是这条测试要
    证明的东西。"""
    repo = _repo(tmp_path)
    ident = _identity(repo)
    created = ident.create_user("z00000099", "pw")

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _write_user():
        try:
            barrier.wait(timeout=5)
            ident.set_user_search_profile(
                created.id, {"answer_shape": "table_first"}, origin="user"
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    def _write_job():
        try:
            barrier.wait(timeout=5)
            ident.set_user_search_profile(
                created.id, {"answer_language": "zh"}, origin="job"
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=_write_user), threading.Thread(target=_write_job)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors

    reread = ident.authenticate_user("z00000099", "pw")
    fields = reread.search_profile["fields"]
    assert fields["answer_shape"]["value"] == "table_first"
    assert fields["answer_language"]["value"] == "zh"


def test_set_user_search_profile_rejects_unknown_user(tmp_path):
    ident = _identity(_repo(tmp_path))
    with pytest.raises(KeyError):
        ident.set_user_search_profile("nope", {"answer_language": "zh"}, origin="user")


def test_set_user_search_profile_rejects_invalid_origin(tmp_path):
    ident = _identity(_repo(tmp_path))
    created = ident.create_user("z00000098", "pw")
    with pytest.raises(ValueError):
        ident.set_user_search_profile(created.id, {"answer_language": "zh"}, origin="robot")
