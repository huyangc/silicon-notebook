import json
import pytest
from app.core.config import Settings
from app.models.schemas import AskRequest
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


class _GlobalLLM:
    """Handles two schema shapes: map (points), reduce (answer)."""
    configured = True
    def __init__(self): self.map_calls = 0; self.reduce_calls = 0
    def chat_json(self, messages, schema_hint, **kwargs):
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
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


# P4-5: test_global_ask_synthesizes_from_reports and test_global_ask_no_reports_graceful
# deleted — they tested _ask_global (GraphRAG map-reduce) which was retired in P4-5.
# The dormant community-report summarizer (summarize_communities /
# get_community_reports, the kg_community_summary workload) was retired later;
# rebuild_communities stays because retrieval reads the community boards.

def test_global_ask_missing_notebook_raises(repo):
    # P4-5: mode="global" now aliases to "chunk" via _RETIRED_MODES.
    # chunk on a missing notebook should raise KeyError (route -> 404).
    bind_chat_client(repo, "ask_answer", _GlobalLLM())
    with pytest.raises(KeyError):
        repo.ask("nb-does-not-exist", AskRequest(question="x", mode="global"))
