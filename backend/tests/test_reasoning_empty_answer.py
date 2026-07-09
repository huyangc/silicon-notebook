"""Reasoning 模式:答案合成返回空 content 的退化路径。

根因(真机诊断坐实):reasoning 用思考型模型(deepseek-v4-pro),先吐 reasoning_content
(思维链,被 _stream_chat_content 丢弃)再吐 content(答案)。难对比题偶发把输出预算耗在
reasoning_content 上 → content 空 → chat_json 兜底返回 "{}" → _answer_reasoning 得空
answer(不抛异常,status=ok)→ 原先 ask_reasoning 静默退化成误导占位串
"Found N relevant KG object(s)" + 0 anchors,且不记任何 model_error(运维不可见)。

本用例锁定修复语义:①空 content 有界重试一次;②仍空则诚实降级(不冒充成功占位);
③补 model_error 可观测。max_tokens 与本 bug 无关(已确认 16384,答案实际才用 ~2k)。
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


class _StubLLM:
    """plan→reflect(answer)→refine(no-op)→answer 合成。answer 合成按 `answers` 依次返回
    (dict 或裸 JSON 串);超出次数则重复最后一项。answer_calls 记合成调用次数。"""
    configured = True

    def __init__(self, answers):
        self._answers = list(answers)
        self.answer_calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if "relevant" in schema_hint:              # evidence refine — no-op
            return json.dumps({"relevant": []})
        self.answer_calls += 1                     # answer synthesis
        item = self._answers[min(self.answer_calls - 1, len(self._answers) - 1)]
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
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


_PLACEHOLDER = "Found "   # 误导占位串前缀 "Found N relevant KG object(s)"


def test_empty_synthesis_retries_and_recovers(arepo):
    # 首次合成空 content("{}") → 重试第二次拿到真答案 → 用真答案,不退化
    nb = _seed(arepo)
    stub = _StubLLM(answers=["{}", {"answer": "RTL到GDSII是标准流程 [k1].", "grounded": True}])
    arepo.llm_client = stub
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert stub.answer_calls == 2, f"空 content 应触发一次重试,实际合成 {stub.answer_calls} 次"
    assert not resp.conclusion.startswith(_PLACEHOLDER)
    assert "RTL到GDSII是标准流程" in resp.conclusion


def test_empty_synthesis_degrades_honestly(arepo):
    # 两次合成皆空 → 不冒充成 "Found N objects";记 model_error;证据仍保留
    nb = _seed(arepo)
    stub = _StubLLM(answers=["{}"])                # 每次都空
    arepo.llm_client = stub
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert stub.answer_calls == 2, "应重试一次后仍空"
    assert not resp.conclusion.startswith(_PLACEHOLDER), "不得输出误导占位串"
    assert resp.model_errors, "空 content 静默失败应显式记 model_error(可观测)"
    assert resp.related_knowledge, "降级仍应保留已检索证据"


def test_healthy_synthesis_not_retried(arepo):
    # 回归:首次即产出真答案 → 不重试(answer_calls==1),行为不变
    nb = _seed(arepo)
    stub = _StubLLM(answers=[{"answer": "RTL到GDSII是标准流程 [k1].", "grounded": True}])
    arepo.llm_client = stub
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert stub.answer_calls == 1, "正常答案不应触发重试"
    assert "RTL到GDSII是标准流程" in resp.conclusion
