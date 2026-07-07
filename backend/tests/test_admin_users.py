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
