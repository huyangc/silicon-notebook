"""Regression tests for the LLM client's fail-fast behavior: a stalled
connection must NOT be amplified into a ~6-minute block by SDK auto-retries or
by the JSON-mode -> plain-mode fallback."""
import httpx
import pytest
from openai import APIConnectionError

import app.core.llm as llm_mod
from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient


class _Msg:
    def __init__(self, c): self.content = c


class _Choice:
    def __init__(self, c): self.message = _Msg(c)


class _Resp:
    def __init__(self, c='{"ok":1}'):
        self.choices = [_Choice(c)]
        self.usage = None


class _FakeCreate:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = []

    def __call__(self, **kwargs):
        i = len(self.calls)
        self.calls.append(kwargs)
        b = self.behaviors[i] if i < len(self.behaviors) else self.behaviors[-1]
        if isinstance(b, Exception):
            raise b
        return b


class _Completions:
    def __init__(self, create): self.create = create


class _Chat:
    def __init__(self, create): self.completions = _Completions(create)


class _FakeOpenAI:
    def __init__(self, create): self.chat = _Chat(create)


def _make(monkeypatch, create):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://x")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    c = OpenAICompatibleClient(Settings())
    monkeypatch.setattr(c, "client", lambda: _FakeOpenAI(create))
    return c


def test_connection_error_fails_fast_no_fallback(monkeypatch):
    err = APIConnectionError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err])
    c = _make(monkeypatch, create)
    with pytest.raises(APIConnectionError):
        c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert len(create.calls) == 1  # NO second (plain-mode) attempt on a network stall


def test_param_rejection_falls_back_to_plain(monkeypatch):
    create = _FakeCreate([ValueError("response_format unsupported"), _Resp()])
    c = _make(monkeypatch, create)
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert len(create.calls) == 2  # genuine param rejection DOES fall back
    assert "response_format" in create.calls[0]
    assert "response_format" not in create.calls[1]
    assert out == '{"ok":1}'


def test_client_built_with_no_sdk_retries(monkeypatch):
    captured = {}

    def fake_openai(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(llm_mod, "OpenAI", fake_openai)
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://x")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    c = OpenAICompatibleClient(Settings())
    c.client()
    assert captured.get("max_retries") == 0
