import re

import pytest
from fastapi.testclient import TestClient


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


def _auth(client, username="z00123456"):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post("/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_mask_key_never_exposes_full_short_key():
    from app.api.routes import _mask_key
    assert _mask_key("") == ""
    assert _mask_key("abc") == "…"        # ≤4 位整体隐去
    assert _mask_key("abcd") == "…"       # 恰好 4 位也不暴露
    masked = _mask_key("sk-secret123")    # 长 key 只露尾 4 位
    assert masked == "…t123" and "secret" not in masked


def test_get_defaults_masked(client):
    h = _auth(client)
    r = client.get("/api/me/model-settings", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank"}
    assert body["llm"]["has_key"] is False
    assert not body["llm"].get("api_key")   # 绝不回显完整 key


def test_put_then_get_masks_key(client):
    h = _auth(client)
    client.put("/api/me/model-settings", json={
        "llm": {"base_url": "https://u/v1", "api_key": "sk-secret123", "model": "m"}}, headers=h)
    body = client.get("/api/me/model-settings", headers=h).json()
    assert body["llm"]["base_url"] == "https://u/v1" and body["llm"]["model"] == "m"
    assert body["llm"]["has_key"] is True
    assert "secret" not in (body["llm"].get("key_hint") or "")
    assert not body["llm"].get("api_key")


def test_put_omit_key_preserves_clear_empties(client):
    h = _auth(client)
    client.put("/api/me/model-settings", json={
        "llm": {"base_url": "https://u/v1", "api_key": "sk-secret123", "model": "m"}}, headers=h)
    # 省略 api_key → 保留；改 model
    client.put("/api/me/model-settings", json={"llm": {"base_url": "https://u/v1", "model": "m2"}}, headers=h)
    body = client.get("/api/me/model-settings", headers=h).json()
    assert body["llm"]["model"] == "m2" and body["llm"]["has_key"] is True
    # 显式空串 → 清除 base_url
    client.put("/api/me/model-settings", json={"llm": {"base_url": ""}}, headers=h)
    body = client.get("/api/me/model-settings", headers=h).json()
    assert body["llm"]["base_url"] == ""


def test_test_endpoint_incomplete_returns_not_ok(client):
    h = _auth(client)
    r = client.post("/api/me/model-settings/test",
                    json={"service": "llm", "base_url": "", "model": ""}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is False


# --- 200 响应里的两个通道 -------------------------------------------------------
# 这个端点挂不上 X-User-Message 头(200 不是 4xx),所以出处由 schema 承载:
# `code` = 稳定枚举(文案在前端 vocabulary.ts),`error` = 诊断只进 console。
# 后端刻意不存中文:存了就绕开只扫 frontend/app 的界面词汇守卫。
# 前端是 deny-by-default 的,填错通道的后果是文案静默消失,两侧都要钉住。


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"service": "nope", "base_url": "x", "model": "x"}, "unknown_service"),
        ({"service": "llm", "base_url": "", "model": ""}, "missing_config"),
    ],
)
def test_precheck_failures_go_in_code_not_error(client, payload, expected):
    body = client.post("/api/me/model-settings/test", json=payload, headers=_auth(client)).json()
    assert body["code"] == expected
    # 诊断通道必须留空:这两条不是异常,没有原文可给排查。
    assert body["error"] == ""
    # 后端不得夹带中文文案——文案归前端,否则绕开界面词汇守卫。
    assert not re.search(r"[一-鿿]", body["code"])


def test_exception_path_fills_error_and_uses_generic_code(client, monkeypatch):
    """异常分支只给 code,绝不把 str(exc) 送上任何可展示通道。"""
    import app.api.routes as routes

    class Boom:
        def __init__(self, *a, **k): pass
        def chat_json(self, *a, **k): raise RuntimeError("upstream 10.0.0.7:8000 refused")

    monkeypatch.setattr("app.core.llm.OpenAICompatibleClient", Boom)
    monkeypatch.setattr(routes, "identity_repository", lambda: _StubRepo())
    body = client.post(
        "/api/me/model-settings/test",
        json={"service": "llm", "base_url": "http://x", "model": "m", "api_key": "k"},
        headers=_auth(client),
    ).json()
    assert body["ok"] is False
    assert body["code"] == "upstream_error"      # ← 用户看到的是这个 code 的中文映射
    assert "RuntimeError" in body["error"]       # ← 排查侧仍拿得到原文
    assert "10.0.0.7:8000" in body["error"]


def _install_status_service(monkeypatch, probe):
    from app.api import routes
    from app.api.deps import identity_repository
    from app.core.config import get_settings
    from app.services.model_status import ModelStatusService

    service = ModelStatusService(identity_repository(), get_settings(), probe=probe)
    monkeypatch.setattr(routes, "_model_status_service", lambda: service)
    return service


def test_model_service_status_requires_authentication(client):
    response = client.get("/api/me/model-services/status")
    assert response.status_code == 401


def test_model_service_status_returns_all_roles_without_probing_or_secrets(client, monkeypatch):
    calls = []
    _install_status_service(monkeypatch, lambda config: calls.append(config))
    headers = _auth(client)
    client.put("/api/me/model-settings", json={
        "llm": {
            "base_url": "https://private-provider.invalid/v1",
            "api_key": "key-private-secret",
            "model": "current-runtime-model",
        },
        "rerank": {
            "base_url": "https://rerank-provider.invalid/v1",
            "api_key": "rerank-private-secret",
            "model": "current-rerank-model",
        },
    }, headers=headers)

    response = client.get("/api/me/model-services/status", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert {item["service"] for item in body["services"]} == {
        "llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank", "embedding"
    }
    assert calls == []
    encoded = str(body)
    for private_value in (
        "key-private-secret",
        "rerank-private-secret",
        "private-provider.invalid",
        "rerank-provider.invalid",
    ):
        assert private_value not in encoded


def test_current_model_service_test_sanitizes_upstream_failure(client, monkeypatch):
    def fail(_config):
        raise RuntimeError("provider 10.0.0.8 rejected secret payload")

    _install_status_service(monkeypatch, fail)
    headers = _auth(client)
    client.put("/api/me/model-settings", json={
        "llm": {
            "base_url": "https://private-provider.invalid/v1",
            "api_key": "key-private-secret",
            "model": "current-runtime-model",
        },
    }, headers=headers)

    response = client.post("/api/me/model-services/llm/test", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["code"] == "upstream_error"
    encoded = str(body)
    for private_value in ("10.0.0.8", "secret", "provider", "private-provider.invalid"):
        assert private_value not in encoded


def test_current_model_service_test_succeeds_and_unknown_service_is_404(client, monkeypatch):
    _install_status_service(monkeypatch, lambda _config: None)
    headers = _auth(client)
    client.put("/api/me/model-settings", json={
        "llm": {
            "base_url": "https://private-provider.invalid/v1",
            "api_key": "key-private-secret",
            "model": "current-runtime-model",
        },
    }, headers=headers)

    success = client.post("/api/me/model-services/llm/test", headers=headers)
    unknown = client.post("/api/me/model-services/not-a-service/test", headers=headers)

    assert success.status_code == 200
    assert success.json()["status"] == "ok"
    assert unknown.status_code == 404


def test_put_model_settings_invalidates_status_for_primary_and_inheriting_variants(client, monkeypatch):
    _install_status_service(monkeypatch, lambda _config: None)
    headers = _auth(client)
    initial = {
        "base_url": "https://private-provider.invalid/v1",
        "api_key": "key-private-secret",
        "model": "current-runtime-model",
    }
    client.put("/api/me/model-settings", json={"llm": initial}, headers=headers)
    tested = client.post("/api/me/model-services/test-all", headers=headers)
    assert tested.status_code == 200
    assert all(
        item["status"] == "ok"
        for item in tested.json()["services"]
        if item["service"] in {"llm", "reasoning_llm", "rewrite_llm", "kg_llm"}
    )

    changed = client.put("/api/me/model-settings", json={
        "llm": {**initial, "model": "rotated-runtime-model"},
    }, headers=headers)
    snapshot = client.get("/api/me/model-services/status", headers=headers)

    assert changed.status_code == 200
    statuses = {item["service"]: item["status"] for item in snapshot.json()["services"]}
    assert {role: statuses[role] for role in ("llm", "reasoning_llm", "rewrite_llm", "kg_llm")} == {
        "llm": "untested",
        "reasoning_llm": "untested",
        "rewrite_llm": "untested",
        "kg_llm": "untested",
    }


class _StubRepo:
    def get_user_model_settings(self, _uid):
        return {}
