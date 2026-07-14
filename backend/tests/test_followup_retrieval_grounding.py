import json
import pytest


def test_settings_have_rewrite_and_evidence_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.evidence_tau_low == 0.18
    assert s.evidence_tau_high == 0.35
    assert s.proc_min == 2
    # 专用快改写模型:默认未配 → False
    assert s.rewrite_llm_configured is False

    monkeypatch.setenv("EVIDENCE_TAU_HIGH", "0.5")
    assert Settings().evidence_tau_high == 0.5
    # 设了 REWRITE_LLM_MODEL → 启用(base_url/api_key 缺省复用主端点)
    monkeypatch.setenv("REWRITE_LLM_MODEL", "deepseek-v4-fast")
    assert Settings().rewrite_llm_configured is True


def test_is_process_query_and_type_weight():
    from app.services.retrieval import is_process_query, type_weight
    assert is_process_query("展开讲讲RTL到GDSII的流程") is True
    assert is_process_query("把这个流程按阶段画成流程图") is True
    assert is_process_query("what are the place and route steps") is True
    assert is_process_query("innovus是什么工具") is False
    assert type_weight("procedure", False) == 0.7
    assert type_weight("claim", False) == 1.0
    assert type_weight("procedure", True) == 1.0
    assert type_weight("claim", True) == 0.9
    assert type_weight("concept", True) == 0.6


def _rk(oid, otype, score):
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=oid, object_type=otype, payload={},
                              score=score, relevance=score)


def test_ensure_procedure_quota_backfills_and_preserves_order():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    scored = [
        _rk("c1", "claim", 0.9), _rk("c2", "claim", 0.8), _rk("c3", "claim", 0.7),
        _rk("p1", "procedure", 0.6), _rk("p2", "procedure", 0.5), _rk("c4", "claim", 0.1),
    ]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    types = [h.object_type for h in out]
    assert types.count("procedure") == 2
    assert len(out) == 3
    assert out[0].object_id == "c1"
    assert [key(h) for h in out] == sorted((key(h) for h in out), reverse=True)

def test_ensure_procedure_quota_noop_when_enough():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    scored = [_rk("p1", "procedure", 0.9), _rk("p2", "procedure", 0.8), _rk("c1", "claim", 0.7)]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    assert [h.object_id for h in out] == ["p1", "p2", "c1"]

def test_ensure_procedure_quota_edge_cases():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    # empty pool → empty result, no crash
    assert ensure_procedure_quota([], top_n=3, min_proc=2, key=key) == []
    # fewer procedures than min_proc → returns what exists, never exceeds top_n
    scored = [_rk("c1", "claim", 0.9), _rk("c2", "claim", 0.8), _rk("p1", "procedure", 0.3)]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    assert len(out) == 3 and sum(h.object_type == "procedure" for h in out) == 1


def _anchor(oid):
    from app.models.schemas import AnswerAnchor
    return AnswerAnchor(key="k1", object_id=oid, object_type="claim", label="x")


def test_classify_evidence_three_levels():
    from app.services.retrieval import classify_evidence
    strong = [_rk("a", "claim", 0.6), _rk("b", "claim", 0.2)]
    lvl, top = classify_evidence(strong, [_anchor("a")], True, 0.18, 0.35)
    assert lvl == "grounded" and top == 0.6
    weak = [_rk("a", "claim", 0.25)]
    lvl, _ = classify_evidence(weak, [_anchor("a")], True, 0.18, 0.35)
    assert lvl == "overview"
    # LLM self-reports grounded but the cited hit is weak → must NOT be grounded
    lvl, _ = classify_evidence(strong, [], True, 0.18, 0.35)
    assert lvl == "inferred"
    lvl, top = classify_evidence([], [], False, 0.18, 0.35)
    assert lvl == "inferred" and top == 0.0


def test_followup_rewrite_prompt():
    from app.services.prompts import followup_rewrite_prompt, FOLLOWUP_REWRITE_SCHEMA_HINT
    p = followup_rewrite_prompt("User: innovus中有哪些常见flow\nAssistant: ...RTL到GDSII...",
                                "展开讲讲这个流程")
    assert "展开讲讲这个流程" in p
    assert "RTL到GDSII" in p
    assert "query" in FOLLOWUP_REWRITE_SCHEMA_HINT


from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


class RecordingLLM:
    """按 schema_hint 区分『改写调用』与『回答调用』。"""
    configured = True
    def __init__(self):
        self.rewrite_calls = []
        self.answer_calls = []
    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[0]["content"]
        if schema_hint == '{"query":""}':
            self.rewrite_calls.append(content)
            return json.dumps({"query": "RTL到GDSII流程 步骤"})
        self.answer_calls.append(content)
        return json.dumps({"answer": "答案 [k1].", "grounded": True})


@pytest.fixture
def repo2(tmp_path, monkeypatch):
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
    r.llm_client = RecordingLLM()
    return r


def _seed_flow(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    return nb


def test_first_turn_not_rewritten(repo2):
    nb = _seed_flow(repo2)
    resp = repo2.ask(nb.id, AskRequest(question="innovus中有哪些常见flow"))
    assert repo2.llm_client.rewrite_calls == []
    assert resp.retrieval_query == "innovus中有哪些常见flow"
    assert resp.evidence_level in {"grounded", "overview", "inferred"}


def test_followup_triggers_rewrite_and_uses_rewritten_query(repo2):
    nb = _seed_flow(repo2)
    t1 = repo2.ask(nb.id, AskRequest(question="innovus中有哪些常见flow"))
    repo2.ask(nb.id, AskRequest(question="展开讲讲这个流程",
                                conversation_id=t1.conversation_id))
    assert len(repo2.llm_client.rewrite_calls) == 1
    last = repo2.ask(nb.id, AskRequest(question="再展开这个流程",
                                       conversation_id=t1.conversation_id))
    assert last.retrieval_query == "RTL到GDSII流程 步骤"


def test_ask_sets_evidence_level_field(repo2):
    nb = _seed_flow(repo2)
    resp = repo2.ask(nb.id, AskRequest(question="RTL到GDSII流程"))
    assert hasattr(resp, "evidence_level") and resp.evidence_level
    assert resp.top_relevance >= 0.0


def test_additive_followup_always_rewritten(repo2):
    # 去掉 looks_like_followup 闸门后:此前漏判的「加上…」式追问现在也会触发改写。
    nb = _seed_flow(repo2)
    t1 = repo2.ask(nb.id, AskRequest(question="innovus中有哪些常见flow"))
    assert repo2.llm_client.rewrite_calls == []  # 首轮无 history → 不改写
    repo2.ask(nb.id, AskRequest(question="加上Qwen系列模型和GLM系列模型的对比",
                                conversation_id=t1.conversation_id))
    assert len(repo2.llm_client.rewrite_calls) == 1  # 旧逻辑(闸门漏判)会是 0
    assert "加上Qwen" in repo2.llm_client.rewrite_calls[0]


def test_followup_rewrite_uses_dedicated_rewrite_client(repo2):
    # 配了专用快改写 client → 改写走它,不走主 client(答案仍走主 client)。
    fast = RecordingLLM()
    repo2._rewrite_llm_client = fast
    nb = _seed_flow(repo2)
    t1 = repo2.ask(nb.id, AskRequest(question="innovus中有哪些常见flow"))
    repo2.ask(nb.id, AskRequest(question="加上Qwen系列的对比",
                                conversation_id=t1.conversation_id))
    assert len(fast.rewrite_calls) == 1          # 改写落在专用快 client
    assert repo2.llm_client.rewrite_calls == []  # 主 client 未收到改写
