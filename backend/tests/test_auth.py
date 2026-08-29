import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")  # 本套验证真实登录
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def test_register_returns_token_and_user(client):
    r = client.post("/api/auth/register", json={"username": "z12345678", "password": "pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]
    assert body["user"]["username"] == "z12345678"
    assert body["user"]["role"] == "user"


def test_register_invalid_username_400(client):
    r = client.post("/api/auth/register", json={"username": "bad", "password": "pw"})
    assert r.status_code == 400


def test_register_empty_password_400(client):
    r = client.post("/api/auth/register", json={"username": "z00123456", "password": ""})
    assert r.status_code == 400


def test_register_duplicate_400(client):
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    r = client.post("/api/auth/register", json={"username": "z00123456", "password": "x"})
    assert r.status_code == 400


def test_login_and_me(client):
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    r = client.post("/api/auth/login", json={"username": "Z00123456", "password": "pw"})
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "z00123456"


def test_login_wrong_password_401(client):
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    r = client.post("/api/auth/login", json={"username": "z00123456", "password": "nope"})
    assert r.status_code == 401


def test_me_without_token_401_when_required(client):
    assert client.get("/api/me").status_code == 401


def test_logout_invalidates_token(client):
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    token = client.post("/api/auth/login", json={"username": "z00123456", "password": "pw"}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=h).status_code == 204
    assert client.get("/api/me", headers=h).status_code == 401


def test_admin_login_with_seeded_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"
