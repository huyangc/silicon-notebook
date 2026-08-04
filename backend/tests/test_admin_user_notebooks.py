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
        for user_id, username in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute("INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?,?,?)", (user_id, f"{user_id}@x", user_id.upper(), "user", "active", username, now, now))
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
        db.execute("INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,?)", ("c2", "n1", "u2", now, now))
        for job_id, conversation_id, creator, question, status in (
            ("j1", "c1", "u1", "first?", "completed"),
            ("j2", "c1", "u1", "second?", "failed"),
            ("j3", "c2", "u2", "shared?", "cancelled"),
        ):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,conversation_id,created_by,mode,question,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, "n1", conversation_id, creator, "chunk", question, status, now, now),
            )


def test_source_count_excludes_hidden_projection_sources(repo):
    """表头的「来源 N」必须与展开后的清单是同一个口径(F4)。

    memory / knowhow 投影行是隐藏合成源:``list_sources_page``(左栏展开用)与来源
    页签都带 VISIBLE_SOURCE_TYPES_PREDICATE 把它们排除。这个计数曾经是裸
    ``COUNT(*)``,于是存过一条 Memory 或建过一张 Knowhow 表的用户,表头写「来源 3」
    而展开的清单只有 1 条——同一屏自相矛盾。
    """
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now))
        db.execute("INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?)", ("n1", "NB-n1", "u1", "ready", now, now))
        for sid, source_type in (
            ("s-real", "pdf"), ("s-mem", "memory"), ("s-know", "knowhow"),
        ):
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?)", (sid, "n1", sid, source_type, now, now))

    rows = repo.list_user_notebooks("u1")
    assert rows[0]["sources"] == 1
    # 与左栏展开用的那条路径逐字对齐(它是这个数字的「展开形态」)。
    assert repo.list_sources_page("n1", 0, 50).total_count == 1


def test_notebook_exists_for_owner_matches_the_activity_owned_predicate(repo):
    """归属判定是**一行**的问题(F8);谓词必须与 list_user_activity 的 owned 分支
    逐字相同——包括 ``status != 'copying'`` 那一半(深拷贝中的库不算)。"""
    _seed(repo)
    assert repo.notebook_exists_for_owner("n1", "u1") is True
    assert repo.notebook_exists_for_owner("n3", "u1") is False   # copying
    assert repo.notebook_exists_for_owner("n1", "u2") is False   # 别人的
    assert repo.notebook_exists_for_owner("nope", "u1") is False  # 不存在
    # 与 list_user_notebooks 的口径一致(那份清单同样排除 copying)。
    assert {r["id"] for r in repo.list_user_notebooks("u1")} == {"n1", "n2"}


def test_list_user_notebooks_counts_and_excludes_copying(repo):
    _seed(repo)
    rows = repo.list_user_notebooks("u1")
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"n1", "n2"}                 # copying n3 排除
    assert by_id["n1"]["name"] == "NB-n1"
    assert by_id["n1"]["sources"] == 2
    assert by_id["n1"]["reports"] == 1
    assert by_id["n1"]["conversations"] == 2
    assert by_id["n1"]["questions"] == 2
    assert by_id["n2"]["sources"] == 0
    # 明细是 owner-only 库清单，不是用户总提问的完整分解；u2 在共享库 n1 的
    # 提问计入用户总览，但这里刻意不把 n1 伪装成 u2 拥有的笔记本。
    assert repo.list_user_notebooks("u2") == []
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
