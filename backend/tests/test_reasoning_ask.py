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
    def chat_json(self, messages, schema_hint, **kwargs):
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
        def chat_json(self, messages, schema_hint, **kwargs):
            raise RuntimeError("boom")
    arepo.llm_client = _BoomLLM()
    # LLM 全程抛错: run 内 plan/reflect 各自容错降级,answer 合成失败被吞,
    # 但检索不依赖 LLM,故仍返回合法 AskResponse(轨迹与候选仍在)。
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.reasoning_trace is not None        # 检索轨迹仍构建
    assert resp.answer == ""                        # 答案合成失败 → 空
    assert resp.evidence_level == "inferred"        # 无 anchor → 推断档
    assert resp.conversation_id


def _seed_graph(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_reasoning_expand_then_answer_end_to_end(arepo):
    nb = _seed_graph(arepo)
    claim = next((h for h in arepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                  if h.object_type == "claim"), None)
    assert claim is not None
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
            {"next_action": "answer", "sufficient": True}],
        answer={"answer": "流程概述 [k1],其布线步骤见 [k2].", "grounded": True})
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    step_kinds = [t.step_type for t in resp.reasoning_trace]
    assert "expand" in step_kinds
    assert step_kinds.index("expand") < step_kinds.index("answer")  # 深挖发生在合成之前
    # 深挖到的 procedure 进入了 related_knowledge
    assert any(r.object_type == "procedure" for r in resp.related_knowledge)


def test_reasoning_search_elements_fallback_path(arepo):
    nb = _seed_graph(arepo)
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "完全不相关的冷门问题zzz"}]},
        reflects=[
            {"next_action": "search_elements", "elements_query": "zzz"},
            {"next_action": "answer", "sufficient": True}],
        answer={"answer": "知识库未覆盖,以下为推断。", "grounded": False})
    resp = arepo.ask(nb.id, AskRequest(question="冷门zzz", mode="reasoning"))
    assert any(t.step_type == "fallback" for t in resp.reasoning_trace)
    # inferred 的根因: LLM 答案无 [k] 标记 → anchors 为空 → classify_evidence 落入 inferred
    # (无 anchor 时既不能 grounded 也不能 overview,与 KG 候选相关度高低无关)
    assert not resp.anchors
    assert resp.evidence_level == "inferred"


def test_reasoning_conversation_persists(arepo):
    nb = _seed_graph(arepo)
    arepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "答案 [k1].", "grounded": True})
    t1 = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    detail = arepo.get_conversation(t1.conversation_id)
    assert detail.turn_count == 1
    # 存回的轮次能反序列化(reasoning_trace 不破坏 AskResponse 往返)
    assert detail.turns[0].response.evidence_level == t1.evidence_level
