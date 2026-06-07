import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _hit(i):
    return RetrievedKnowledge(object_id=f"o{i}", object_type="concept",
                              payload={"name": f"Concept {i}"}, evidence=[])


def test_answer_context_respects_char_budget(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    long_def = "x" * 5000
    # Each concept reports a 5000-char definition; without a budget the block
    # would be ~25k chars. Distinct cluster ids so none are collapsed.
    monkeypatch.setattr(repo, "_concept_cluster_id", lambda nbid, oid: oid)
    monkeypatch.setattr(repo, "node_context", lambda nbid, oid: {
        "occurrences": [{"element_text": long_def, "source_title": "S",
                         "section_path": "1"}],
        "definition": long_def, "steps": None})
    repo.settings.answer_context_budget_chars = 1000
    repo.settings.answer_context_min_items = 2
    block, id_map = repo._answer_context(nb.id, [_hit(i) for i in range(5)])
    assert len(block) <= 1000 + 5000      # bounded: at most one over-budget line
    assert len(id_map) >= 2               # min_items honored
    assert len(id_map) < 5                # not all 5 packed in


def test_answer_context_keeps_all_when_small(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    monkeypatch.setattr(repo, "_concept_cluster_id", lambda nbid, oid: oid)
    monkeypatch.setattr(repo, "node_context", lambda nbid, oid: {
        "occurrences": [{"element_text": "short", "source_title": "S",
                         "section_path": "1"}],
        "definition": "short", "steps": None})
    repo.settings.answer_context_budget_chars = 6000
    block, id_map = repo._answer_context(nb.id, [_hit(i) for i in range(3)])
    assert len(id_map) == 3               # all small hits fit
