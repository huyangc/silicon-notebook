import threading
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_connect_sets_performance_pragmas(repo):
    with repo._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1        # NORMAL
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2         # MEMORY
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 268435456  # 256MB
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -65536    # 64MB
