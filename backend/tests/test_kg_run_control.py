from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.services.kg.run_control import (
    MODEL_UNAVAILABLE_MESSAGE,
    KgBuildAborted,
    KgBuildFailure,
    KgExtractionRunControl,
    TaskScopedKgClient,
    probe_kg_model,
)


def _settings(retries):
    return SimpleNamespace(
        kg_llm_timeout_seconds=60,
        kg_llm_max_retries=retries,
    )


def _connection_error():
    return APIConnectionError(
        request=httpx.Request("POST", "https://model.example/chat/completions")
    )


def _timeout_error():
    return APITimeoutError(
        request=httpx.Request("POST", "https://model.example/chat/completions")
    )


def _status_error(status):
    request = httpx.Request("POST", "https://model.example/chat/completions")
    response = httpx.Response(
        status,
        request=request,
        json={"error": {"message": f"status {status}"}},
    )
    return APIStatusError(
        f"status {status}",
        response=response,
        body=response.json(),
    )


class _SequenceClient:
    configured = True
    model = "test-model"

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0
        self.kwargs = []

    def chat_json(self, messages, response_schema_hint, **kwargs):
        self.kwargs.append(kwargs)
        index = self.calls
        self.calls += 1
        behavior = (
            self.behaviors[index]
            if index < len(self.behaviors)
            else self.behaviors[-1]
        )
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _BlockingFailureClient:
    configured = True
    model = "test-model"

    def __init__(self, entered):
        self.entered = entered
        self.calls = 0

    def chat_json(self, messages, response_schema_hint, **kwargs):
        self.calls += 1
        self.entered.set()
        raise _connection_error()


def test_transient_exhaustion_opens_only_its_run(monkeypatch):
    monkeypatch.setattr(
        "app.services.kg.run_control.random.uniform",
        lambda *_args: 0,
    )
    a = KgExtractionRunControl("job-a")
    b = KgExtractionRunControl("job-b")
    delegate = _SequenceClient(
        [_connection_error(), _connection_error(), _connection_error()]
    )
    client = TaskScopedKgClient(delegate, _settings(retries=2), a)

    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")

    assert raised.value.failure.code == "model_unavailable"
    assert delegate.calls == 3
    assert all(call["timeout"] == 60 for call in delegate.kwargs)
    assert all(call["max_retries"] == 0 for call in delegate.kwargs)
    assert a.aborted is True
    assert b.aborted is False


def test_success_before_limit_does_not_open_circuit(monkeypatch):
    monkeypatch.setattr(
        "app.services.kg.run_control.random.uniform",
        lambda *_args: 0,
    )
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_timeout_error(), '{"ok":true}'])
    client = TaskScopedKgClient(delegate, _settings(retries=2), control)
    assert (
        client.chat_json([{"role": "user", "content": "x"}], "{}")
        == '{"ok":true}'
    )
    assert control.aborted is False


def test_auth_failure_is_immediate():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_status_error(401)])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert raised.value.failure.code == "model_auth_failed"
    assert delegate.calls == 1


def test_other_http_rejection_is_immediate():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_status_error(404)])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert raised.value.failure.code == "model_request_rejected"
    assert delegate.calls == 1


def test_abort_wakes_retry_backoff(monkeypatch):
    monkeypatch.setattr(
        "app.services.kg.run_control.random.uniform",
        lambda *_args: 30,
    )
    control = KgExtractionRunControl("job-a")
    entered = threading.Event()
    delegate = _BlockingFailureClient(entered)
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.chat_json,
            [{"role": "user", "content": "x"}],
            "{}",
        )
        assert entered.wait(1)
        control.abort(
            KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
        )
        with pytest.raises(KgBuildAborted):
            future.result(timeout=1)
    assert delegate.calls == 1


def test_first_abort_failure_wins():
    control = KgExtractionRunControl("job-a")
    first = KgBuildFailure("first", "first message")
    second = KgBuildFailure("second", "second message")
    assert control.abort(first) is first
    assert control.abort(second) is first
    with pytest.raises(KgBuildAborted) as raised:
        control.raise_if_aborted()
    assert raised.value.failure is first


def test_unclassified_failure_does_not_open_circuit():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([ValueError("malformed model payload")])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(ValueError, match="malformed model payload"):
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert delegate.calls == 1
    assert control.aborted is False


def test_malformed_json_result_remains_soft():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient(["not json"])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    assert (
        client.chat_json([{"role": "user", "content": "x"}], "{}")
        == "not json"
    )
    assert control.aborted is False


def test_probe_uses_small_bounded_request():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient(['{"ok":true}'])
    settings = _settings(retries=2)
    client = TaskScopedKgClient(delegate, settings, control)
    probe_kg_model(client)
    assert delegate.kwargs == [
        {
            "max_tokens": 16,
            "bypass_cache": True,
            "timeout": 60,
            "max_retries": 0,
        }
    ]
    assert client.configured is True
    assert client.model == "test-model"
    assert client.settings is settings
