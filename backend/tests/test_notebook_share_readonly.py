# backend/tests/test_notebook_share_readonly.py
import uuid
import pytest
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
