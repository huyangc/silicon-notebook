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
