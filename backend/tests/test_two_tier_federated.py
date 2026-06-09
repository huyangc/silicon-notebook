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


class TestTask2:
    def _seed_two_notebooks(self, repo):
        """base notebook with one claim; personal notebook with one concept."""
        base_nb = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base_nb.id)
        repo.store_kg(base_nb.id, None, [
            {"local_id": "B1", "object_type": "claim",
             "payload": {"name": "base claim about capacitance", "section_path": "1"},
             "evidence": []},
        ], [])
        personal_nb = repo.create_notebook(NotebookCreate(name="personal"))
        repo.store_kg(personal_nb.id, None, [
            {"local_id": "P1", "object_type": "concept",
             "payload": {"name": "capacitance concept note", "section_path": "1"},
             "evidence": []},
        ], [])
        return base_nb, personal_nb

    def test_federated_retrieve_returns_hits_from_both_notebooks(self, repo):
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        nb_ids = {h.notebook_id for h in hits}
        assert base_nb.id in nb_ids
        assert personal_nb.id in nb_ids

    def test_federated_retrieve_tags_tier(self, repo):
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        base_hits = [h for h in hits if h.notebook_id == base_nb.id]
        personal_hits = [h for h in hits if h.notebook_id == personal_nb.id]
        assert all(h.tier == "base" for h in base_hits)
        assert all(h.tier == "personal" for h in personal_hits)

    def test_federated_retrieve_preserves_relevance_range(self, repo):
        """All relevance values must stay [0,1]; no [k] inflation from federation."""
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        for h in hits:
            assert 0.0 <= h.relevance <= 1.0, f"relevance {h.relevance!r} out of [0,1]"

    def test_ask_uses_federated_retrieve_when_base_exists(self, repo):
        """ask() on a personal notebook surfaces hits from the base notebook."""
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        resp = repo.ask(personal_nb.id, AskRequest(question="capacitance"))
        all_ids = {a.object_id for a in resp.anchors}
        all_ids |= {r.id for r in resp.related_knowledge}
        # At least one object from the base notebook must appear.
        with repo._connect() as db:
            base_ids = {r["id"] for r in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=?", (base_nb.id,)).fetchall()}
        assert all_ids & base_ids, "no base-notebook objects reached the answer"
