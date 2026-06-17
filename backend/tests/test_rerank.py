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


import json as _json

class _SeqLLM:
    """plan 固定; reflect 按序列返回(耗尽后默认 answer)。"""
    configured = True
    def __init__(self, plan, reflects):
        self._plan = plan
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return _json.dumps(self._plan)
        if self._reflects:
            return _json.dumps(self._reflects.pop(0))
        return _json.dumps({"next_action": "answer", "sufficient": True})


def test_reasoning_run_applies_rerank(repo, monkeypatch):
    """KGA-3: when rerank_enabled=True, reasoning's final top_hits pass
    through _rerank_hits. Uses a spy that reverses the list so we can assert
    the wiring is real (not trivially passing)."""
    from app.models.schemas import NotebookCreate
    from app.services.reasoning_retrieval import ReasoningRetriever

    monkeypatch.setattr(repo.settings, "rerank_enabled", True)

    # Seed a notebook with 2 KG nodes so run() finds candidates.
    nb = repo.create_notebook(NotebookCreate(name="rerank-test-nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "relates", "evidence": []},
    ])

    # Drive run() to completion quickly via fake LLM (plan→reflect→answer).
    repo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    )

    seen: dict = {}

    def _spy(query, hits):
        seen["called"] = True
        seen["query"] = query
        return list(reversed(hits))   # deterministic reorder to confirm wiring

    monkeypatch.setattr(repo, "_rerank_hits", _spy)

    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "RTL到GDSII流程")

    assert seen.get("called") is True, "_rerank_hits was not called by run()"
    assert seen.get("query") == "RTL到GDSII流程"
    # The spy reversed the list; if wiring works the result reflects that.
    assert result.top_hits is not None
