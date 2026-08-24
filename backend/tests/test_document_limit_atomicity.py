"""文档数量上限的事务内强制(PR #584 codex R6)——建源端点的 check-then-insert 竞态。

此前容量检查(_document_capacity)是插入之前的独立读取:只剩一个名额时,两个并发
请求(两个标签页/两位可写成员)都能快照到 capacity=1,双双入库越过上限。修法是把
判定挪进建源的**同一写事务**:SQLite 在 BEGIN IMMEDIATE + 进程写锁下 COUNT+INSERT
(PG 侧对 notebook 行加锁,twin 在 tests/postgres/test_core_store_conformance.py)。

并发用例沿用 test_upload_dedup.py 的手法:真线程 + Barrier 把两个请求钉在原子闸
**之前**同时放行——若把闸挪回事务外的预读(变异),两个线程都会在 Barrier 前读到
「还剩 1」,双双入库,用例转红;没有任何 sleep,串行由写锁/行锁自身决定。

既有行为(预检 409 文案、URL 部分成功、admin 豁免、去重复用不占名额)必须逐字
不变——由 test_document_limit.py / test_url_sources_api.py 继续钉住。
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import DocumentCapacityExceeded, UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # URL 用例要过「已配置解析服务」前置闸;probe 已打桩,绝不发真实网络请求。
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")
    return Settings()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = SQLiteRepository(_settings(tmp_path, monkeypatch))
    monkeypatch.setattr(
        r._runtime.source_ingestion,
        "process_source",
        lambda sid, hooks: None,
    )
    return r


@pytest.fixture
def notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb")).id


def _file(name: str, content: bytes) -> UploadedSourceFile:
    return UploadedSourceFile(
        file_name=name, content_type="text/plain", content=content
    )


# ---------------------------------------------------------------- store 层语义
def test_store_gate_refuses_at_limit_inside_the_transaction(repo, notebook_id):
    """同一把闸的串行语义:第 1 条(0<1)入库,第 2 条(1>=1)在事务内被拒、不插行,
    异常带事务内的 current/limit 供路由拼同一句 409 文案。"""
    store = repo._runtime.source_store

    def insert(sid, digest):
        return store.insert_source_if_absent(
            source_id=sid, notebook_id=notebook_id, digest=digest,
            title=sid, source_type="markdown", status="queued",
            parse_status="queued", file_name=f"{sid}.md", file_path="",
            file_size=1, summary="", doc_type="", capacity_limit=1,
        )

    assert insert("src-a", "digest-a") is None          # 0 < 1:入库
    with pytest.raises(DocumentCapacityExceeded) as exc:
        insert("src-b", "digest-b")                     # 1 >= 1:事务内拒绝
    assert exc.value.current == 1 and exc.value.limit == 1
    assert repo.visible_document_count(notebook_id) == 1


def test_store_gate_dedup_reuse_wins_over_a_full_notebook(repo, notebook_id):
    """判序:去重重查在容量闸**之前**。满库重传相同字节必须复用既有行(复用不新增
    文档),绝不能被上限拒绝——顺序反了(变异)这里转红。"""
    store = repo._runtime.source_store

    def insert(sid):
        return store.insert_source_if_absent(
            source_id=sid, notebook_id=notebook_id, digest="digest-same",
            title=sid, source_type="markdown", status="queued",
            parse_status="queued", file_name=f"{sid}.md", file_path="",
            file_size=1, summary="", doc_type="", capacity_limit=1,
        )

    assert insert("src-first") is None                  # 占满(1/1)
    assert insert("src-retry") == "src-first"           # 满库复用,不抛不插


def test_insert_source_capacity_requires_an_owned_transaction(repo, notebook_id):
    """capacity_limit 只支持自有写事务:joined connection 无法保证 COUNT 在写锁
    之后,静默放行等于闸没生效——必须响亮拒绝。"""
    store = repo._runtime.source_store
    with repo._write() as db:
        with pytest.raises(ValueError, match="owned write transaction"):
            store.insert_source(
                source_id="src-x", notebook_id=notebook_id, title="x",
                source_type="pdf", status="queued", parse_status="queued",
                file_name="x.pdf", file_path="", file_size=0, file_hash="",
                summary="", doc_type="", connection=db, capacity_limit=1,
            )


def test_url_capacity_is_an_absolute_limit_not_a_frozen_budget(
    repo, notebook_id, monkeypatch
):
    """capacity_limit 语义钉死为**绝对上限**(事务内重新计数):库里已有 1 篇、上限 2,
    两个有效 URL 只建 1 个。旧的冻结剩余额度语义会把 2 读成「还能建 2 个」而全建。"""
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    repo.upload_sources(
        notebook_id, [_file("seed.txt", b"seed")], scheduler=lambda s: None
    )
    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    result = repo.add_url_sources(
        notebook_id, ["https://a/1.pdf", "https://a/2.pdf"],
        scheduler=lambda s: None, capacity_limit=2,
    )
    assert len(result.created) == 1
    assert len(result.rejected) == 1
    assert "文档数量上限" in result.rejected[0].reason
    assert repo.visible_document_count(notebook_id) == 2


def test_url_over_limit_short_circuits_after_the_first_capacity_rejection(
    repo, notebook_id, monkeypatch
):
    """上限用尽后的剩余 URL 不再逐条开写事务(codex 评审 P3):第一次容量拒绝后
    本请求内直接进 rejected(粘滞短路)。判据数 insert_source 调用次数:limit=1、
    3 个探测通过的 URL → 恰好 2 次(1 建成 + 1 被闸拒),第 3 条零事务零 COUNT。
    粘滞只在本请求内,仍比退役的「请求头冻结预算」新鲜——中途释放的名额由下一次
    请求拿到。"""
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    store = repo._runtime.source_store
    real = store.insert_source
    attempted: list[str] = []

    def counting(**kwargs):
        attempted.append(kwargs["source_id"])
        return real(**kwargs)

    monkeypatch.setattr(store, "insert_source", counting)

    result = repo.add_url_sources(
        notebook_id, ["https://a/1.pdf", "https://a/2.pdf", "https://a/3.pdf"],
        scheduler=lambda s: None, capacity_limit=1,
    )
    assert len(result.created) == 1
    assert len(result.rejected) == 2
    assert all("文档数量上限" in row.reason for row in result.rejected)
    assert len(attempted) == 2, (
        f"第一次容量拒绝后必须短路,实际发起了 {len(attempted)} 次插入"
    )
    assert repo.visible_document_count(notebook_id) == 1


# ------------------------------------------------------------ 并发竞态(真线程)
def test_concurrent_uploads_with_one_slot_admit_exactly_one(
    repo, notebook_id, monkeypatch, tmp_path
):
    """两个并发上传、不同内容、只剩 1 个名额:Barrier 让两个线程都走过预检位置、
    同时抵达原子闸,写锁串行后恰有一个入库,输家拿到 DocumentCapacityExceeded 且
    不留孤儿落盘文件。"""
    store = repo._runtime.source_store
    real = store.insert_source_if_absent
    barrier = threading.Barrier(2, timeout=5)

    def racing(**kwargs):
        barrier.wait()
        return real(**kwargs)

    monkeypatch.setattr(store, "insert_source_if_absent", racing)

    refused: list[DocumentCapacityExceeded] = []
    created: list[str] = []
    lock = threading.Lock()

    def upload(name, content):
        try:
            rows = repo.upload_sources(
                notebook_id, [_file(name, content)],
                scheduler=lambda s: None, capacity_limit=1,
            )
            with lock:
                created.extend(row.id for row in rows)
        except DocumentCapacityExceeded as exc:
            with lock:
                refused.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(upload, "a.txt", b"content-a")
        f2 = pool.submit(upload, "b.txt", b"content-b")
        f1.result(timeout=10)
        f2.result(timeout=10)

    assert len(created) == 1, f"只剩 1 个名额必须恰好放行一个,实际 {created}"
    assert len(refused) == 1
    assert refused[0].current == 1 and refused[0].limit == 1
    assert repo.visible_document_count(notebook_id) == 1
    # 输家刚落盘的文件必须被清掉:目录里只剩赢家那一个。
    uploads_dir = Path(str(tmp_path / "storage")) / "notebooks" / notebook_id
    assert len(list(uploads_dir.iterdir())) == 1


def test_concurrent_url_imports_with_one_slot_admit_exactly_one(
    repo, notebook_id, monkeypatch
):
    """URL 路径同一竞态:两个并发单 URL 请求、上限 1。输家不 409,而是沿用既有的
    部分成功语义——该 URL 进 rejected(超限原因)。"""
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    store = repo._runtime.source_store
    real = store.insert_source
    barrier = threading.Barrier(2, timeout=5)

    def racing(**kwargs):
        barrier.wait()
        return real(**kwargs)

    monkeypatch.setattr(store, "insert_source", racing)

    results = []
    lock = threading.Lock()

    def add(url):
        result = repo.add_url_sources(
            notebook_id, [url], scheduler=lambda s: None, capacity_limit=1
        )
        with lock:
            results.append(result)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(add, "https://a/1.pdf")
        f2 = pool.submit(add, "https://b/2.pdf")
        f1.result(timeout=10)
        f2.result(timeout=10)

    all_created = [row for result in results for row in result.created]
    all_rejected = [row for result in results for row in result.rejected]
    assert len(all_created) == 1, "只剩 1 个名额必须恰好建成一个"
    assert len(all_rejected) == 1
    assert "文档数量上限" in all_rejected[0].reason
    assert repo.visible_document_count(notebook_id) == 1


# ----------------------------------------------------- 路由层:竞态输家 → 同句 409
def test_upload_endpoint_maps_the_race_loser_to_the_same_409(tmp_path, monkeypatch):
    """预检被并发抢先(打桩成只回上限、不拒绝)时,store 的事务内闸拒绝,路由必须翻成
    与预检**同一句** 409(X-User-Message + 中文文案),而不是 500。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/http.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_UPLOAD_DOCUMENT_LIMIT", "2")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.api import deps

    deps.repository.cache_clear()
    from app.api import source_routes

    monkeypatch.setattr(
        source_routes.kg_scheduler, "submit_job", lambda fn, *a, **k: None
    )
    # 模拟「预检通过后名额被并发占走」:预检不拒绝、只回真上限。
    monkeypatch.setattr(
        source_routes, "_enforce_document_capacity", lambda nb, adding: 2
    )
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    token = client.post(
        "/api/auth/login", json={"username": "z00123456", "password": "pw"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    nb_id = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()["id"]

    # 占满 2/2(逐个上传,预检桩不拦、真闸 0<2/1<2 放行)。
    for i in range(2):
        resp = client.post(
            f"/api/notebooks/{nb_id}/sources",
            files=[("files", (f"u{i}.md", f"# {i}".encode(), "text/markdown"))],
            headers=headers,
        )
        assert resp.status_code == 200

    resp = client.post(
        f"/api/notebooks/{nb_id}/sources",
        files=[("files", ("u2.md", b"# 2", "text/markdown"))],
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.headers.get("X-User-Message") == "1"
    assert resp.json()["detail"] == source_routes.document_capacity_message(2, 2, 1)
    listing = client.get(f"/api/notebooks/{nb_id}/sources", headers=headers).json()
    assert listing["total_count"] == 2
