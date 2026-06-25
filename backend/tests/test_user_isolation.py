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


def _auth(client, username):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post("/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client):
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_user_cannot_see_or_access_others_notebook(client):
    a = _auth(client, "zhang00123456")
    b = _auth(client, "li00000042")
    nb = client.post("/api/notebooks", json={"name": "A's"}, headers=a).json()
    nb_id = nb["id"]
    # B 列表看不到 A 的
    assert client.get("/api/notebooks", headers=b).json() == []
    # B 直接访问 A 的 notebook 及子资源 → 404
    assert client.get(f"/api/notebooks/{nb_id}", headers=b).status_code == 404
    assert client.get(f"/api/notebooks/{nb_id}/sources", headers=b).status_code == 404
    # A 自己能访问
    assert client.get(f"/api/notebooks/{nb_id}", headers=a).status_code == 200


def test_regular_user_cannot_mark_base(client):
    a = _auth(client, "zhang00123456")
    nb_id = client.post("/api/notebooks", json={"name": "x"}, headers=a).json()["id"]
    r = client.post(f"/api/notebooks/{nb_id}/tier", json={"tier": "base"}, headers=a)
    assert r.status_code == 403


def test_admin_can_mark_base(client):
    admin = _auth_admin(client)
    nb_id = client.post("/api/notebooks", json={"name": "ref"}, headers=admin).json()["id"]
    r = client.post(f"/api/notebooks/{nb_id}/tier", json={"tier": "base"}, headers=admin)
    assert r.status_code == 200


def test_article_cross_user_blocked(client):
    a = _auth(client, "zhang00123456")
    b = _auth(client, "li00000042")
    nb = client.post("/api/notebooks", json={"name": "A"}, headers=a).json()["id"]
    create_resp = client.post(
        f"/api/notebooks/{nb}/articles",
        json={"title": "secret", "abstract": "s"},
        headers=a,
    )
    assert create_resp.status_code == 200, f"Article create failed: {create_resp.text}"
    art_id = create_resp.json()["id"]
    # B cannot research or delete A's article
    assert client.post(f"/api/articles/{art_id}/research", headers=b).status_code == 404
    assert client.delete(f"/api/articles/{art_id}", headers=b).status_code == 404
    # A can delete its own
    assert client.delete(f"/api/articles/{art_id}", headers=a).status_code == 204


def test_promotion_queue_admin_only(client):
    b = _auth(client, "li00000042")
    assert client.get("/api/promotion-queue", headers=b).status_code == 403
    assert client.post("/api/promotion-queue/bogus/approve", headers=b).status_code == 403
    assert client.post("/api/promotion-queue/bogus/reject", json={}, headers=b).status_code == 403
    admin = _auth_admin(client)
    assert client.get("/api/promotion-queue", headers=admin).status_code == 200


def test_global_config_write_admin_only(client):
    b = _auth(client, "li00000042")
    # write attempts by a regular user are forbidden
    assert client.post("/api/object-schemas", json={"object_type": "TestType"}, headers=b).status_code == 403
    assert client.post("/api/kg/concept-whitelist", json={"term": "XYZ"}, headers=b).status_code == 403
    # reads stay open
    assert client.get("/api/object-schemas", headers=b).status_code == 200


def test_streaming_ask_attributes_conversation_to_caller(client):
    a = _auth(client, "zhang00123456")
    nb = client.post("/api/notebooks", json={"name": "A"}, headers=a).json()["id"]
    # stream an ask as user A; consume the response
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "hello"}, headers=a)
    assert r.status_code == 200, r.text
    # the conversation created by the stream must be visible in A's own history
    convs = client.get(f"/api/notebooks/{nb}/conversations", headers=a).json()
    assert len(convs) >= 1, "streamed conversation should appear in caller's history"
