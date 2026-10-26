from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.core.model_context import (
    ContextBudget,
    ModelContextExceeded,
    RequestAdmissionError,
    is_context_error,
    request_admission,
)
from app.services.model_circuit_breaker import FailureKind, classify_provider_failure
from app.services.model_provider import ModelInvocationError, RuntimeModelProvider
from app.services.model_registry import ModelServiceDefinition, SystemModelServiceRegistry


def _settings():
    return Settings(_env_file=None, llm_log_enabled=False, event_log_enabled=False, llm_cache_enabled=False)


def _service():
    return ModelServiceDefinition(id="chat", display_name="测试模型", kind="chat", protocol="openai",
        base_url="https://example.invalid/v1", model="test-model", api_key_env="TEST_KEY",
        api_key="private-test-key", max_concurrency=1, fingerprint="v1",
        context_window_tokens=4096, max_output_tokens=1024)


def _result():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")])


def test_complete_wire_budget_includes_unicode_wrapper_and_schema():
    messages = [{"role": "system", "content": "约束"}, {"role": "user", "content": "中文代码 `{x}`"}]
    hint = '{"ok":true}'
    unlimited = ContextBudget(100000, 1000)
    counted = unlimited.input_tokens(messages, hint)
    assert counted > len(str(messages).encode("utf-8")) + len(hint)
    assert unlimited.input_tokens(messages, '{"longer_property_name":true}') > counted
    # max_input_tokens is an independent ceiling, inclusive at its boundary.
    assert ContextBudget(100000, 1000, counted).fits(messages, hint)
    assert not ContextBudget(100000, 1000, counted - 1).fits(messages, hint)


def test_safety_ratio_changes_each_request_budget_without_shared_state():
    standard = ContextBudget(10000, 1000)
    conservative = ContextBudget(10000, 1000, safety_ratio=0.25)
    exact_visible_only = ContextBudget(10000, 1000, safety_ratio=0)
    assert standard.max_input_tokens == 8500
    assert conservative.max_input_tokens == 6500
    assert exact_visible_only.max_input_tokens == 9000
    assert ContextBudget(10000, 1000).max_input_tokens == standard.max_input_tokens


@pytest.mark.parametrize("ratio", [-0.01, 1, float("inf"), float("nan")])
def test_settings_reject_invalid_context_safety_ratio(ratio):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_context_safety_ratio=ratio)


@pytest.mark.parametrize("ratio", [0, 0.25, 0.999])
def test_settings_accept_bounded_context_safety_ratio(ratio):
    assert Settings(_env_file=None, model_context_safety_ratio=ratio).model_context_safety_ratio == ratio


def test_guard_refuses_before_probe_and_clamps_actual_output():
    client = OpenAICompatibleClient(_settings(), base_url="https://example.invalid/v1",
        api_key="private", model="test", context_budget=ContextBudget(4096, 1000), enable_json_schema_probe=True)
    calls = []
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: calls.append(kw) or _result())))
    with pytest.raises(ModelContextExceeded):
        client.chat_json([{"role": "user", "content": "长" * 4000}], '{"ok":true}')
    assert not calls
    client._enable_json_schema_probe = False
    client.chat_json([{"role": "user", "content": "hi"}], '{"ok":true}', max_tokens=5000)
    assert calls[0]["max_tokens"] == 1000


def test_admission_covers_fallback_and_preserves_refusal_cause():
    calls = []
    def create(**kw):
        calls.append(kw)
        error = ValueError("response_format json_object unsupported")
        raise error
    client = OpenAICompatibleClient(_settings(), base_url="https://example.invalid/v1", api_key="private", model="test")
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    admitted = []
    refusal = RuntimeError("extra request limit")
    def admit():
        if admitted:
            raise refusal
        admitted.append(True)
    with request_admission(admit), pytest.raises(RequestAdmissionError) as raised:
        client.chat_json([{"role": "user", "content": "hi"}], '{"ok":true}')
    assert raised.value.__cause__ is refusal
    assert len(calls) == 1
    assert classify_provider_failure(raised.value) is FailureKind.IGNORED


def test_provider_context_error_keeps_code_and_never_trips_breaker():
    service = _service()
    registry = SystemModelServiceRegistry({service.id: service}, {"ask_answer": service.id})
    provider = RuntimeModelProvider(_settings(), SimpleNamespace(emit=lambda event: None), registry=registry,
        chat_factory=lambda service: SimpleNamespace(chat_json=lambda *a, **kw: _result()))
    try:
        with pytest.raises(ModelInvocationError) as caught:
            provider.chat("ask_answer").chat_json([{"role": "user", "content": "x" * 5000}], '{"ok":true}')
        assert caught.value.code == "model_context_exceeded"
        assert is_context_error(caught.value)
        assert classify_provider_failure(caught.value) is FailureKind.IGNORED
        assert provider.scheduler_snapshot(service.id).breaker_state == "closed"
    finally:
        provider.close()


def test_pin_keeps_capacity_route_and_thinking_across_registry_update():
    first = _service()
    calls = []
    def factory(service):
        return SimpleNamespace(chat_json=lambda *args, **kwargs: calls.append((service.fingerprint, kwargs)) or '{"ok":true}')
    registry = SystemModelServiceRegistry({first.id: first}, {"ask_answer": first.id}, {"ask_answer": "disabled"})
    settings = _settings().model_copy(update={"model_context_safety_ratio": 0.25})
    provider = RuntimeModelProvider(settings, SimpleNamespace(emit=lambda event: None), registry=registry, chat_factory=factory)
    try:
        adapter = provider.chat("ask_answer")
        pinned = adapter.pin_context(max_tokens=600)
        second = replace(first, fingerprint="v2", context_window_tokens=8192, max_output_tokens=2048)
        provider.registry = SystemModelServiceRegistry({second.id: second}, {"ask_answer": second.id}, {"ask_answer": "enabled"})
        pinned.chat_json([{"role": "user", "content": "hi"}], '{"ok":true}')
        adapter.chat_json([{"role": "user", "content": "hi"}], '{"ok":true}')
        assert pinned.context_budget.max_output_tokens == 600
        assert pinned.context_budget.context_window_tokens == 4096
        assert pinned.context_budget.safety_ratio == 0.25
        assert pinned.context_budget.max_input_tokens == 2472
        assert [(fingerprint, kw["thinking_mode"], kw["max_tokens"]) for fingerprint, kw in calls] == [
            ("v1", "disabled", 600), ("v2", "enabled", 2048)]
    finally:
        provider.close()


def test_remote_overflow_requires_specific_code_or_context_wording():
    error = RuntimeError("private upstream details")
    error.code = "context_length_exceeded"
    assert is_context_error(error)
    error.code = "bad_request"
    error.status_code = 400
    assert not is_context_error(error)
    error.body = {"error": {"code": "context_length_exceeded"}}
    assert is_context_error(error)
