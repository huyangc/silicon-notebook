import json
import pytest
from app.core.config import Settings
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


def _hit(i, score):
    return RetrievedKnowledge(object_id=f"o{i}", object_type="concept",
                              payload={"name": f"C{i}"}, evidence=[], score=score)


class _RerankLLM:
    configured = True

    def chat_json(self, messages, schema_hint, **kwargs):
        # Promote index 2 to the top, demote index 0.
        return json.dumps({"items": [{"index": 0, "score": 0.1},
                                     {"index": 1, "score": 0.5},
                                     {"index": 2, "score": 0.9}]})


def test_rerank_reorders_by_llm_score(repo):
    repo.llm_client = _RerankLLM()
    repo.settings.rerank_enabled = True
    hits = [_hit(0, 0.9), _hit(1, 0.8), _hit(2, 0.7)]   # original order by score
    out = repo._rerank_hits("q", hits)
    assert [h.object_id for h in out] == ["o2", "o1", "o0"]


def test_rerank_noop_when_unconfigured(repo):
    class _Off:
        configured = False
        def chat_json(self, *a, **k):  # pragma: no cover
            raise AssertionError("must not call LLM")
    repo.llm_client = _Off()
    repo.settings.rerank_enabled = True   # no-op must be due to unconfigured, not disabled
    hits = [_hit(0, 0.9), _hit(1, 0.8)]
    assert repo._rerank_hits("q", hits) == hits


def test_rerank_noop_when_disabled(repo):
    repo.llm_client = _RerankLLM()
    repo.settings.rerank_enabled = False
    hits = [_hit(0, 0.9), _hit(1, 0.8)]
    assert repo._rerank_hits("q", hits) == hits


def test_rerank_falls_back_on_bad_json(repo):
    class _BadLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kwargs):
            return "not json"
    repo.llm_client = _BadLLM()
    repo.settings.rerank_enabled = True
    hits = [_hit(0, 0.9), _hit(1, 0.8)]
    assert repo._rerank_hits("q", hits) == hits   # unchanged order on parse failure
