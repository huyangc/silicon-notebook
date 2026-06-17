import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
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
    import app.api.routes as routes_mod
    from app.main import app
    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    monkeypatch.setattr(routes_mod.kg_scheduler, "submit_job", lambda fn, *a, **k: None)
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
