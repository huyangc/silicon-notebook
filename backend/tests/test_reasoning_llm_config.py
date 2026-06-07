"""推理搜索独立模型配置 (REASONING_LLM_*) 的回归测试。"""
import pytest
from app.core.config import Settings
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
