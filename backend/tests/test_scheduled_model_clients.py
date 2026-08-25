from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.core.event_logging import get_log_owner, reset_log_owner, set_log_owner
from app.services.cancellation import AskCancelled
from app.services import model_provider as provider_mod
from app.services.model_registry import ModelServiceDefinition, SystemModelServiceRegistry

RuntimeModelProvider = provider_mod.RuntimeModelProvider


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _service(
    service_id: str, kind: str, maximum: int = 2, *, model: str = ""
) -> ModelServiceDefinition:
    return ModelServiceDefinition(
        id=service_id,
        display_name=f"安全服务-{service_id}",
        kind=kind,
        protocol="openai",
        base_url=f"https://{service_id}.example/v1",
        model=model or f"safe-{service_id}",
        api_key_env=f"{service_id.upper()}_KEY",
        api_key="sk-private",
        max_concurrency=maximum,
        fingerprint=f"fp-{service_id}",
    )


def _registry(
    *,
    maximum: int = 2,
    chat_model: str = "",
    thinking_modes=None,
    bind_reasoning: bool = False,
) -> SystemModelServiceRegistry:
    services = {
        "chat": _service("chat", "chat", maximum, model=chat_model),
        "embed": _service("embed", "embedding", maximum),
        "rerank": _service("rerank", "rerank", maximum),
    }
    bindings = {
        "ask_answer": "chat",
        "query_rewrite": "chat",
        "kg_extract": "chat",
        "kg_glean": "chat",
        "kg_refine": "chat",
        "retrieval_query_embedding": "embed",
        "retrieval_rerank": "rerank",
    }
    if bind_reasoning:
        bindings["reasoning_agent"] = "chat"
    return SystemModelServiceRegistry(
        services, bindings, thinking_modes
    )


def test_deepseek_v4_uses_workload_thinking_defaults():
    raw = _Chat()
    provider = _provider(
        registry=_registry(
            chat_model="deepseek-v4-flash", bind_reasoning=True
        ),
        chat=raw,
    )
    try:
        for workload_id in ("kg_extract", "kg_glean", "kg_refine"):
            provider.chat(workload_id).chat_json([], "{}")
        provider.chat("query_rewrite").chat_json([], "{}")
        provider.chat("ask_answer").chat_json([], "{}")
        provider.chat("reasoning_agent").chat_json([], "{}")
    finally:
        provider.close()

    assert [
        call["kwargs"].get("thinking_mode") for call in raw.calls
    ] == [
        "disabled", "disabled", "disabled", "disabled", "enabled", "enabled",
    ]


def test_workload_thinking_configuration_overrides_the_default():
    raw = _Chat()
    provider = _provider(
        registry=_registry(
            chat_model="deepseek-v4-pro",
            thinking_modes={
                "ask_answer": "disabled",
                "reasoning_agent": "provider_default",
            },
            bind_reasoning=True,
        ),
        chat=raw,
    )
    try:
        provider.chat("ask_answer").chat_json([], "{}")
        provider.chat("reasoning_agent").chat_json([], "{}")
    finally:
        provider.close()

    assert [
        call["kwargs"].get("thinking_mode") for call in raw.calls
    ] == [
        "disabled", None,
    ]


def test_non_deepseek_kg_service_does_not_receive_provider_specific_extension():
    raw = _Chat()
    provider = _provider(chat=raw)
    try:
        provider.chat("kg_extract").chat_json([], "{}")
    finally:
        provider.close()

    assert raw.calls[0]["kwargs"].get("thinking_mode") is None


class _Chat:
    configured = True

    def __init__(self, result: str = '{"ok": true}') -> None:
        self.model = "raw-private-model"
        self.result = result
        self.calls: list[dict] = []

    def chat_json(self, messages, response_schema_hint, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.result


class _Embedder:
    configured = True
    dim = 3

    def embed_texts(self, texts):
        return [[float(i), 1.0, 2.0] for i, _ in enumerate(texts)]

    def embed_query(self, text):
        return [3.0, 2.0, 1.0]


class _Reranker:
    configured = True
    max_docs = 1

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    def _rerank_batch(self, query, documents):
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        time.sleep(0.03)
        with self._lock:
            self.active -= 1
        return [{"index": 0, "relevance_score": float(ord(documents[0][0]))}]


def _provider(
    *, registry=None, chat=None, embedder=None, reranker=None, events=None,
    observation_sink=None,
):
    raw_chat = chat or _Chat()
    raw_embedder = embedder or _Embedder()
    raw_reranker = reranker or _Reranker()
    return RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        events or _EventLog(),
        registry=registry or _registry(),
        chat_factory=lambda _service: raw_chat,
        embedding_factory=lambda _service: raw_embedder,
        rerank_factory=lambda _service: raw_reranker,
        observation_sink=observation_sink,
    )


def test_kind_mismatch_fails_before_constructing_or_calling_transport():
    chat_factory_calls = []
    provider = RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        _EventLog(),
        registry=_registry(),
        chat_factory=lambda service: chat_factory_calls.append(service.id) or _Chat(),
    )
    try:
        with pytest.raises(ValueError, match="kind"):
            provider.chat("retrieval_query_embedding")
        assert chat_factory_calls == []
    finally:
        provider.close()


def test_unbound_workloads_are_deterministically_offline_and_plan_one_worker():
    provider = _provider()
    try:
        client = provider.chat("reasoning_agent")
        assert not client.configured
        assert provider.configured("reasoning_agent") is False
        assert provider.parallelism("reasoning_agent") == 1

        offline_embedder = provider.embedding("source_element_embedding")
        assert offline_embedder.configured is False
        assert offline_embedder.embed_query("same") == offline_embedder.embed_query("same")
        assert provider.parallelism("source_element_embedding") == 1
    finally:
        provider.close()


def test_raw_and_queued_chat_cancellation_preserve_ask_cancelled():
    events = _EventLog()

    class CancellingChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            raise AskCancelled()

    provider = _provider(chat=CancellingChat(), events=events)
    try:
        with pytest.raises(AskCancelled):
            provider.chat("ask_answer").chat_json([], "{}")
        assert events.events[-1]["status"] == "cancelled"
        assert events.events[-1]["retry_outcome"] == "cancelled"
    finally:
        provider.close()

    entered = threading.Event()
    release = threading.Event()

    class BlockingChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            entered.set()
            assert release.wait(2)
            return self.result

    events = _EventLog()
    provider = _provider(registry=_registry(maximum=1), chat=BlockingChat(), events=events)
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        client = provider.chat("ask_answer")
        active = executor.submit(client.chat_json, [], "{}")
        assert entered.wait(1)
        cancelled = threading.Event()
        queued = executor.submit(client.chat_json, [], "{}", cancel_event=cancelled)
        cancelled.set()
        with pytest.raises(AskCancelled):
            queued.result(timeout=2)
        assert events.events[-1]["status"] == "cancelled"
        release.set()
        assert active.result(timeout=2) == '{"ok": true}'
    finally:
        release.set()
        provider.close()
        executor.shutdown()


def test_each_submission_copies_the_current_log_owner_context():
    owners = []

    class OwnerChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            owners.append(get_log_owner())
            return self.result

    provider = _provider(chat=OwnerChat())
    try:
        for owner in ("user-1111111111", "user-2222222222"):
            token = set_log_owner(owner)
            try:
                provider.chat("ask_answer").chat_json([], "{}")
            finally:
                reset_log_owner(token)
        assert owners == ["user-1111111111", "user-2222222222"]
    finally:
        provider.close()


def test_bound_workloads_share_one_raw_client_and_scheduler_per_service():
    raw = _Chat()
    constructed = []
    provider = RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        _EventLog(),
        registry=_registry(maximum=3),
        chat_factory=lambda service: constructed.append(service.id) or raw,
    )
    try:
        first = provider.chat("ask_answer")
        second = provider.chat("query_rewrite")
        assert json.loads(first.chat_json([], "{}")) == {"ok": True}
        assert json.loads(second.chat_json([], "{}")) == {"ok": True}
        assert constructed == ["chat"]
        assert provider.parallelism("ask_answer") == 3
        assert provider.scheduler_snapshot("chat").maximum == 3
    finally:
        provider.close()


def test_scheduled_chat_forwards_response_validator_to_raw_client():
    raw = _Chat('{"nodes": [], "edges": []}')
    provider = _provider(chat=raw)

    def validator(content: str) -> bool:
        return content.startswith('{"nodes"')

    try:
        assert provider.chat("ask_answer").chat_json(
            [], "{}", response_validator=validator
        ) == raw.result
        assert raw.calls[-1]["kwargs"]["response_validator"] is validator
    finally:
        provider.close()


def test_embedding_calls_are_scheduled_and_bound_to_service_capacity():
    provider = _provider(registry=_registry(maximum=4))
    try:
        embedder = provider.embedding("retrieval_query_embedding")
        assert embedder.configured
        assert embedder.embed_query("q") == [3.0, 2.0, 1.0]
        assert embedder.embed_texts(["a", "b"])[1] == [1.0, 1.0, 2.0]
        assert provider.parallelism("retrieval_query_embedding") == 4
    finally:
        provider.close()


@pytest.mark.parametrize("payload", ["", "[]", "not-json", "null"])
def test_chat_rejects_empty_or_non_object_success_with_safe_support_metadata(payload):
    events = _EventLog()
    provider = _provider(chat=_Chat(payload), events=events)
    try:
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            provider.chat("ask_answer").chat_json(
                [{"role": "user", "content": "PRIVATE PROMPT"}], "{}"
            )

        error = caught.value
        assert error.code == "malformed_response"
        assert error.service_id == "chat"
        assert error.service_name == "安全服务-chat"
        assert error.workload_id == "ask_answer"
        assert error.workload_label == "问答回答"
        assert error.model == "safe-chat"
        assert error.support_id.startswith("mdl-")
        serialized = json.dumps(events.events, ensure_ascii=False)
        assert "PRIVATE PROMPT" not in serialized
        assert "sk-private" not in serialized
        assert "https://" not in serialized
    finally:
        provider.close()


def test_approved_chat_workload_repairs_complete_json_syntax_faults():
    events = _EventLog()
    provider = _provider(
        chat=_Chat('{answer: "Recovered [k1]", grounded: true}'), events=events
    )
    try:
        raw = provider.chat("ask_answer").chat_json(
            [{"role": "user", "content": "q"}],
            '{"answer":"","grounded":true}',
        )

        assert json.loads(raw) == {
            "answer": "Recovered [k1]",
            "grounded": True,
        }
        repair = next(
            event for event in events.events
            if event.get("kind") == "model_json_repair"
        )
        assert repair["status"] == "repaired"
        assert repair["workload_id"] == "ask_answer"
        assert repair["support_id"].startswith("mdl-")
        assert "Recovered" not in json.dumps(repair)
    finally:
        provider.close()


def test_shadow_mode_observes_repair_but_keeps_strict_failure():
    events = _EventLog()
    provider = RuntimeModelProvider(
        Settings(
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
            model_json_repair_mode="shadow",
        ),
        events,
        registry=_registry(),
        chat_factory=lambda _service: _Chat('{answer: "x", grounded: true}'),
    )
    try:
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            provider.chat("ask_answer").chat_json(
                [], '{"answer":"","grounded":true}'
            )

        assert caught.value.code == "malformed_response"
        repair = next(
            event for event in events.events
            if event.get("kind") == "model_json_repair"
        )
        assert repair["status"] == "repairable"
    finally:
        provider.close()


def test_unapproved_workload_remains_strict_when_repair_is_on():
    provider = _provider(chat=_Chat('{query: "q"}'))
    try:
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            provider.chat("query_rewrite").chat_json([], '{"query":""}')

        assert caught.value.code == "malformed_response"
    finally:
        provider.close()


def test_json_repair_observability_failure_does_not_reject_repaired_response():
    class FailingEventLog(_EventLog):
        def emit(self, event: dict) -> None:
            raise OSError("diagnostic sink unavailable")

    provider = _provider(
        chat=_Chat('{answer: "kept", grounded: false}'),
        events=FailingEventLog(),
    )
    try:
        content = provider.chat("ask_answer").chat_json(
            [], '{"answer":"","grounded":true}'
        )
        assert json.loads(content) == {"answer": "kept", "grounded": False}
    finally:
        provider.close()


def test_upstream_failure_never_exposes_raw_exception_endpoint_or_key():
    class FailingChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            raise RuntimeError("https://private.example sk-private response body")

    provider = _provider(chat=FailingChat())
    try:
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            provider.chat("ask_answer").chat_json([], "{}")
        error = caught.value
        assert error.code == "provider_error"
        assert error.support_id.startswith("mdl-")
        assert "private" not in str(error)
        assert "https://" not in str(error)
        assert "sk-" not in str(error)
    finally:
        provider.close()


def test_raw_retry_loop_remains_inside_one_scheduler_slot():
    class RetryingChat(_Chat):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.active = 0
            self.maximum_active = 0

        def chat_json(self, messages, response_schema_hint, **kwargs):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            for _ in range(3):
                time.sleep(0.01)
            with self.lock:
                self.active -= 1
            return self.result

    raw = RetryingChat()
    provider = _provider(registry=_registry(maximum=1), chat=raw)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(provider.chat("ask_answer").chat_json, [], "{}")
                for _ in range(2)
            ]
            assert [future.result() for future in futures] == [raw.result, raw.result]
        assert raw.maximum_active == 1
    finally:
        provider.close()


def test_rerank_splits_are_separate_visible_scheduled_calls_and_merge_order():
    raw = _Reranker()
    provider = _provider(registry=_registry(maximum=2), reranker=raw)
    try:
        order = provider.rerank("retrieval_rerank").rerank("q", ["a", "c", "b"])
        assert order == [1, 2, 0]
        assert raw.maximum_active == 2
    finally:
        provider.close()


def test_rerank_uses_a_capacity_bounded_submission_window_for_many_batches():
    raw = _Reranker()
    provider = _provider(registry=_registry(maximum=2), reranker=raw)
    documents = list("abcdefghijklmnopqrst")
    try:
        order = provider.rerank("retrieval_rerank").rerank("q", documents)
        assert order == list(reversed(range(len(documents))))
        assert raw.maximum_active == 2
    finally:
        provider.close()


def test_malformed_rerank_rows_are_breaker_visible_and_fall_back_with_typed_error():
    class MalformedReranker(_Reranker):
        def _rerank_batch(self, query, documents):
            return [{"index": "bad", "relevance_score": "not-a-number"}]

    errors = []
    provider = _provider(registry=_registry(maximum=1), reranker=MalformedReranker())
    try:
        client = provider.rerank("retrieval_rerank")
        for _ in range(3):
            assert client.rerank("q", ["a"], on_error=errors.append) == [0]
        assert [error.code for error in errors] == [
            "malformed_response",
            "malformed_response",
            "malformed_response",
        ]
        assert provider.scheduler_snapshot("rerank").breaker_state == "open"
    finally:
        provider.close()


def test_close_rejects_new_work_but_drains_the_active_call():
    entered = threading.Event()
    release = threading.Event()

    class BlockingChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            entered.set()
            assert release.wait(2)
            return self.result

    provider = _provider(registry=_registry(maximum=1), chat=BlockingChat())
    client = provider.chat("ask_answer")
    executor = ThreadPoolExecutor(max_workers=2)
    active = executor.submit(client.chat_json, [], "{}")
    assert entered.wait(1)
    closing = executor.submit(provider.close)
    deadline = time.monotonic() + 1
    while not closing.running():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    with pytest.raises(provider_mod.ModelInvocationError) as caught:
        client.chat_json([], "{}")
    assert caught.value.code == "model_service_unavailable"
    release.set()
    assert active.result() == '{"ok": true}'
    closing.result()
    executor.shutdown()


def test_scheduler_event_has_safe_correlation_and_no_content_or_endpoint():
    events = _EventLog()
    provider = _provider(events=events)
    try:
        assert provider.chat("ask_answer").chat_json(
            [{"role": "user", "content": "PRIVATE PROMPT"}], "PRIVATE SCHEMA"
        )
        event = events.events[-1]
        assert event["kind"] == "model_scheduler"
        assert event["support_id"].startswith("mdl-")
        assert event["workload_id"] == "ask_answer"
        assert event["workload_label"] == "问答回答"
        assert event["service_id"] == "chat"
        assert event["service_name"] == "安全服务-chat"
        assert event["model"] == "safe-chat"
        assert event["actor_id"] == "system"
        assert "parent_id" in event
        assert event["queue_latency_ms"] >= 0
        assert event["execution_latency_ms"] >= 0
        assert event["retry_outcome"] == "succeeded"
        assert "breaker_transition" in event
        serialized = json.dumps(event, ensure_ascii=False)
        assert "PRIVATE" not in serialized
        assert "sk-private" not in serialized
        assert "https://" not in serialized
    finally:
        provider.close()


def test_probe_uses_the_named_service_scheduler_and_returns_safe_observation():
    raw = _Chat()
    provider = _provider(chat=raw)
    try:
        observation = provider.probe("chat", actor_id="admin", allow_half_open=True)
        assert observation.service_id == "chat"
        assert observation.config_fingerprint == "fp-chat"
        assert observation.status == "ok"
        assert observation.code == "ok"
        assert observation.trigger == "manual_test"
        assert observation.support_id.startswith("mdl-")
        assert observation.latency_ms >= 0
        assert observation.occurred_at.endswith("+00:00")
        assert raw.calls[0]["kwargs"] == {
            "max_retries": 0,
            "bypass_cache": True,
            "thinking_mode": "disabled",
        }
    finally:
        provider.close()


def test_probe_supports_unbound_service_without_product_workload_attribution():
    service = _service("spare", "chat", 1)
    events = _EventLog()
    provider = RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        events,
        registry=SystemModelServiceRegistry({"spare": service}, {}),
        chat_factory=lambda _service: _Chat(),
    )
    try:
        observation = provider.probe("spare", actor_id="admin", allow_half_open=False)
        assert observation.status == "ok"
        event = events.events[-1]
        assert event["workload_id"] == "service_probe"
        assert event["workload_label"] == "模型服务测试"
        assert event["service_id"] == "spare"
    finally:
        provider.close()


def test_provider_observation_persistence_cannot_delay_or_replace_failure():
    observer_entered = threading.Event()
    observer_release = threading.Event()

    class FailingChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            raise TimeoutError("private upstream endpoint timed out")

    def blocking_observer(_observation):
        observer_entered.set()
        assert observer_release.wait(2)
        raise RuntimeError("sqlite observation write failed")

    provider = _provider(chat=FailingChat(), observation_sink=blocking_observer)
    try:
        started = time.perf_counter()
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            provider.chat("ask_answer").chat_json([], "{}")
        elapsed = time.perf_counter() - started
        assert caught.value.code == "provider_unavailable"
        assert elapsed < 0.5
        assert observer_entered.wait(1)
    finally:
        observer_release.set()
        provider.close()


def test_provider_emits_failure_then_one_recovery_but_not_ordinary_success():
    observations = []
    observed = threading.Event()

    class FlakyChat(_Chat):
        def __init__(self):
            super().__init__()
            self.remaining_failures = 1

        def chat_json(self, messages, response_schema_hint, **kwargs):
            if self.remaining_failures:
                self.remaining_failures -= 1
                raise TimeoutError("private timeout")
            return super().chat_json(messages, response_schema_hint, **kwargs)

    def observer(value):
        observations.append(value)
        if len(observations) >= 2:
            observed.set()

    provider = _provider(chat=FlakyChat(), observation_sink=observer)
    try:
        with pytest.raises(provider_mod.ModelInvocationError):
            provider.chat("ask_answer").chat_json([], "{}")
        assert provider.chat("ask_answer").chat_json([], "{}") == '{"ok": true}'
        assert provider.chat("ask_answer").chat_json([], "{}") == '{"ok": true}'
        assert observed.wait(1)
        assert [(item.status, item.trigger) for item in observations] == [
            ("error", "observed_failure"),
            ("ok", "recovery_probe"),
        ]
        assert {item.service_id for item in observations} == {"chat"}
        assert {item.config_fingerprint for item in observations} == {"fp-chat"}
    finally:
        provider.close()


def test_ignored_programming_error_does_not_arm_or_emit_recovery():
    observations = []

    class InvalidThenSuccessfulChat(_Chat):
        def __init__(self):
            super().__init__()
            self.first = True

        def chat_json(self, messages, response_schema_hint, **kwargs):
            if self.first:
                self.first = False
                raise ValueError("local programming error")
            return super().chat_json(messages, response_schema_hint, **kwargs)

    provider = _provider(
        chat=InvalidThenSuccessfulChat(), observation_sink=observations.append
    )
    client = provider.chat("ask_answer")
    with pytest.raises(provider_mod.ModelInvocationError):
        client.chat_json([], "{}")
    assert client.chat_json([], "{}") == '{"ok": true}'
    provider.close()

    assert observations == []


@pytest.mark.parametrize("failure", ["malformed", "transient"])
def test_provider_failure_classes_emit_one_failure_and_one_recovery(failure):
    observations = []
    observed = threading.Event()

    class FailingThenSuccessfulChat(_Chat):
        def __init__(self):
            super().__init__()
            self.first = True

        def chat_json(self, messages, response_schema_hint, **kwargs):
            if not self.first:
                return super().chat_json(messages, response_schema_hint, **kwargs)
            self.first = False
            if failure == "malformed":
                return "[]"
            if failure == "transient":
                raise TimeoutError("provider timeout")
            raise AssertionError(failure)

    def observer(value):
        observations.append(value)
        if len(observations) == 2:
            observed.set()

    provider = _provider(
        chat=FailingThenSuccessfulChat(), observation_sink=observer
    )
    try:
        client = provider.chat("ask_answer")
        with pytest.raises(provider_mod.ModelInvocationError):
            client.chat_json([], "{}")
        assert client.chat_json([], "{}") == '{"ok": true}'
        assert observed.wait(1)
        assert [(item.status, item.trigger) for item in observations] == [
            ("error", "observed_failure"),
            ("ok", "recovery_probe"),
        ]
    finally:
        provider.close()


def test_fatal_provider_failure_is_observed_and_opens_without_false_recovery():
    observations = []

    class FatalProviderError(RuntimeError):
        status_code = 401

    class FatalChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            raise FatalProviderError("provider rejected credentials")

    provider = _provider(chat=FatalChat(), observation_sink=observations.append)
    client = provider.chat("ask_answer")
    with pytest.raises(provider_mod.ModelInvocationError):
        client.chat_json([], "{}")
    provider.close()

    assert [(item.status, item.trigger) for item in observations] == [
        ("error", "observed_failure")
    ]
    assert provider.scheduler_snapshot("chat").breaker_state == "open"


def test_completion_order_not_delayed_caller_resolution_drives_recovery():
    failure_entered = threading.Event()
    failure_release = threading.Event()
    failure_finished = threading.Event()
    failure_future_done = threading.Event()
    failure_resolution_release = threading.Event()
    observed = threading.Event()
    observations = []

    class OrderedChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            if messages == ["fail"]:
                failure_entered.set()
                assert failure_release.wait(2)
                failure_finished.set()
                raise TimeoutError("older provider failure")
            assert failure_finished.wait(2)
            return self.result

    def observer(value):
        observations.append(value)
        if len(observations) == 2:
            observed.set()

    provider = _provider(
        registry=_registry(maximum=2),
        chat=OrderedChat(),
        observation_sink=observer,
    )
    client = provider.chat("ask_answer")
    original_resolve = client._resolve

    def delayed_failure_resolve(call):
        if threading.current_thread().name.startswith("delayed-failure"):
            call.future.add_done_callback(lambda _future: failure_future_done.set())
            assert failure_future_done.wait(2)
            assert failure_resolution_release.wait(2)
        return original_resolve(call)

    client._resolve = delayed_failure_resolve
    failure_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="delayed-failure"
    )
    success_executor = ThreadPoolExecutor(max_workers=1)
    try:
        failed = failure_executor.submit(client.chat_json, ["fail"], "{}")
        assert failure_entered.wait(1)
        succeeded = success_executor.submit(client.chat_json, ["success"], "{}")
        failure_release.set()
        assert failure_future_done.wait(1)
        assert succeeded.result(timeout=2) == '{"ok": true}'
        assert observed.wait(1)
        assert [(item.status, item.trigger) for item in observations] == [
            ("error", "observed_failure"),
            ("ok", "recovery_probe"),
        ]
        assert observations[0].occurred_at <= observations[1].occurred_at
        failure_resolution_release.set()
        with pytest.raises(provider_mod.ModelInvocationError):
            failed.result(timeout=2)
    finally:
        failure_release.set()
        failure_resolution_release.set()
        provider.close()
        failure_executor.shutdown()
        success_executor.shutdown()


def test_close_drains_failure_observation_even_when_caller_resolves_late():
    transport_entered = threading.Event()
    transport_release = threading.Event()
    future_done = threading.Event()
    resolution_release = threading.Event()
    observer_entered = threading.Event()
    observer_release = threading.Event()
    observations = []

    class BlockingFailureChat(_Chat):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            transport_entered.set()
            assert transport_release.wait(2)
            raise TimeoutError("provider failed during close")

    def observer(value):
        observations.append(value)
        observer_entered.set()
        assert observer_release.wait(2)

    provider = _provider(
        registry=_registry(maximum=1),
        chat=BlockingFailureChat(),
        observation_sink=observer,
    )
    client = provider.chat("ask_answer")
    original_resolve = client._resolve

    def delayed_resolve(call):
        call.future.add_done_callback(lambda _future: future_done.set())
        assert future_done.wait(2)
        assert resolution_release.wait(2)
        return original_resolve(call)

    client._resolve = delayed_resolve
    caller = ThreadPoolExecutor(max_workers=1)
    closer = ThreadPoolExecutor(max_workers=1)
    try:
        result = caller.submit(client.chat_json, [], "{}")
        assert transport_entered.wait(1)
        closing = closer.submit(provider.close)
        transport_release.set()
        assert future_done.wait(1)
        assert observer_entered.wait(1)
        time.sleep(0.03)
        assert not closing.done()
        observer_release.set()
        closing.result(timeout=2)
        assert [(item.status, item.trigger) for item in observations] == [
            ("error", "observed_failure")
        ]
        resolution_release.set()
        with pytest.raises(provider_mod.ModelInvocationError):
            result.result(timeout=2)
    finally:
        transport_release.set()
        observer_release.set()
        resolution_release.set()
        provider.close()
        caller.shutdown()
        closer.shutdown()


def test_close_cancels_queued_rerank_call_without_deadlock():
    entered = threading.Event()
    release = threading.Event()

    class BlockingReranker(_Reranker):
        def _rerank_batch(self, query, documents):
            entered.set()
            assert release.wait(2)
            return [{"index": 0, "relevance_score": 1.0}]

    provider = _provider(
        registry=_registry(maximum=1), reranker=BlockingReranker()
    )
    client = provider.rerank("retrieval_rerank")
    executor = ThreadPoolExecutor(max_workers=3)
    try:
        active = executor.submit(client.rerank, "q", ["a"])
        assert entered.wait(1)
        queued = executor.submit(client.rerank, "q", ["b"])
        deadline = time.monotonic() + 1
        while provider.scheduler_snapshot("rerank").queued != 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        closing = executor.submit(provider.close)
        release.set()
        assert active.result(timeout=2) == [0]
        assert queued.result(timeout=2) == [0]
        closing.result(timeout=2)
    finally:
        release.set()
        provider.close()
        executor.shutdown()


def test_close_prevents_unmaterialized_construction_and_closes_raw_once():
    constructed = []
    provider = RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        _EventLog(),
        registry=_registry(),
        chat_factory=lambda service: constructed.append(service.id) or _Chat(),
    )
    provider.close()
    provider.close()

    with pytest.raises(Exception) as caught:
        provider.chat("ask_answer")
    assert getattr(caught.value, "code", "") == "model_service_unavailable"
    assert constructed == []


def test_close_stops_all_service_admission_before_waiting_for_active_work():
    services = {
        "first": _service("first", "chat", 1),
        "second": _service("second", "chat", 1),
    }
    registry = SystemModelServiceRegistry(
        services,
        {"ask_answer": "first", "query_rewrite": "second"},
    )
    entered = threading.Event()
    release = threading.Event()

    class ClosableChat(_Chat):
        def __init__(self, *, block=False) -> None:
            super().__init__()
            self.block = block
            self.close_calls = 0
            self.transport_calls = 0

        def chat_json(self, messages, response_schema_hint, **kwargs):
            self.transport_calls += 1
            if self.block:
                entered.set()
                assert release.wait(2)
            return self.result

        def close(self):
            self.close_calls += 1

    raws = {"first": ClosableChat(block=True), "second": ClosableChat()}
    provider = RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        _EventLog(),
        registry=registry,
        chat_factory=lambda service: raws[service.id],
    )
    first = provider.chat("ask_answer")
    second = provider.chat("query_rewrite")
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        active = executor.submit(first.chat_json, [], "{}")
        assert entered.wait(1)
        closing = executor.submit(provider.close)
        deadline = time.monotonic() + 1
        while not provider._closing:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            second.chat_json([], "{}")
        assert caught.value.code == "model_service_unavailable"
        assert raws["second"].transport_calls == 0
        release.set()
        assert active.result(timeout=2) == '{"ok": true}'
        closing.result(timeout=2)
        provider.close()
        assert [raw.close_calls for raw in raws.values()] == [1, 1]
    finally:
        release.set()
        executor.shutdown()


def test_close_and_submit_share_one_atomic_provider_admission_boundary():
    services = {
        "first": _service("first", "chat", 1),
        "second": _service("second", "chat", 1),
    }
    registry = SystemModelServiceRegistry(
        services,
        {"ask_answer": "first", "query_rewrite": "second"},
    )

    class ClosableChat(_Chat):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.transport_calls = 0

        def chat_json(self, messages, response_schema_hint, **kwargs):
            self.transport_calls += 1
            return self.result

        def close(self):
            self.close_calls += 1

    raws = {"first": ClosableChat(), "second": ClosableChat()}
    provider = RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        _EventLog(),
        registry=registry,
        chat_factory=lambda service: raws[service.id],
    )
    first = provider.chat("ask_answer")
    second = provider.chat("query_rewrite")
    stop_entered = threading.Event()
    allow_stop = threading.Event()
    submit_entered = threading.Event()
    original_stop_admission = first._runtime.scheduler.stop_admission

    def blocked_stop_admission():
        stop_entered.set()
        assert allow_stop.wait(2)
        original_stop_admission()

    first._runtime.scheduler.stop_admission = blocked_stop_admission
    executor = ThreadPoolExecutor(max_workers=3)
    try:
        closing = executor.submit(provider.close)
        assert stop_entered.wait(1)
        assert provider._closing is True
        assert provider._closed is False

        def submit_after_close_started():
            submit_entered.set()
            return second.chat_json([], "{}")

        late_call = executor.submit(submit_after_close_started)
        assert submit_entered.wait(1)
        with pytest.raises(provider_mod.ModelInvocationError) as caught:
            late_call.result(timeout=2)
        assert caught.value.code == "model_service_unavailable"
        assert raws["second"].transport_calls == 0

        concurrent_close = executor.submit(provider.close)
        allow_stop.set()
        closing.result(timeout=2)
        concurrent_close.result(timeout=2)
        assert raws["second"].transport_calls == 0
        assert [raw.close_calls for raw in raws.values()] == [1, 1]
    finally:
        allow_stop.set()
        executor.shutdown()
