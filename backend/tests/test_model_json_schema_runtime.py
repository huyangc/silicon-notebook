"""Native structured output negotiation stays inside the physical runtime."""
from __future__ import annotations

from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core import llm
from app.core.model_context import request_admission
from app.services.model_provider import ModelInvocationError, RuntimeModelProvider
from app.services.model_registry import ModelServiceDefinition, SystemModelServiceRegistry


class EventLog:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class FakeSDK:
    """Only the SDK transport is replaced; real negotiation and scheduling run."""
    def __init__(self, behavior="supported"):
        self.behavior = behavior
        self.calls = []
        self.business_data = None
        self.business_raw = None
        self.finish_reason = "stop"
        self.business_usage = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response_format = kwargs.get("response_format", {})
        if self.behavior == "text" and response_format:
            error = RuntimeError("response_format is unsupported")
            error.status_code = 400
            raise error
        schema = response_format.get("json_schema", {}).get("schema", {})
        properties = schema.get("properties", {})
        if "proof" in properties:
            if self.behavior == "rejected":
                error = RuntimeError("response_format json_schema is unsupported")
                error.status_code = 400
                raise error
            if self.behavior == "failed":
                raise TimeoutError("private upstream detail")
            if self.behavior == "ignored":
                data = {"ignored": True}
            else:
                data = {"proof": properties["proof"]["enum"][0], "count": 7}
        elif self.business_data is not None:
            data = self.business_data
        elif "query" in properties:
            data = {"query": "valid query"}
        elif "ok" in properties or kwargs["messages"][-1]["content"] == "health check":
            data = {"ok": ["wrong type"] if self.behavior == "invalid_health" else True}
        elif self.behavior == "invalid_business":
            data = {"answer": ["wrong type"], "grounded": True}
        else:
            data = {"answer": "valid answer", "grounded": True}
        content = self.business_raw if self.business_raw is not None and "proof" not in properties else json.dumps(data)
        usage = self.business_usage if "proof" not in properties else None
        if kwargs.get("stream"):
            return [SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=content), finish_reason=self.finish_reason,
            )], usage=usage)]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                     finish_reason=self.finish_reason)],
            usage=usage,
        )

    def close(self):
        pass


class AsyncProbeSDK:
    def __init__(self, sdk):
        self.sdk = sdk
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        chunks = self.sdk.create(**kwargs)

        class ProbeStream:
            async def __aiter__(self):
                for chunk in chunks:
                    yield chunk

            async def close(self):
                pass

        return ProbeStream()

    async def close(self):
        pass


def service():
    return ModelServiceDefinition(
        id="shared", display_name="Shared chat", kind="chat", protocol="openai",
        base_url="https://models.invalid/v1", api_key="private-test-credential",
        api_key_env="CHAT_KEY", model="test-model", max_concurrency=2,
        fingerprint="generation-1",
        context_window_tokens=1048576, max_output_tokens=131072,
    )


def registry(model_service):
    return SystemModelServiceRegistry(
        {model_service.id: model_service},
        {"ask_answer": model_service.id, "query_rewrite": model_service.id},
    )


def make_provider(tmp_path, monkeypatch, sdk):
    monkeypatch.setattr(llm.OpenAICompatibleClient, "client", lambda self: sdk)
    monkeypatch.setattr(llm.OpenAICompatibleClient, "_probe_client", lambda self, timeout: AsyncProbeSDK(sdk))
    event_log = EventLog()
    provider = RuntimeModelProvider(Settings(
        _env_file=None, model_services_config="", llm_cache_enabled=False,
        event_log_enabled=False, llm_log_enabled=False,
        storage_dir=str(tmp_path),
    ), event_log, registry=registry(service()))
    return provider, event_log


def is_probe(call):
    return "proof" in call.get("response_format", {}).get(
        "json_schema", {},
    ).get("schema", {}).get("properties", {})


def capability_events(log):
    return [event for event in log.events
            if event.get("kind") == "model_json_schema_capability"]


def ask(provider):
    return provider.chat("ask_answer").chat_json(
        [{"role": "user", "content": "question"}],
        '{"answer":"","grounded":true}', bypass_cache=True,
    )


def test_read_only_runtime_access_never_probes_upstream(tmp_path, monkeypatch):
    sdk = FakeSDK()
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
        assert provider.configured("ask_answer")
        assert provider.parallelism("ask_answer") == 2
        assert provider.chat("ask_answer").configured
        assert provider.chat("query_rewrite").configured
        assert provider.scheduler_snapshot("shared").active == 0
        assert sdk.calls == []
        assert capability_events(log) == []
    finally:
        provider.close()


@pytest.mark.parametrize("allowed", [0, 1])
def test_admission_bounds_schema_probe_and_business_send(tmp_path, monkeypatch, allowed):
    sdk = FakeSDK()
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    admitted = 0
    refusal = RuntimeError("extra report requests exhausted")
    def admit():
        nonlocal admitted
        if admitted >= allowed:
            raise refusal
        admitted += 1
    try:
        with request_admission(admit), pytest.raises(ModelInvocationError) as caught:
            ask(provider)
        assert caught.value.code == "report_context_budget_exhausted"
        assert len(sdk.calls) == admitted == allowed
        assert all(is_probe(call) for call in sdk.calls)
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
        assert caught.value.__cause__.__cause__ is refusal
    finally:
        provider.close()


def test_one_schema_probe_shared_by_workloads_and_manual_health_check(tmp_path, monkeypatch):
    sdk = FakeSDK()
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        assert json.loads(ask(provider))["answer"] == "valid answer"
        assert json.loads(provider.chat("query_rewrite").chat_json(
            [{"role": "user", "content": "rewrite"}], '{"query":""}',
            bypass_cache=True,
        ))["query"] == "valid query"
        observation = provider.probe("shared", actor_id="admin", allow_half_open=False)
        assert observation.status == "ok"
        assert len(sdk.calls) == 4
        assert sum(map(is_probe, sdk.calls)) == 1
        assert all(call["response_format"]["type"] == "json_schema" for call in sdk.calls)
        assert capability_events(log) == [{
            "kind": "model_json_schema_capability", "status": "supported",
            "reason": "schema_enforced", "service_id": "shared", "model": "test-model",
        }]
    finally:
        provider.close()


@pytest.mark.parametrize("changed", [
    {"model": "replacement-model"}, {"base_url": "https://replacement.invalid/v1"},
    {"api_key": "replacement-credential"},
])
def test_changed_service_generation_reprobes_but_stable_generation_reuses(
    tmp_path, monkeypatch, changed,
):
    sdk = FakeSDK()
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        adapter = provider.chat("ask_answer")
        ask(provider)
        provider.registry = registry(service())
        ask(provider)
        assert sum(map(is_probe, sdk.calls)) == 1
        provider.registry = registry(replace(service(), fingerprint="generation-2", **changed))
        adapter.chat_json([{"role": "user", "content": "next question"}],
                          '{"answer":"","grounded":true}', bypass_cache=True)
        assert sum(map(is_probe, sdk.calls)) == 2
        assert len(capability_events(log)) == 2
    finally:
        provider.close()


def test_capacity_or_label_reload_keeps_unchanged_model_capability(tmp_path, monkeypatch):
    sdk = FakeSDK()
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        ask(provider)
        provider.registry = registry(replace(
            service(), max_concurrency=3, display_name="Renamed service",
            fingerprint="capacity-generation",
        ))
        ask(provider)
        assert provider.scheduler_snapshot("shared").maximum == 3
        assert sum(map(is_probe, sdk.calls)) == 1
        assert len(capability_events(log)) == 1
    finally:
        provider.close()


@pytest.mark.parametrize(("behavior", "status", "reason"), [
    ("ignored", "unsupported", "schema_ignored"),
    ("rejected", "unsupported", "schema_rejected"),
    ("failed", "unverified", "probe_error"),
])
def test_capability_failure_preserves_business_calls_and_circuit_breaker(
    tmp_path, monkeypatch, behavior, status, reason,
):
    sdk = FakeSDK(behavior)
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        for _ in range(2):
            assert json.loads(ask(provider))["answer"] == "valid answer"
        assert sum(map(is_probe, sdk.calls)) == 1
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
        assert [call.get("response_format", {}).get("type") for call in sdk.calls[1:]] == [
            "json_object", "json_object",
        ]
        assert capability_events(log) == [{
            "kind": "model_json_schema_capability", "status": status,
            "reason": reason, "service_id": "shared", "model": "test-model",
        }]
        assert not any("private upstream detail" in json.dumps(event) for event in log.events)
    finally:
        provider.close()


def test_strict_response_rejection_retains_private_artifact_without_breaker_failure(
    tmp_path, monkeypatch,
):
    sdk = FakeSDK()
    sdk.business_data = {"unrelated": "private unusable reply"}
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    artifacts = []
    provider.set_malformed_response_sink(artifacts.append)
    try:
        with pytest.raises(ModelInvocationError) as caught:
            ask(provider)
        assert caught.value.code == "malformed_response"
        assert caught.value.detail == "missing_expected_key"
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
        assert len(artifacts) == 1
        assert artifacts[0].failure_kind == "schema_mismatch"
        assert json.loads(artifacts[0].response) == sdk.business_data
        assert not any("private unusable reply" in json.dumps(event) for event in log.events)
        sdk.business_data = None
        assert json.loads(ask(provider))["answer"] == "valid answer"
        assert sum(map(is_probe, sdk.calls)) == 1
    finally:
        provider.close()


def test_manual_health_check_schema_failure_is_malformed_without_breaker_failure(
    tmp_path, monkeypatch,
):
    sdk = FakeSDK("invalid_health")
    provider, _ = make_provider(tmp_path, monkeypatch, sdk)
    try:
        observation = provider.probe("shared", actor_id="admin", allow_half_open=False)
        assert observation.status == "error"
        assert observation.code == "malformed_response"
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
@pytest.mark.parametrize("workload", ["reasoning_agent", "report_sufficiency", "kg_extract"])
@pytest.mark.parametrize("stream", [False, True])
def test_partial_reflect_reply_reaches_shared_json_fallback(
    tmp_path, monkeypatch, behavior, workload, stream,
):
    from app.services import prompts

    sdk = FakeSDK(behavior)
    sdk.business_data = {
        "sufficient": False, "next_action": "search_chunks",
        "query": "N+2 N+3 process differences", "reason": "need more evidence",
    }
    sdk.business_usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    provider.registry = SystemModelServiceRegistry(
        {"shared": service()}, {workload: "shared"},
    )
    stats = {}
    try:
        reply = provider.chat(workload).chat_json(
            [{"role": "user", "content": "question"}],
            prompts.reflect_schema_hint(search_chunks=True), bypass_cache=True,
            call_stats=stats,
            cancel_event=threading.Event() if stream else None,
        )
        assert json.loads(reply) == sdk.business_data
        assert len(sdk.calls) == (3 if behavior == "text" else 2)
        assert stats["status"] == "ok" and stats["finish_reason"] == "stop"
        assert stats["attempts"] == len(sdk.calls)
        assert stats["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        assert bool([event for event in log.events if event.get("reason") == "schema_mismatch"]) == (behavior == "supported")
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
@pytest.mark.parametrize("wrapper", [
    '```json\n%s\n```', '<think>private reasoning</think>\n%s', 'reply:\n%s\nend',
    'Results from 2 sources:\n%s', '%s\nthe answer is above.',
    '%s\nfinal answer follows above.', '%s\nno further details.',
])
def test_common_json_wrappers_are_accepted_in_every_wire_mode(tmp_path, monkeypatch, behavior, wrapper):
    sdk = FakeSDK(behavior)
    sdk.business_raw = wrapper % '{"answer":"valid answer","grounded":true}'
    provider, _ = make_provider(tmp_path, monkeypatch, sdk)
    try:
        assert json.loads(ask(provider)) == {"answer": "valid answer", "grounded": True}
    finally:
        provider.close()


def test_unusable_wrapped_schema_reply_keeps_original_private_artifact(tmp_path, monkeypatch):
    sdk = FakeSDK()
    sdk.business_raw = '<think>private reasoning</think>\n```json\n{"unrelated":"private reply"}\n```'
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    artifacts = []
    provider.set_malformed_response_sink(artifacts.append)
    try:
        with pytest.raises(ModelInvocationError):
            ask(provider)
        assert len(artifacts) == 1 and artifacts[0].response == sdk.business_raw
        assert not any("private reasoning" in json.dumps(event) for event in log.events)
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
def test_shared_fallback_preserves_literal_think_tags_in_answer(tmp_path, monkeypatch, behavior):
    sdk = FakeSDK(behavior)
    sdk.business_data = {"answer": "Keep <think>literal source text</think> exactly"}
    provider, _ = make_provider(tmp_path, monkeypatch, sdk)
    try:
        assert json.loads(ask(provider)) == sdk.business_data
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
def test_shared_fallback_rejects_conflicting_unfinished_tail(tmp_path, monkeypatch, behavior):
    sdk = FakeSDK(behavior)
    sdk.business_raw = '{"answer":"first","grounded":false}\n{"answer":"different unfinished'
    provider, _ = make_provider(tmp_path, monkeypatch, sdk)
    try:
        with pytest.raises(ModelInvocationError) as caught:
            ask(provider)
        assert caught.value.code == "malformed_response"
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
def test_schema_field_coercion_uses_the_same_shared_shape_walk(tmp_path, monkeypatch, behavior):
    sdk = FakeSDK(behavior)
    sdk.business_data = {"answer": "valid answer", "grounded": "true"}
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        assert json.loads(ask(provider)) == {"answer": "valid answer", "grounded": True}
        events = [event for event in log.events if event.get("kind") == "model_json_shape"]
        assert events[0]["deviations"] == [{"path": "grounded", "reason": "invalid_boolean", "fix": "coerced"}]
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
@pytest.mark.parametrize("raw", [
    "{}", "[]", '[{"answer":"nested answer"}]',
    'reply:\n[{"answer":"nested answer"}',
    'reply:\n[{"answer":"nested answer"}]',
    'reply: true {"answer":"nested answer"}',
    '{"answer":"first"}\nNaN',
    '{"unrelated":"private reply"}', '{"answer":null}',
    '{"grounded":NaN}', '{"answer":"unfinished',
])
def test_shared_fallback_still_rejects_unusable_responses(tmp_path, monkeypatch, behavior, raw):
    sdk = FakeSDK(behavior)
    sdk.business_raw = raw
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    artifacts = []
    provider.set_malformed_response_sink(artifacts.append)
    try:
        with pytest.raises(ModelInvocationError) as caught:
            ask(provider)
        assert caught.value.code == "malformed_response"
        assert len(artifacts) == 1 and artifacts[0].response == raw
        assert provider.scheduler_snapshot("shared").breaker_state == "closed"
        assert not any("private reply" in json.dumps(event) for event in log.events)
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
def test_truncated_reply_is_not_adopted_or_counted_as_schema_failure(tmp_path, monkeypatch, behavior):
    sdk = FakeSDK(behavior)
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        ask(provider)  # finish the probe before changing the business finish reason
        sdk.business_raw = '{"answer":"unfinished'
        sdk.finish_reason = "length"
        for _ in range(provider.settings.model_json_schema_failure_threshold):
            with pytest.raises(ModelInvocationError) as caught:
                ask(provider)
            assert caught.value.finish_reason == "length"
        sdk.finish_reason = "stop"
        sdk.business_raw = None
        ask(provider)
        assert sdk.calls[-1].get("response_format", {}).get("type") == {
            "supported": "json_schema", "ignored": "json_object", "text": None,
        }[behavior]
        assert len(capability_events(log)) == 1
    finally:
        provider.close()


def test_schema_downgrade_survives_capacity_reload_but_not_model_identity_change(tmp_path, monkeypatch):
    sdk = FakeSDK()
    sdk.business_data = {"answer": "usable partial answer"}
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        ask(provider)
        provider.registry = registry(replace(service(), max_concurrency=3, fingerprint="capacity-generation"))
        for _ in range(provider.settings.model_json_schema_failure_threshold - 1):
            ask(provider)
        ask(provider)
        assert sdk.calls[-1]["response_format"]["type"] == "json_object"
        assert sum(map(is_probe, sdk.calls)) == 1
        event = capability_events(log)[-1]
        assert event["status"] == "unsupported" and event["reason"] == "schema_not_enforced"
        assert event["schema_name"] == sdk.calls[1]["response_format"]["json_schema"]["name"]
        provider.registry = registry(replace(service(), model="replacement", fingerprint="new-model"))
        ask(provider)
        assert sdk.calls[-1]["response_format"]["type"] == "json_schema"
        assert sum(map(is_probe, sdk.calls)) == 2
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored", "text"])
@pytest.mark.parametrize("repair_mode", ["off", "shadow", "on"])
def test_fallback_keeps_existing_syntax_repair_policy(tmp_path, monkeypatch, behavior, repair_mode):
    sdk = FakeSDK(behavior)
    sdk.business_raw = '{"answer":"valid answer" "grounded":true}'
    provider, _ = make_provider(tmp_path, monkeypatch, sdk)
    provider.settings.model_json_repair_mode = repair_mode
    try:
        if repair_mode == "on":
            assert json.loads(ask(provider)) == {"answer": "valid answer", "grounded": True}
        else:
            with pytest.raises(ModelInvocationError) as caught:
                ask(provider)
            assert caught.value.code == "malformed_response"
            if repair_mode == "shadow":
                assert caught.value.detail == "repairable_shadow"
    finally:
        provider.close()


@pytest.mark.parametrize("behavior", ["supported", "ignored"])
def test_conforming_nulls_are_not_reported_as_shape_drift(
    tmp_path, monkeypatch, behavior,
):
    from app.core.model_schema import response_format_for_hint
    from app.services import prompts

    hint = prompts.reflect_schema_hint(
        ("text",), ("Concept",), True, True, True, True,
    )
    schema = response_format_for_hint(hint)["json_schema"]["schema"]
    sdk = FakeSDK(behavior)
    sdk.business_data = dict.fromkeys(schema["properties"])
    sdk.business_data.update(sufficient=True, next_action="answer", reason="ok")
    provider, log = make_provider(tmp_path, monkeypatch, sdk)
    try:
        reply = provider.chat("ask_answer").chat_json(
            [{"role": "user", "content": "question"}], hint, bypass_cache=True,
        )
        # Strict or JSON mode, the consumer sees the unused branches as absent
        # and the nulls the prompt asks for are not drift.
        assert json.loads(reply) == {
            "sufficient": True, "next_action": "answer", "reason": "ok",
        }
        business = [call for call in sdk.calls if not is_probe(call)]
        assert ("response_format" in business[-1] and business[-1][
            "response_format"]["type"] == "json_schema") is (behavior == "supported")
        assert not [event for event in log.events
                    if event.get("kind") == "model_json_shape"]
    finally:
        provider.close()
