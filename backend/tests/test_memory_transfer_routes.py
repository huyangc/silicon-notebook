# backend/tests/test_memory_transfer_routes.py
# 同 A4：真实 _login，repo fixture 与 app 共享同一 tmp DB。
#
# B3 评审前车之鉴（knowhow transfer route 任务）：全部用例都以 owner 身份跑，
# 「swapped guard」这种接线错误可以全绿溜过去。这里 memory 没有 knowhow 那种
# 路径参数 + 读/写守卫三元表达式（transfer 的鉴权整个在 service 层：目标
# notebook 恒需 owner，逐条 memory 靠 memory_for_user 的 created_by 过滤），
# 所以对应的风险点是「路由是否老实地把 user.id 转发给 service、老实地复用
# _memory_call 的异常映射，而不是自己另起一套」。下面覆盖：目标 notebook 非
# 本人 → 404 且目标侧确无新建；别人的 memory id → 单条 ok=False 而非泄露/500；
# 未 confirmed 的候选 → 单条 ok=False，整批仍 200；copy/move 两种模式的正向
# 路径。
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
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


def _uid(client, headers):
    return client.get("/api/me", headers=headers).json()["id"]


def _seeded_service(repo):
    # 同 B1/B2 的夹具惯用法：直接拿真实 facade 背后的 memory_service，关掉
    # embedding/KG 后台调度，让候选/确认走同步路径，测试聚焦在路由层。
    service = repo._runtime.memory_service
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None
    return service


def _confirmed_memory(repo, nb, uid, title="T", content="B"):
    service = _seeded_service(repo)
    cand = service.create_candidate(nb, uid, None, f"req-{title}", title, content, [], "task")
    return service.confirm(cand.id, uid)


def _candidate_memory(repo, nb, uid, title="C", content="B"):
    service = _seeded_service(repo)
    return service.create_candidate(nb, uid, None, f"req-{title}", title, content, [], "task")


# --------------------------------------------------------------------------
# 正向路径：两种模式
# --------------------------------------------------------------------------

def test_transfer_endpoint_copies(client, repo):
    h = _login(client, "a00000001")
    uid = _uid(client, h)
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    mem = _confirmed_memory(repo, src, uid)

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [mem.id], "target_notebook_id": dst, "mode": "copy", "extract_kg": False},
        headers=h,
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["ok"] is True
    assert result["status"] == "copied"
    assert result["error"] is None
    assert result["source_id"] == mem.id
    new_id = result["new_id"]
    assert new_id and new_id != mem.id

    # 源仍在（copy 不动源）
    src_check = client.get(f"/api/memories/{mem.id}", headers=h)
    assert src_check.status_code == 200
    assert src_check.json()["notebook_id"] == src
    # 副本确实落在目标 notebook
    dst_check = client.get(f"/api/memories/{new_id}", headers=h)
    assert dst_check.status_code == 200
    assert dst_check.json()["notebook_id"] == dst


def test_transfer_endpoint_moves(client, repo):
    h = _login(client, "a00000002")
    uid = _uid(client, h)
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    mem = _confirmed_memory(repo, src, uid)

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [mem.id], "target_notebook_id": dst, "mode": "move", "extract_kg": False},
        headers=h,
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["ok"] is True
    assert result["status"] == "moved"
    assert result["error"] is None
    new_id = result["new_id"]

    # 源已被删除（move 会删源）
    gone = client.get(f"/api/memories/{mem.id}", headers=h)
    assert gone.status_code == 404
    moved = client.get(f"/api/memories/{new_id}", headers=h)
    assert moved.status_code == 200
    assert moved.json()["notebook_id"] == dst


# --------------------------------------------------------------------------
# 必需覆盖 1：目标 notebook 非本人 owner → 404，且目标侧确无新建
# --------------------------------------------------------------------------

def test_target_notebook_not_owned_returns_404(client, repo):
    owner_h = _login(client, "a00000010")
    owner_uid = _uid(client, owner_h)
    src = client.post("/api/notebooks", json={"name": "src"}, headers=owner_h).json()["id"]
    mem = _confirmed_memory(repo, src, owner_uid)

    other_h = _login(client, "b00000011")
    other_nb = client.post("/api/notebooks", json={"name": "other"}, headers=other_h).json()["id"]

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [mem.id], "target_notebook_id": other_nb, "mode": "copy", "extract_kg": False},
        headers=owner_h,
    )

    assert resp.status_code == 404, resp.text
    # 目标 notebook（属于 other_h）里什么都没被创建
    listing = client.get(f"/api/notebooks/{other_nb}/memories", headers=other_h)
    assert listing.status_code == 200
    assert listing.json()["items"] == []
    # 源也原封不动
    src_check = client.get(f"/api/memories/{mem.id}", headers=owner_h)
    assert src_check.status_code == 200
    assert src_check.json()["notebook_id"] == src


# --------------------------------------------------------------------------
# 必需覆盖 2：列表里混入别人的 memory id → 该条 ok=False（不是泄露，不是 500），
# 且目标侧没有产生任何副本
# --------------------------------------------------------------------------

def test_someone_elses_memory_id_is_not_leaked_or_copied(client, repo):
    alice_h = _login(client, "a00000020")
    alice_dst = client.post("/api/notebooks", json={"name": "adst"}, headers=alice_h).json()["id"]

    bob_h = _login(client, "b00000021")
    bob_uid = _uid(client, bob_h)
    bob_nb = client.post("/api/notebooks", json={"name": "bnb"}, headers=bob_h).json()["id"]
    bob_mem = _confirmed_memory(repo, bob_nb, bob_uid)

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [bob_mem.id], "target_notebook_id": alice_dst, "mode": "copy", "extract_kg": False},
        headers=alice_h,
    )

    # 目标 notebook 是 alice 自己的 → 批量调用本身放行(200)；问题条目单独 ok=False
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["new_id"] is None
    assert result["error"]
    assert result["source_id"] == bob_mem.id

    # 目标 notebook 里没有出现任何副本
    listing = client.get(f"/api/notebooks/{alice_dst}/memories", headers=alice_h)
    assert listing.json()["items"] == []
    # bob 的源没有被动过
    still_there = client.get(f"/api/memories/{bob_mem.id}", headers=bob_h)
    assert still_there.status_code == 200
    assert still_there.json()["notebook_id"] == bob_nb


# --------------------------------------------------------------------------
# 必需覆盖 3：未 confirmed 的候选 → ok=False 且 error 明确，整批仍 200
# --------------------------------------------------------------------------

def test_non_confirmed_memory_returns_ok_false_batch_still_200(client, repo):
    h = _login(client, "a00000030")
    uid = _uid(client, h)
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    cand = _candidate_memory(repo, src, uid)

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [cand.id], "target_notebook_id": dst, "mode": "copy", "extract_kg": False},
        headers=h,
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["new_id"] is None
    assert "confirmed" in result["error"].lower()

    listing = client.get(f"/api/notebooks/{dst}/memories", headers=h)
    assert listing.json()["items"] == []


# --------------------------------------------------------------------------
# 批内混合：一条会成功、一条是别人的 → 整批仍 200，且各自结果互不影响
# （直接落地 service 层已验证的"批不因单条失败而整体报错"到 HTTP 层）
# --------------------------------------------------------------------------

def test_batch_mixes_ok_and_failed_items_without_500(client, repo):
    alice_h = _login(client, "a00000040")
    alice_uid = _uid(client, alice_h)
    src = client.post("/api/notebooks", json={"name": "src"}, headers=alice_h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=alice_h).json()["id"]
    mine = _confirmed_memory(repo, src, alice_uid, title="mine")

    bob_h = _login(client, "b00000041")
    bob_uid = _uid(client, bob_h)
    bob_nb = client.post("/api/notebooks", json={"name": "bnb"}, headers=bob_h).json()["id"]
    bobs = _confirmed_memory(repo, bob_nb, bob_uid, title="bobs")

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [mine.id, bobs.id], "target_notebook_id": dst, "mode": "copy", "extract_kg": False},
        headers=alice_h,
    )

    assert resp.status_code == 200, resp.text
    results = {r["source_id"]: r for r in resp.json()["results"]}
    assert results[mine.id]["ok"] is True
    assert results[mine.id]["status"] == "copied"
    assert results[bobs.id]["ok"] is False
    assert results[bobs.id]["status"] == "failed"
    assert results[bobs.id]["new_id"] is None


# --------------------------------------------------------------------------
# Final-fix-wave Important 5: memory_ids must be bounded, matching its sibling
# MemoryBulkDeleteRequest.memory_ids (max_length=200) and answer_memory_links
# (200 cap). Transfer is far more expensive per item than delete (write txn +
# vector copy + potential LLM ingest + delete), so an unbounded body can
# occupy a threadpool worker indefinitely. Pydantic rejects with 422 before
# the route body (and therefore the service layer) ever runs.
# --------------------------------------------------------------------------

def test_empty_memory_ids_rejected_with_422(client, repo):
    h = _login(client, "a00000050")
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [], "target_notebook_id": dst, "mode": "copy"},
        headers=h,
    )

    assert resp.status_code == 422, resp.text


def test_over_200_memory_ids_rejected_with_422(client, repo):
    h = _login(client, "a00000051")
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]

    resp = client.post(
        "/api/memories/transfer",
        json={
            "memory_ids": [f"mem-{i}" for i in range(201)],
            "target_notebook_id": dst,
            "mode": "copy",
        },
        headers=h,
    )

    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# round 10 P1-B：error_code 必须在 HTTP JSON 里原样透传——这条端点没有
# response_model（memory_routes.py 的 transfer_memories 直接
# `return {"results": results}`，service 返回的 dict 原样序列化），理论上不
# 该有任何字段被悄悄丢掉；这条用例确认真走一遍 HTTP 也确实如此，不只是
# service 层直接 Python 调用能看到这个字段。并发编辑通过 monkeypatch 类级
# `MemoryStore.create_copy_with_initial_revision` 注入——同上面 A4 knowhow
# 路由测试头部注释的理由：app 的 repository() 和这里的 repo 夹具是两个不同
# 的 Python 对象（只是共享同一个 tmp DB 文件），必须 patch 类本身才能两边
# 都命中。
# --------------------------------------------------------------------------

def test_move_source_changed_reports_error_code_over_http(client, repo, monkeypatch):
    from app.models.schemas import MemoryUpdate
    from app.repositories.sqlite.memory_store import MemoryStore

    real_create_copy = MemoryStore.create_copy_with_initial_revision

    def _create_copy_then_concurrent_edit(
        self, write, source_memory_id, changed_by, reason, expected_source_revision
    ):
        copied = real_create_copy(
            self, write, source_memory_id, changed_by, reason, expected_source_revision
        )
        # 模拟并发写者：就在副本提交之后、transfer() 走到自己的收尾删源之前，
        # 另一个请求编辑了源 memory 的标题。
        repo._runtime.memory_service.update(
            source_memory_id, write.created_by, MemoryUpdate(title="并发改的标题")
        )
        return copied

    monkeypatch.setattr(
        MemoryStore, "create_copy_with_initial_revision", _create_copy_then_concurrent_edit
    )

    h = _login(client, "a00000060")
    uid = _uid(client, h)
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    mem = _confirmed_memory(repo, src, uid)

    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [mem.id], "target_notebook_id": dst, "mode": "move", "extract_kg": False},
        headers=h,
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["ok"] is False
    assert result["status"] == "copied_source_not_removed"
    # 这条断言是本用例存在的理由：error_code 必须原样穿过 HTTP JSON，前端
    # summarizeTransferResults() 靠它区分 source_changed / cleanup_error。
    assert result["error_code"] == "source_changed"

    # 源仍在，且带着并发编辑后的新标题——不是复制那一刻的旧快照
    src_check = client.get(f"/api/memories/{mem.id}", headers=h)
    assert src_check.status_code == 200
    assert src_check.json()["title"] == "并发改的标题"
