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


# P4-5: test_global_ask_synthesizes_from_reports and test_global_ask_no_reports_graceful
# deleted — they tested _ask_global (GraphRAG map-reduce) which was retired in P4-5.
# get_community_reports / rebuild_communities / summarize_communities are kept dormant.

def test_global_ask_missing_notebook_raises(repo):
    # P4-5: mode="global" now aliases to "chunk" via _RETIRED_MODES.
    # chunk on a missing notebook should raise KeyError (route -> 404).
    repo.llm_client = _GlobalLLM()
    with pytest.raises(KeyError):
        repo.ask("nb-does-not-exist", AskRequest(question="x", mode="global"))
