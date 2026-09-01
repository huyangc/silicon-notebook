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
        db.execute("INSERT INTO reports (id,notebook_id,question,created_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?)", ("r1", "n1", "q?", "u1", now, now))
        # 共享笔记本里另一位可写成员(u2)建的报告:表头必须**不**把它算进 owner(u1)
        # 的计数(P2-2,codex 第 3 轮),否则「报告 N」与活动流展开的条目数对不上。
        db.execute("INSERT INTO reports (id,notebook_id,question,created_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?)", ("r2", "n1", "shared?", "u2", now, now))
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
    逐字相同——包括 ``NOTEBOOK_LIVE_SQL`` 那一半(深拷贝中的库不算)。"""
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
    # r2 由共享成员 u2 建,不计入 owner(u1)的报告数(P2-2)。
    assert by_id["n1"]["reports"] == 1
    assert by_id["n1"]["conversations"] == 2
    assert by_id["n1"]["questions"] == 2
    assert by_id["n2"]["sources"] == 0
    # 明细是 owner-only 库清单，不是用户总提问的完整分解；u2 在共享库 n1 的
    # 提问计入用户总览，但这里刻意不把 n1 伪装成 u2 拥有的笔记本。
    assert repo.list_user_notebooks("u2") == []
    assert repo.list_user_notebooks("nobody") == []


def test_reports_count_matches_activity_stream_entries(repo):
    """P2-2(codex 第 3 轮 P2):左栏「报告 N」必须与中栏活动流真正展开的报告条目数
    一致——两者都必须按 created_by 收窄。此前 ``list_user_notebooks`` 的报告聚合
    没有这条谓词(``questions`` 那条本来就有),于是共享笔记本里别的可写成员建的
    报告被计进了 owner 的表头,点进去却在活动流里看不到对应条目。"""
    _seed(repo)
    rows = repo.list_user_notebooks("u1")
    reports_count = {r["id"]: r["reports"] for r in rows}["n1"]

    activity = repo.list_user_activity("u1", notebook_id="n1", limit=50)
    report_ids = {item["id"] for item in activity["items"] if item["type"] == "report"}
    assert report_ids == {"r1"}  # r2(u2 建的)不在这位用户自己的活动流里
    assert len(report_ids) == reports_count


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


def test_user_notebooks_forbidden_for_other_regular_user(client):
    """self-or-admin(P1-2,codex 评审第 1 轮):放宽后「读别人的」必须依旧拒绝。

    此前这里断言的是 ``/admin/users/whoever/notebooks``(一个压根不存在的
    user_id)返回 403——放宽成 self-or-admin 之后那条断言仍然为真,但不再是
    真正要守的那条线:它测不出「a 能不能读到 b 的笔记本」。改成两个真实注册用户,
    a 尝试读 b 的笔记本,才是本次改动唯一要挡住的路径。
    """
    a = _auth(client, "z00123456")
    b = _auth(client, "z00654321")
    uid_b = client.get("/api/me", headers=b).json()["id"]
    assert client.get(f"/api/admin/users/{uid_b}/notebooks", headers=a).status_code == 403


def test_user_notebooks_allowed_for_self(client):
    """P1-2:普通用户读**自己的** user_id 必须 200——这是 P1-2 要修的那个 bug
    本身(活动视图左栏复用这个端点展开自己的笔记本清单,此前「仅 admin」会让
    普通用户看自己的活动时被 403 换成一整块权限错误页)。"""
    a = _auth(client, "z00123456")
    uid_a = client.get("/api/me", headers=a).json()["id"]
    client.post("/api/notebooks", json={"name": "NB-Self"}, headers=a)
    resp = client.get(f"/api/admin/users/{uid_a}/notebooks", headers=a)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["name"] == "NB-Self" for r in rows)


def test_user_notebooks_lists_for_admin(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    uid = client.get("/api/me", headers=a).json()["id"]
    client.post("/api/notebooks", json={"name": "NB-One"}, headers=a)
    resp = client.get(f"/api/admin/users/{uid}/notebooks", headers=admin)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["name"] == "NB-One" and "sources" in r for r in rows)
