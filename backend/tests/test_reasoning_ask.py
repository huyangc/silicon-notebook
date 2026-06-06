import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


class _SeqLLM:
    configured = True
    def __init__(self, plan, reflects, answer):
        self._plan, self._reflects, self._answer = plan, list(reflects), answer
    def chat_json(self, messages, schema_hint):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps(self._answer)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    return nb


def test_reasoning_ask_returns_trace_and_evidence_level(arepo):
    nb = _seed(arepo)
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "RTL到GDSII是标准流程 [k1].", "grounded": True})
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.reasoning_trace and resp.reasoning_trace[0].step_type == "plan"
    assert resp.evidence_level in {"grounded", "overview", "inferred"}
    assert resp.conversation_id


def test_fast_mode_unaffected_and_no_trace(arepo):
    nb = _seed(arepo)
    arepo.llm_client = _SeqLLM(plan={}, reflects=[],
                               answer={"answer": "x", "grounded": False})
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程"))  # 默认 fast
    assert resp.reasoning_trace is None


def test_reasoning_degrades_gracefully_on_llm_error(arepo):
    nb = _seed(arepo)
    class _BoomLLM:
        configured = True
        def chat_json(self, messages, schema_hint):
            raise RuntimeError("boom")
    arepo.llm_client = _BoomLLM()
    # LLM 全程抛错: run 内 plan/reflect 各自容错降级,answer 合成失败被吞,
    # 但检索不依赖 LLM,故仍返回合法 AskResponse(轨迹与候选仍在)。
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.reasoning_trace is not None        # 检索轨迹仍构建
    assert resp.answer == ""                        # 答案合成失败 → 空
    assert resp.evidence_level == "inferred"        # 无 anchor → 推断档
    assert resp.conversation_id
