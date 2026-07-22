import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_embedding_client


class _ReportLLM:
    configured = True
    def __init__(self): self.calls = 0
    def chat_json(self, messages, schema_hint):
        self.calls += 1
        return json.dumps({"title": "Cascode techniques",
                           "summary": "A group about cascode and output resistance.",
                           "findings": ["cascode raises output resistance", "limits swing"]})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    provider = RecordingModelProvider()
    r = SQLiteRepository(Settings(), model_provider=provider)
    bind_embedding_client(r, FakeEmbedder(dim=16))
    r.recording_model_provider = provider
    return r


def _claim(lid, name):
    return {"local_id": lid, "object_type": "claim", "payload": {"name": name, "section_path": "1"}, "evidence": []}


def _rel(s, t):
    return {"source_local_id": s, "target_local_id": t, "edge_type": "supports", "evidence": []}


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # community_min_size default is now 3 (drops noise/singletons); this suite's
    # A-B group is size 2 by design, so lower the threshold to exercise it.
    repo.settings.community_min_size = 2
    repo.store_kg(nb.id, None, [_claim("A", "cascode raises output resistance"),
                                _claim("B", "cascode limits output swing"),
                                _claim("C", "current mirror copies current")],
                  [_rel("A", "B")])   # A-B one community; C isolated (no community)
    repo.rebuild_communities(nb.id)
    return nb


def test_summarize_communities_generates_reports(repo):
    llm = _ReportLLM()
    bind_chat_client(repo, "kg_community_summary", llm)
    repo.settings.kg_community_summary_enabled = True
    nb = _seed(repo)
    n = repo.summarize_communities(nb.id)
    assert n >= 1 and llm.calls >= 1
    assert ("chat", "kg_community_summary") in repo.recording_model_provider.calls
    reports = repo.get_community_reports(nb.id)
    assert reports and reports[0]["summary"] and reports[0]["title"]
    assert isinstance(reports[0]["findings"], list) and reports[0]["findings"]


def test_summarize_communities_off_by_default(repo):
    llm = _ReportLLM()
    bind_chat_client(repo, "kg_community_summary", llm)
    nb = _seed(repo)                       # kg_community_summary_enabled defaults False
    assert repo.summarize_communities(nb.id) == 0
    assert llm.calls == 0
    assert repo.get_community_reports(nb.id) == []   # no reports persisted


def test_get_community_reports_empty_when_unsummarized(repo):
    nb = _seed(repo)
    assert repo.get_community_reports(nb.id) == []
