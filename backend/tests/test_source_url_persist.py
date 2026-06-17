import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_source_url_persists_and_reads_back(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    sid, now = "src-urltest01", _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status, "
            "parse_status, file_name, file_path, source_url, file_size, file_hash, "
            "summary, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pdf', 'queued', 'queued', ?, '', ?, 0, '', '', ?, ?)",
            (sid, nb.id, "paper.pdf", "paper.pdf", "https://x/paper.pdf", now, now),
        )
    assert repo.get_source(sid).source_url == "https://x/paper.pdf"
