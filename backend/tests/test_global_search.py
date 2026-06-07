import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate, AskRequest
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


class _GlobalLLM:
    """Handles three schema shapes: community-report (title), map (points), reduce (answer)."""
    configured = True
    def __init__(self): self.map_calls = 0; self.reduce_calls = 0
    def chat_json(self, messages, schema_hint, **kwargs):
        if '"title"' in schema_hint:
            # community report summarization
            return json.dumps({"title": "Cascode", "summary": "Cascode techniques overview.",
                               "findings": ["cascode raises output resistance", "limits swing"]})
        if '"points"' in schema_hint:
            self.map_calls += 1
            return json.dumps({"points": [{"description": "cascode boosts output resistance", "score": 90}]})
        self.reduce_calls += 1
        return json.dumps({"answer": "Cascode raises output resistance across the design.", "grounded": True})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _claim(lid, name):
    return {"local_id": lid, "object_type": "claim", "payload": {"name": name, "section_path": "1"}, "evidence": []}


def _prep_reports(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [_claim("A", "cascode raises output resistance"),
                                _claim("B", "cascode limits swing")],
                  [{"source_local_id": "A", "target_local_id": "B", "edge_type": "supports", "evidence": []}])
    repo.rebuild_communities(nb.id)
    repo.settings.kg_community_summary_enabled = True
    repo.llm_client = _GlobalLLM()
    repo.summarize_communities(nb.id)   # generate reports via stub
    return nb


def test_global_ask_synthesizes_from_reports(repo):
    nb = _prep_reports(repo)
    resp = repo.ask(nb.id, AskRequest(question="how does cascode affect output resistance?", mode="global"))
    assert "output resistance" in (resp.answer or resp.conclusion)
    assert repo.llm_client.map_calls >= 1 and repo.llm_client.reduce_calls == 1


def test_global_ask_no_reports_graceful(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.llm_client = _GlobalLLM()
    resp = repo.ask(nb.id, AskRequest(question="anything?", mode="global"))
    # no community reports -> graceful fallback, no crash, no reduce call
    assert resp is not None and (resp.answer or resp.conclusion)
    assert repo.llm_client.reduce_calls == 0
