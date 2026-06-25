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
