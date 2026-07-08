import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def _seed(repo):
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now))
        for nid, status in (("n1", "ready"), ("n2", "ready"), ("n3", "copying")):
            db.execute("INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?)", (nid, f"NB-{nid}", "u1", status, now, now))
        for sid in ("s1", "s2"):
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?)", (sid, "n1", sid, "md", now, now))
        db.execute("INSERT INTO reports (id,notebook_id,question,created_at,updated_at)"
                   " VALUES (?,?,?,?,?)", ("r1", "n1", "q?", now, now))
        db.execute("INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,?)", ("c1", "n1", "u1", now, now))


def test_list_user_notebooks_counts_and_excludes_copying(repo):
    _seed(repo)
    rows = repo.list_user_notebooks("u1")
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"n1", "n2"}                 # copying n3 排除
    assert by_id["n1"]["name"] == "NB-n1"
    assert by_id["n1"]["sources"] == 2
    assert by_id["n1"]["reports"] == 1
    assert by_id["n1"]["conversations"] == 1
    assert by_id["n2"]["sources"] == 0
    assert repo.list_user_notebooks("nobody") == []


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
    t = client.post("/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _auth_admin(client):
    t = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def test_user_notebooks_forbidden_for_regular(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/users/whoever/notebooks", headers=b).status_code == 403


def test_user_notebooks_lists_for_admin(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    uid = client.get("/api/me", headers=a).json()["id"]
    client.post("/api/notebooks", json={"name": "NB-One"}, headers=a)
    resp = client.get(f"/api/admin/users/{uid}/notebooks", headers=admin)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["name"] == "NB-One" and "sources" in r for r in rows)
