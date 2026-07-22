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


def _service(service_id: str, kind: str, maximum: int = 2) -> ModelServiceDefinition:
    return ModelServiceDefinition(
        id=service_id,
        display_name=f"安全服务-{service_id}",
        kind=kind,
        protocol="openai",
        base_url=f"https://{service_id}.example/v1",
        model=f"safe-{service_id}",
        api_key_env=f"{service_id.upper()}_KEY",
        api_key="sk-private",
        max_concurrency=maximum,
        fingerprint=f"fp-{service_id}",
    )


def _registry(*, maximum: int = 2) -> SystemModelServiceRegistry:
    services = {
        "chat": _service("chat", "chat", maximum),
        "embed": _service("embed", "embedding", maximum),
        "rerank": _service("rerank", "rerank", maximum),
    }
    return SystemModelServiceRegistry(
        services,
        {
            "ask_answer": "chat",
            "query_rewrite": "chat",
            "retrieval_query_embedding": "embed",
            "retrieval_rerank": "rerank",
        },
    )


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


def _provider(*, registry=None, chat=None, embedder=None, reranker=None, events=None):
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
    provider = _provider()
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
        while not provider._closed:
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
