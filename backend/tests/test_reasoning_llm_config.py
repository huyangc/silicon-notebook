"""推理搜索独立模型配置 (REASONING_LLM_*) 的回归测试。"""
import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate, AskRequest
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


def test_reasoning_llm_configured_true_when_all_set(monkeypatch):
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")
    monkeypatch.setenv("REASONING_LLM_API_KEY", "rk")
    monkeypatch.setenv("REASONING_LLM_MODEL", "reason-model")
    assert Settings().reasoning_llm_configured is True


def test_reasoning_llm_configured_false_when_partial(monkeypatch):
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    assert Settings().reasoning_llm_configured is False


def test_reasoning_llm_configured_false_when_none(monkeypatch):
    monkeypatch.delenv("REASONING_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    assert Settings().reasoning_llm_configured is False


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("REASONING_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_reasoning_client_is_llm_client_when_unconfigured(repo):
    # 未配 REASONING_LLM_* → 推理 client 即全局 client（同一对象）。
    assert repo.reasoning_llm_client is repo.llm_client


def test_reasoning_client_follows_llm_client_reassignment(repo):
    # 未配置时回退是动态的：运行时替换 llm_client（既有推理测试就这么注入），
    # 推理 client 必须跟随——这正是既有推理测试零改动保持绿的保证。
    sentinel = object()
    repo.llm_client = sentinel
    assert repo.reasoning_llm_client is sentinel


def test_reasoning_client_distinct_and_uses_reasoning_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")
    monkeypatch.setenv("REASONING_LLM_API_KEY", "rk")
    monkeypatch.setenv("REASONING_LLM_MODEL", "reason-model")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    assert r.reasoning_llm_client is not r.llm_client
    assert r.reasoning_llm_client.base_url == "https://reason"
    assert r.reasoning_llm_client.model == "reason-model"
    assert r.reasoning_llm_client.configured is True


class _SeqLLM:
    """按 schema_hint 顺序返回预置 JSON，并记录调用次数。"""
    configured = True
    def __init__(self, plan, reflects, answer):
        self._plan, self._reflects, self._answer = plan, list(reflects), answer
        self.calls = 0
    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls += 1
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps(self._answer)


def test_reasoning_path_routes_through_reasoning_client(repo):
    # 注入"独立推理 client"为记录型 fake；全局 llm_client 设为一调用即爆，
    # 证明推理路径全程只走 reasoning_llm_client、绝不碰全局 client。
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    reasoning_llm = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "答案 [k1].", "grounded": True})

    class _BoomLLM:
        configured = True
        def chat_json(self, *a, **k):
            raise AssertionError("reasoning 路径不得使用全局 llm_client")

    repo.llm_client = _BoomLLM()
    repo._reasoning_llm_client = reasoning_llm   # 模拟已配置独立推理模型
    resp = repo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.answer.startswith("答案")
    assert reasoning_llm.calls >= 1
