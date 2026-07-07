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
        # 两个用户(notebooks.created_by 是 FK→users(id),必须先建用户)
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        # u1: 2 个正常 notebook + 1 个 copying(应被排除);u2: 0
        for nid, status in (("n1", "ready"), ("n2", "ready"), ("n3", "copying")):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (nid, nid, "u1", status, now, now),
            )
        # u1 在 n1 下:2 个 source、1 个 report、1 个 conversation
        for sid in ("s1", "s2"):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (sid, "n1", sid, "md", now, now),
            )
        db.execute(
            "INSERT INTO reports (id,notebook_id,question,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("r1", "n1", "q?", now, now),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("c1", "n1", "u1", "2026-07-06T10:00:00", "2026-07-06T12:00:00"),
        )


def test_list_user_usage_counts(repo):
    _seed(repo)
    rows = {r["username"]: r for r in repo.list_user_usage()}
    # Should include u1, u2 and the auto-created admin user; check only the two we seeded
    assert "a00000001" in rows and "b00000002" in rows
    a = rows["a00000001"]
    assert a["id"] == "u1"
    assert a["role"] == "user"
    assert a["notebooks"] == 2          # copying 被排除
    assert a["sources"] == 2
    assert a["conversations"] == 1
    assert a["reports"] == 1
    assert a["last_active"] == "2026-07-06T12:00:00"
    b = rows["b00000002"]
    assert b["notebooks"] == 0 and b["sources"] == 0
    assert b["conversations"] == 0 and b["reports"] == 0
    assert b["last_active"] is None


def test_created_by_indexes_present_after_migration(repo):
    with repo._connect() as db:
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_notebooks_created_by" in names
    assert "idx_conversations_created_by" in names


def test_migration_2_runs_on_already_v1_db(tmp_path, monkeypatch):
    # 模拟被旧 _migration_1 迁移过、停在 user_version=1 且无 created_by 索引的既有库
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    r1 = SQLiteRepository(Settings())
    with r1._write() as db:
        db.execute("DROP INDEX IF EXISTS idx_notebooks_created_by")
        db.execute("DROP INDEX IF EXISTS idx_conversations_created_by")
        db.execute("PRAGMA user_version = 1")
    # 重新打开同一库:_migrate 应发现 1 < 2、跑 _migration_2、重建两个索引并盖章 2
    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        names = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        ver = db.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2
    assert "idx_notebooks_created_by" in names
    assert "idx_conversations_created_by" in names


# ===== API Tests for GET /api/admin/users =====

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
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_admin_users_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/users", headers=b).status_code == 403


def test_admin_users_lists_username_and_counts(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    client.post("/api/notebooks", json={"name": "A1"}, headers=a)
    client.post("/api/notebooks", json={"name": "A2"}, headers=a)
    resp = client.get("/api/admin/users", headers=admin)
    assert resp.status_code == 200
    rows = {r["username"]: r for r in resp.json()}
    assert "admin" in rows and "z00123456" in rows
    assert rows["z00123456"]["notebooks"] == 2
    assert rows["z00123456"]["role"] == "user"
    # 展示用户名,但内部 id 仍是 user-<hex>(未统一)
    assert rows["z00123456"]["id"].startswith("user-")
