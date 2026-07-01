# backend/tests/test_notebook_share_readonly.py
import uuid
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _mk_user(repo, uid, username=None):
    # users 表有 NOT NULL 无默认列(email/display_name/updated_at);漏了 INSERT OR IGNORE
    # 会静默吞掉整行 → 后续 notebook_members 的 FK 失败。故补齐必填列。
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, f"{uid}@t", uid, username or uid, "x", "user", _now(), _now()))


def _mk_nb(repo, owner="user-local", name="NB"):
    nb = f"nb-{uuid.uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (nb, name, "", "Semiconductor", "draft", owner, _now(), _now()))
    return nb


def test_membership_crud_and_read_access(repo):
    nb = _mk_nb(repo, owner="user-local")
    _mk_user(repo, "user-bob", "b00000001")
    assert repo.is_member(nb, "user-bob") is False
    assert repo.user_can_read_notebook(nb, "user-bob") is False   # 非成员非 owner
    assert repo.user_can_read_notebook(nb, "user-local") is True  # owner 恒可读
    repo.add_member(nb, "user-bob")
    assert repo.is_member(nb, "user-bob") is True
    assert repo.user_can_read_notebook(nb, "user-bob") is True    # 成员可读
    assert [m["username"] for m in repo.list_members(nb)] == ["b00000001"]
    repo.add_member(nb, "user-bob")  # 幂等
    assert len(repo.list_members(nb)) == 1
    repo.kick_all_members(nb)
    assert repo.list_members(nb) == []
    assert repo.user_can_read_notebook(nb, "user-bob") is False


def test_user_can_read_source_follows_membership(repo):
    nb = _mk_nb(repo, owner="user-local")
    _mk_user(repo, "user-bob")
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", ("src-1", nb, "S", "document", "s.md", "", 0, _now(), _now()))
    assert repo.user_can_read_source("src-1", "user-bob") is False
    repo.add_member(nb, "user-bob")
    assert repo.user_can_read_source("src-1", "user-bob") is True
    assert repo.user_can_read_source("src-1", "user-local") is True  # owner


# ---------------------------------------------------------------- Task 2
def test_unshare_kicks_members(repo):
    nb = _mk_nb(repo, owner="user-local")
    _mk_user(repo, "user-bob")
    repo.share_notebook(nb)
    repo.add_member(nb, "user-bob")
    repo.unshare_notebook(nb)
    assert repo.list_members(nb) == []


def test_preview_mode_readonly_for_large(repo, monkeypatch):
    nb = _mk_nb(repo, owner="user-local")
    with repo._write() as db:  # 造 2 个 knowledge_objects 触发超阈
        for i in range(2):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,created_at,updated_at) "
                       "VALUES (?,?,?,?,?)", (f"ko-{i}", nb, "concept", _now(), _now()))
    repo.settings.notebook_copy_max_rows = 1  # 逼超阈
    assert repo.shared_preview(nb)["mode"] == "readonly"
    repo.settings.notebook_copy_max_rows = 5000
    assert repo.shared_preview(nb)["mode"] == "copy"


# ---------------------------------------------------------------- Task 3
def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


# 读路由(成员应 200)与写路由(成员应 404)的枚举样本。完整清单见 spec §3.2。
READ_ROUTES = ["", "/analytics", "/sources", "/graph", "/search?q=x", "/conversations"]
WRITE_ROUTES = [("patch", ""), ("delete", ""), ("post", "/kg/rebuild"), ("post", "/tier"), ("post", "/share")]


def test_member_can_read_cannot_write(tmp_path, monkeypatch, repo):
    # repo fixture 与 client 共用同一 tmp DB(同 tmp_path)
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    bob_h = _login(client, "b00000002")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)   # bob 成为只读成员
    for suffix in READ_ROUTES:
        r = client.get(f"/api/notebooks/{nb}{suffix}", headers=bob_h)
        assert r.status_code == 200, (suffix, r.status_code)
    for method, suffix in WRITE_ROUTES:
        r = client.request(method.upper(), f"/api/notebooks/{nb}{suffix}", headers=bob_h,
                           json={} if method in ("post", "patch") else None)
        assert r.status_code == 404, (method, suffix, r.status_code)  # 非 owner→404 不泄露


# ---------------------------------------------------------------- Task 4
def test_member_reads_source_but_cannot_delete(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000003")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", ("src-x", nb, "S", "document", "s.md", "", 0, _now(), _now()))
    bob_h = _login(client, "b00000004")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)
    assert client.get("/api/sources/src-x", headers=bob_h).status_code == 200      # 成员可读
    assert client.delete("/api/sources/src-x", headers=bob_h).status_code == 404   # 成员不能删


def test_conversation_owner_is_creator_not_notebook_owner(repo):
    # 先建 owner 用户:notebooks.created_by 有 FK→users.id,漏建会触发 FOREIGN KEY 约束。
    _mk_user(repo, "user-owner"); _mk_user(repo, "user-mbr")
    nb = _mk_nb(repo, owner="user-owner")
    with repo._write() as db:
        db.execute("INSERT INTO conversations (id,notebook_id,title,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?)", ("cv-1", nb, "chat", "user-mbr", _now(), _now()))
    assert repo.conversation_owner("cv-1") == "user-mbr"   # 创建者,不是 notebook owner
