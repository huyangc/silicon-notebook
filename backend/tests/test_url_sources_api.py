from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import remote_sources
from app.services.remote_sources import FetchResult, PdfProbe
from app.services.sqlite_repository import SQLiteRepository


def _env(tmp_path, monkeypatch, token=None):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    if token:
        monkeypatch.setenv("MINERU_API_TOKEN", token)
    else:
        monkeypatch.delenv("MINERU_API_TOKEN", raising=False)


def _client(repo, monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.source_routes as source_routes_mod
    from app.main import app
    import app.api.deps as deps_mod

    monkeypatch.setattr(deps_mod, "repository", lambda: repo)
    monkeypatch.setattr(source_routes_mod, "repository", lambda: repo)
    monkeypatch.setattr(source_routes_mod.kg_scheduler, "submit_job", lambda fn, *a, **k: None)
    return TestClient(app)


def test_endpoint_partial_created_and_rejected(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(url.endswith(".pdf"),
                                   "" if url.endswith(".pdf") else "URL 不是 PDF（Content-Type=text/html）",
                                   1, "d.pdf"),
    )
    client = _client(repo, monkeypatch)
    resp = client.post(f"/api/notebooks/{nb.id}/sources/url",
                       json={"urls": ["https://a/d.pdf", "https://b/p.html"]})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["source_url"] == "https://a/d.pdf"
    assert len(body["rejected"]) == 1
    assert "不是 PDF" in body["rejected"][0]["reason"]


def test_endpoint_imports_only_configured_proxy_markdown(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token="tok")
    monkeypatch.setenv("URL_IMPORT_TRUSTED_PROXY_HOSTS", "http://127.0.0.1:8100")
    settings = Settings()
    repo = SQLiteRepository(settings)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    import app.api.source_routes as source_routes_mod

    monkeypatch.setattr(source_routes_mod, "get_settings", lambda: settings)
    seen = []

    def fake_fetch(url, timeout, *, allow_private=False):
        seen.append((url, allow_private))
        return FetchResult(200, "text/markdown", 9, b"# Snapshot")

    monkeypatch.setattr(remote_sources, "_default_fetch", fake_fetch)
    client = _client(repo, monkeypatch)
    trusted = "http://127.0.0.1:8100/export/snapshot.md"
    other = "http://127.0.0.1:8200/export/snapshot.md"
    response = client.post(
        f"/api/notebooks/{nb.id}/sources/url", json={"urls": [trusted, other]}
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["source_url"] for row in body["created"]] == [trusted]
    assert body["created"][0]["type"] == "markdown"
    assert [row["url"] for row in body["rejected"]] == [other]
    assert seen == [(trusted, True), (other, False)]


def test_endpoint_trusted_markdown_without_mineru(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token=None)
    monkeypatch.setenv("MINERU_MODE", "off")
    monkeypatch.setenv("URL_IMPORT_TRUSTED_PROXY_HOSTS", "http://127.0.0.1:8100")
    settings = Settings()
    repo = SQLiteRepository(settings)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    import app.api.source_routes as source_routes_mod

    monkeypatch.setattr(source_routes_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(
        remote_sources,
        "_default_fetch",
        lambda url, timeout, *, allow_private=False: FetchResult(
            200, "text/markdown", 11, b"# Snapshot\n"
        ),
    )
    client = _client(repo, monkeypatch)
    response = client.post(
        f"/api/notebooks/{nb.id}/sources/url",
        json={"urls": ["http://127.0.0.1:8100/export/snapshot.md"]},
    )
    assert response.status_code == 200
    assert response.json()["created"][0]["type"] == "markdown"


def test_endpoint_no_token_returns_400(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token=None)
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    client = _client(repo, monkeypatch)
    resp = client.post(f"/api/notebooks/{nb.id}/sources/url", json={"urls": ["https://a/d.pdf"]})
    assert resp.status_code == 400


def test_endpoint_unknown_notebook_returns_404(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    client = _client(repo, monkeypatch)
    resp = client.post("/api/notebooks/nb-missing/sources/url", json={"urls": ["https://a/d.pdf"]})
    assert resp.status_code == 404


# --- 每笔记本文档数量上限:URL 导入把绝对上限穿给逐条建源,store 在 INSERT 自己的
# 写事务内重新计数(收 codex 第 1 轮 P2;PR #584 codex R6 原子化,冻结预算退役)---
def test_add_url_sources_capacity_fills_remaining_then_rejects(tmp_path, monkeypatch):
    """上限 N:探测通过的 URL 建到 N 个,其余(仍通过探测)进 rejected(超限原因)。"""
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(remote_sources, "probe_pdf",
                        lambda url, **kw: PdfProbe(True, "", 1, "d.pdf"))
    result = repo.add_url_sources(
        nb.id, ["https://a/1.pdf", "https://a/2.pdf", "https://a/3.pdf"],
        scheduler=lambda sid: None, capacity_limit=2)
    assert len(result.created) == 2
    assert len(result.rejected) == 1
    assert "文档数量上限" in result.rejected[0].reason


def test_add_url_sources_capacity_none_is_unlimited(tmp_path, monkeypatch):
    """capacity_limit=None(admin 笔记本豁免)→ 全部有效 URL 都建,无超限拒绝。"""
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(remote_sources, "probe_pdf",
                        lambda url, **kw: PdfProbe(True, "", 1, "d.pdf"))
    result = repo.add_url_sources(
        nb.id, ["https://a/1.pdf", "https://a/2.pdf", "https://a/3.pdf"],
        scheduler=lambda sid: None, capacity_limit=None)
    assert len(result.created) == 3 and len(result.rejected) == 0


def test_add_url_sources_capacity_ignores_invalid_urls(tmp_path, monkeypatch):
    """codex 场景:上限 1,[有效 PDF, 无效非 PDF] → 有效建成、无效按自身原因拒;无效
    URL 不占配额,故不会让有效 URL 被误挡(修复前 len(urls)=2>1 会整批 409)。"""
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(url.endswith(".pdf"),
                                   "" if url.endswith(".pdf") else "URL 不是 PDF（Content-Type=text/html）",
                                   1, "d.pdf"))
    result = repo.add_url_sources(
        nb.id, ["https://a/valid.pdf", "https://b/invalid.html"],
        scheduler=lambda sid: None, capacity_limit=1)
    assert len(result.created) == 1
    assert result.created[0].source_url == "https://a/valid.pdf"
    assert len(result.rejected) == 1
    assert "不是 PDF" in result.rejected[0].reason
