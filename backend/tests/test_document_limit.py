"""每笔记本文档数量上限(per-user 可配)——IMPL-1 后端:配额引擎 + 强制 + 只读暴露。

覆盖:
  * schema v28 迁移(全新库建表/加列 + 已部署库补建/补列两条路径)
  * 计数口径(visible_document_count 与 list_sources_page.total_count 逐字一致,
    memory/knowhow 隐藏合成源都不计)
  * 有效上限解析三态(用户覆盖 / 全局默认 / config 回退)+ 坏配置回退
  * notebook_owner(owner_id, owner_role) 解析
  * _enforce_document_capacity(under / at=limit 放行 / over 拦 / admin 豁免)
  * 上传/导入端点超限 → 409 且带 X-User-Message;URL 导入接近上限时按剩余额度
    逐条部分成功(路由算 capacity + 穿参,逐条核算的服务级直测在 test_url_sources_api)
  * NotebookSummary.document_limit 经 get_notebook 回填 owner 有效上限
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.request_context import set_request_user
from app.models.identity import UserProfile
from app.models.notebooks import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture(autouse=True)
def _clear_request_user():
    """直连仓储的单元用例会 set_request_user 设当前用户;进程级 ContextVar 不会自动
    复位,收尾清掉以免泄漏到后续用例(HTTP 用例走各自请求的 get_current_user,不受影响)。"""
    yield
    set_request_user(None)


# --------------------------------------------------------------------------- #
# 直连仓储的单元层(不经 HTTP)——迁移 / 计数 / 有效上限解析
# --------------------------------------------------------------------------- #
def _repo(tmp_path, *, limit: int = 20, name: str = "t.db") -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/{name}",
            storage_dir=str(tmp_path / "storage"),
            user_upload_document_limit=limit,
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )


def _insert(repo: SQLiteRepository, notebook_id: str, sid: str, source_type: str = "pdf") -> None:
    repo._runtime.source_store.insert_source(
        source_id=sid, notebook_id=notebook_id, title=sid, source_type=source_type,
        status="uploaded", parse_status="uploaded", file_name="f", file_path="",
        file_size=0, file_hash="", summary="", doc_type="",
    )


def test_visible_document_count_excludes_memory_and_knowhow(tmp_path):
    repo = _repo(tmp_path)
    set_request_user(UserProfile(id="user-local", email="", display_name="a", role="admin"))
    nb = repo.create_notebook(NotebookCreate(name="n"))
    _insert(repo, nb.id, "s1")
    _insert(repo, nb.id, "s2")
    _insert(repo, nb.id, "m1", source_type="memory")
    _insert(repo, nb.id, "k1", source_type="knowhow")
    # 计数只算可见文档(2),隐藏合成源不计。
    assert repo.visible_document_count(nb.id) == 2
    # 与来源面板 total_count 逐字一致(单一真源守恒)。
    assert repo.list_sources_page(nb.id).total_count == 2


def test_effective_limit_resolution_three_states(tmp_path):
    repo = _repo(tmp_path, limit=20)
    # 1) 无覆盖、无全局默认 → 回退 config(20)
    assert repo.global_document_limit_default() == 20
    assert repo.user_document_limit_override("user-local") is None
    assert repo.effective_document_limit("user-local") == 20
    # 2) 全局默认覆盖(app_settings) → 应用到无覆盖用户
    with repo._write() as db:
        db.execute(
            "INSERT INTO app_settings(key, value, updated_at) "
            "VALUES('upload_document_limit_default', '7', 't')"
        )
    assert repo.global_document_limit_default() == 7
    assert repo.effective_document_limit("user-local") == 7
    # 3) 用户覆盖优先于全局默认
    with repo._write() as db:
        db.execute(
            "UPDATE user_profiles SET upload_document_limit = 3 WHERE user_id = 'user-local'"
        )
    assert repo.user_document_limit_override("user-local") == 3
    assert repo.effective_document_limit("user-local") == 3


def test_global_default_bad_value_falls_back_to_config(tmp_path):
    repo = _repo(tmp_path, limit=11)
    with repo._write() as db:
        db.execute(
            "INSERT INTO app_settings(key, value, updated_at) "
            "VALUES('upload_document_limit_default', 'not-an-int', 't')"
        )
    # 坏配置不卡死上传:回退 config 默认。
    assert repo.global_document_limit_default() == 11


def test_effective_limit_none_owner_uses_global_default(tmp_path):
    repo = _repo(tmp_path, limit=9)
    assert repo.effective_document_limit(None) == 9


def test_notebook_owner_returns_id_and_role(tmp_path):
    repo = _repo(tmp_path)
    # seeded user-local 是 admin
    admin = UserProfile(id="user-local", email="", display_name="a", role="admin")
    set_request_user(admin)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    owner_id, owner_role = repo.notebook_owner(nb.id)
    assert owner_id == "user-local"
    assert owner_role == "admin"
    assert repo.notebook_owner("nb-missing") == (None, None)


# --------------------------------------------------------------------------- #
# _enforce_document_capacity 直连单元(patch source_routes.source_repository)
# --------------------------------------------------------------------------- #
def _enforce_probe(repo, monkeypatch, notebook_id, adding):
    from app.api import source_routes
    monkeypatch.setattr(source_routes, "source_repository", lambda: repo)
    from fastapi import HTTPException
    try:
        source_routes._enforce_document_capacity(notebook_id, adding)
        return None
    except HTTPException as exc:
        return exc


def test_enforce_allows_under_and_at_limit_blocks_over(tmp_path, monkeypatch):
    repo = _repo(tmp_path, limit=2)
    u = repo.create_user("a00000001", "pw")
    set_request_user(u)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    _insert(repo, nb.id, "s1")
    _insert(repo, nb.id, "s2")  # current = 2 = limit

    assert _enforce_probe(repo, monkeypatch, nb.id, 0) is None      # 2+0=2 <= 2 放行
    exc = _enforce_probe(repo, monkeypatch, nb.id, 1)               # 2+1=3 > 2 拦
    assert exc is not None and exc.status_code == 409
    assert exc.headers.get("X-User-Message") == "1"
    assert "文档" in exc.detail
    # 界面词:不得泄露内部黑话
    assert "source" not in exc.detail.lower()


def test_document_capacity_limit_resolves_without_counting(tmp_path, monkeypatch):
    """只取上限的口(`_document_capacity_limit`,MCP 去重命中分支的穿参来源):
    非 admin → 有效上限;admin → None(豁免);且**绝不数当前文档数**——那正是它
    区别于 _document_capacity 的全部意义(去重命中路径的 COUNT 是白算的,而那是
    Agent 幂等重试的热路径)。"""
    from app.api import source_routes

    repo = _repo(tmp_path, limit=5)
    u = repo.create_user("b00000002", "pw")
    set_request_user(u)
    nb = repo.create_notebook(NotebookCreate(name="n"))

    def boom(*_a, **_k):
        raise AssertionError("limit-only 口不得数当前文档数")

    monkeypatch.setattr(repo, "visible_document_count", boom)
    assert source_routes._document_capacity_limit(nb.id, repo) == 5

    set_request_user(UserProfile(id="user-local", email="", display_name="a", role="admin"))
    nb_admin = repo.create_notebook(NotebookCreate(name="a"))
    assert source_routes._document_capacity_limit(nb_admin.id, repo) is None


def test_enforce_exempts_admin_owned_notebook(tmp_path, monkeypatch):
    repo = _repo(tmp_path, limit=1)
    # user-local 是 admin
    set_request_user(UserProfile(id="user-local", email="", display_name="a", role="admin"))
    nb = repo.create_notebook(NotebookCreate(name="admin nb"))
    for i in range(5):
        _insert(repo, nb.id, f"s{i}")
    assert _enforce_probe(repo, monkeypatch, nb.id, 100) is None    # admin 豁免


# --------------------------------------------------------------------------- #
# HTTP 端到端:三个端点超限 → 409 + X-User-Message;admin 豁免;详情带 document_limit
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/http.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_UPLOAD_DOCUMENT_LIMIT", "2")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.api import source_routes
    monkeypatch.setattr(source_routes.kg_scheduler, "submit_job", lambda fn, *a, **k: None)
    from app.main import create_app
    return TestClient(create_app())


def _auth(client, username="z00123456"):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _new_notebook(client, headers) -> str:
    return client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()["id"]


def _import_files(client, nb_id, headers, n):
    return client.post(
        f"/api/notebooks/{nb_id}/sources/import",
        json={"files": [{"file_name": f"f{i}.pdf"} for i in range(n)]},
        headers=headers,
    )


def test_import_endpoint_allows_up_to_limit_then_blocks_with_user_message(client):
    headers = _auth(client)
    nb_id = _new_notebook(client, headers)
    assert _import_files(client, nb_id, headers, 2).status_code == 200   # 0+2=2 == limit
    resp = _import_files(client, nb_id, headers, 1)                       # 2+1=3 > 2
    assert resp.status_code == 409
    assert resp.headers.get("X-User-Message") == "1"
    assert "文档" in resp.json()["detail"]


def test_import_endpoint_batch_over_limit_blocks(client):
    headers = _auth(client)
    nb_id = _new_notebook(client, headers)
    resp = _import_files(client, nb_id, headers, 3)                       # 0+3 > 2
    assert resp.status_code == 409
    assert resp.headers.get("X-User-Message") == "1"
    # 移动变异防线:强制必须在建源**之前**。若被挪到 import_sources() 之后,这 3 篇
    # 会先落库再报 409(白插),total_count 就变成 3——断言拒绝后计数仍为 0 钉住位置。
    listing = client.get(f"/api/notebooks/{nb_id}/sources", headers=headers).json()
    assert listing["total_count"] == 0


def test_upload_endpoint_over_limit_blocks(client):
    headers = _auth(client)
    nb_id = _new_notebook(client, headers)
    files = [("files", (f"u{i}.md", b"# x", "text/markdown")) for i in range(3)]
    resp = client.post(f"/api/notebooks/{nb_id}/sources", files=files, headers=headers)
    assert resp.status_code == 409
    assert resp.headers.get("X-User-Message") == "1"
    assert "文档" in resp.json()["detail"]


# URL 导入不做整批 409 —— 它天然部分成功(空白/不可达/非 PDF 跳过),容量按成功探测
# 逐条核算(上限由 store 在每条 INSERT 的写事务内重新计数)。逐条核算的服务级直测在
# test_url_sources_api.py;这里做**路由级端到端**:非 admin owner 的笔记本占到 limit-1,
# 提交 2 个有效 URL → 建 1 拒 1(超限),钉住路由的 capacity 计算(_document_capacity)
# 与穿参(capacity_limit=...),补上服务级直测绕过的这段接线。
def test_url_endpoint_respects_owner_capacity_partial_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/http.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("USER_UPLOAD_DOCUMENT_LIMIT", "2")
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")  # 云端已配,不会先报 provider 400
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.api import source_routes
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe
    monkeypatch.setattr(source_routes.kg_scheduler, "submit_job", lambda fn, *a, **k: None)
    monkeypatch.setattr(remote_sources, "probe_pdf",
                        lambda url, **kw: PdfProbe(True, "", 1, "d.pdf"))
    from app.main import create_app
    client = TestClient(create_app())
    headers = _auth(client)                      # 非 admin 用户 → 受上限约束
    nb_id = _new_notebook(client, headers)
    assert _import_files(client, nb_id, headers, 1).status_code == 200   # 占 1/2,剩 1
    resp = client.post(
        f"/api/notebooks/{nb_id}/sources/url",
        json={"urls": ["https://a/1.pdf", "https://a/2.pdf"]},
        headers=headers,
    )
    assert resp.status_code == 200                # 部分成功,非整批 409
    body = resp.json()
    assert len(body["created"]) == 1
    assert len(body["rejected"]) == 1
    assert "文档数量上限" in body["rejected"][0]["reason"]


def test_admin_owned_notebook_is_exempt_over_limit(client):
    admin = _auth_admin(client)
    nb_id = _new_notebook(client, admin)
    # 远超默认上限 2,admin 笔记本豁免。
    assert _import_files(client, nb_id, admin, 6).status_code == 200


def test_notebook_detail_carries_document_limit(client):
    headers = _auth(client)
    nb_id = _new_notebook(client, headers)
    detail = client.get(f"/api/notebooks/{nb_id}", headers=headers).json()
    assert detail["document_limit"] == 2   # 非 admin owner,有效上限 = config 默认 2
