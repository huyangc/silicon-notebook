import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.sqlite_repository import SQLiteRepository


def _base_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")


@pytest.fixture
def cloud_repo(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok-test")
    return SQLiteRepository(Settings())


@pytest.fixture
def notoken_repo(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    return SQLiteRepository(Settings())


def test_add_url_sources_creates_and_rejects(cloud_repo, monkeypatch):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))

    def fake_probe(url, **kw):
        if url.endswith(".pdf"):
            return PdfProbe(True, "", 123, "doc.pdf")
        return PdfProbe(False, "URL 不是 PDF（Content-Type=text/html）", 0, "x.pdf")

    monkeypatch.setattr(remote_sources, "probe_pdf", fake_probe)
    scheduled = []
    result = cloud_repo.add_url_sources(
        nb.id, ["https://a/doc.pdf", "https://b/page.html"],
        scheduler=lambda sid: scheduled.append(sid),
    )
    assert len(result.created) == 1
    assert result.created[0].source_url == "https://a/doc.pdf"
    assert result.created[0].parse_status == "queued"
    assert result.created[0].type == "pdf"
    assert len(result.rejected) == 1
    assert "不是 PDF" in result.rejected[0].reason
    assert scheduled == [result.created[0].id]


def test_add_url_sources_requires_token(notoken_repo):
    nb = notoken_repo.create_notebook(NotebookCreate(name="n"))
    with pytest.raises(MinerUCloudNotConfigured):
        notoken_repo.add_url_sources(nb.id, ["https://a/doc.pdf"])


def test_add_url_sources_unknown_notebook_raises_keyerror(cloud_repo):
    with pytest.raises(KeyError):
        cloud_repo.add_url_sources("nb-missing", ["https://a/doc.pdf"])
