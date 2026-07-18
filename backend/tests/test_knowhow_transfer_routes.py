# backend/tests/test_knowhow_transfer_routes.py
# 认证沿用 tests/test_notebook_share_readonly.py 的 _login 样板：真实注册+登录拿 Bearer，
# repo fixture 与 app 共享同一 tmp DB（autouse conftest 清 repository() lru_cache）。
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

COLUMNS = [{"name": "违例类型", "role": "anchor"}, {"name": "现象识别", "role": "procedure"}]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def client(repo):
    from app.main import app
    return TestClient(app)

def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}

def _table(repo, nb):
    tid = repo.create_knowhow_table(nb, "T", "d", COLUMNS, created_by="")
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲"})
    return tid

def test_copy_endpoint_creates_table_in_target(client, repo):
    h = _login(client, "a00000001")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    tid = _table(repo, src)
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": dst, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    new_tid = resp.json()["new_table_id"]
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst

def test_transfer_to_same_notebook_rejected(client, repo):
    h = _login(client, "a00000002")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    tid = _table(repo, src)
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": src, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 400


def test_move_source_cleanup_failure_returns_409_with_new_table_id(client, repo, monkeypatch):
    """A3 评审附加需求：move 时复制已提交、但删源失败——不能让用户拿到裸 500
    去盲目重试(会在目标侧越堆越多重复副本)。故障注入删源这一步(不依赖真实
    投影/embedder：patch 整个类的 delete_knowhow_table，_table() 建的表从未
    投影，hidden_source_id 恒为 None，走的正是"只删源"这条无条件必经路径)。
    断言：状态码 409 + 结构化 code + new_table_id 在目标侧可解析 + 源表原封
    不动地还在——"重复不丢失"必须被诚实地捅给调用方，而不是悄悄吞掉。"""

    def _boom_delete(self, table_id):
        raise RuntimeError("simulated delete_knowhow_table failure")

    monkeypatch.setattr(SQLiteRepository, "delete_knowhow_table", _boom_delete)

    h = _login(client, "a00000003")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    tid = _table(repo, src)

    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": dst, "mode": "move"},
        headers=h,
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "source_cleanup_failed"
    new_tid = detail["new_table_id"]
    assert new_tid
    # 副本已在目标侧、可解析
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst
    # 源表仍在——删源失败绝不能连带丢了源(duplicate-not-loss)
    assert repo.get_knowhow_table(tid)["notebook_id"] == src
