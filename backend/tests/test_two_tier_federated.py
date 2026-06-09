"""Track B — two-tier + federated tier-aware retrieval.

All five task suites live here. Each suite is gated independently; the full
`pytest -q` must stay green (no regression to single-notebook ask()).
"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


class TestTask1:
    def test_new_notebook_has_personal_tier(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="personal nb"))
        assert nb.tier == "personal"

    def test_mark_notebook_base_sets_tier(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="textbook"))
        repo.mark_notebook_base(nb.id)
        nb2 = repo.get_notebook(nb.id)
        assert nb2.tier == "base"

    def test_tier_is_idempotent_on_existing_db(self, tmp_path, monkeypatch):
        """Running _migrate() twice on a DB that already has the tier column
        must not raise (PRAGMA guard prevents duplicate ALTER TABLE)."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        repo1 = SQLiteRepository(Settings())
        nb = repo1.create_notebook(NotebookCreate(name="nb"))
        repo1.mark_notebook_base(nb.id)
        # Second repo init on same DB must not raise.
        repo2 = SQLiteRepository(Settings())
        assert repo2.get_notebook(nb.id).tier == "base"
