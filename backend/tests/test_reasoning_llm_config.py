"""推理搜索独立模型配置 (REASONING_LLM_*) 的回归测试。"""
import pytest
from app.core.config import Settings


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
