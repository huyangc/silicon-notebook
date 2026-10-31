"""Negotiation, cancellation, cache identity and fallback at the real transport."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core import llm
from app.core.llm import OpenAICompatibleClient
from app.core.model_schema import StructuredModelResponseError
from app.domain.cancellation import AskCancelled


ANSWER_HINT = '{"answer":"","grounded":true}'


class Cache:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value, tag=""):
        self.values[key] = value
        self.writes.append(key)


def response(data, finish_reason="stop", usage=None):
    content = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )], usage=usage)


class Stream:
    def __init__(self, data, before=None):
        self.data, self.before, self.closed = data, before, False

    def __iter__(self):
        if self.before:
            self.before()
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=json.dumps(self.data)), finish_reason="stop",
        )], usage=None)

    def close(self):
        self.closed = True


class SDK:
    def __init__(self):
        self.calls, self.streams = [], []
        self.before_probe = None
        self.reject_answer_schema = False
        self.business_response = None

    def create(self, **kwargs):
        self.calls.append(kwargs)
        fmt = kwargs.get("response_format", {})
        fields = fmt.get("json_schema", {}).get("schema", {}).get("properties", {})
        if "proof" in fields:
            # OpenAI-compatible services need not accept a vendor's thinking
            # control; capability detection must not depend on that extension.
            assert "extra_body" not in kwargs
            stream = Stream({"proof": fields["proof"]["enum"][0], "count": 7}, self.before_probe)
            self.streams.append(stream)
            return stream
        if self.reject_answer_schema and "answer" in fields:
            raise ValueError("response_format schema subset unsupported")
        if self.business_response is not None:
            return self.business_response
        data = {"query": "query"} if "query" in fields else {"answer": "answer", "grounded": True}
        return Stream(data) if kwargs.get("stream") else response(data)


class AsyncStreamAdapter:
    """Only the probe uses async I/O; the business SDK fake remains synchronous."""

    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        for chunk in self.stream:
            yield chunk

    async def close(self):
        self.stream.close()


class AsyncSDKAdapter:
    def __init__(self, sdk):
        self.sdk, self.closed = sdk, False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        return AsyncStreamAdapter(self.sdk.create(**kwargs))

    async def close(self):
        self.closed = True


def client(tmp_path, sdk, *, enabled=True, cache=None):
    result = OpenAICompatibleClient(Settings(
        _env_file=None, llm_cache_enabled=False, llm_log_enabled=False,
        storage_dir=str(tmp_path),
    ), base_url="https://model.invalid/v1", api_key="test", model="test",
        enable_json_schema_probe=enabled, cache=cache)
    result._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=sdk.create),
    ))
    result._probe_client = lambda timeout: AsyncSDKAdapter(sdk)
    return result


def call(result, **kwargs):
    return result.chat_json([{"role": "user", "content": "question"}], ANSWER_HINT, **kwargs)


def probe_count(sdk):
    return sum("proof" in row.get("response_format", {}).get("json_schema", {}).get(
        "schema", {},
    ).get("properties", {}) for row in sdk.calls)


def test_concurrent_first_calls_share_one_probe(tmp_path):
    sdk = SDK()
    entered, release = threading.Event(), threading.Event()

    def hold_probe():
        entered.set()
        assert release.wait(30)

    sdk.before_probe = hold_probe
    result = client(tmp_path, sdk)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, result, bypass_cache=True)
        assert entered.wait(30)
        second = pool.submit(call, result, bypass_cache=True)
        release.set()
        assert json.loads(first.result(timeout=30))["answer"] == "answer"
        assert json.loads(second.result(timeout=30))["answer"] == "answer"
    assert probe_count(sdk) == 1
    assert sdk.streams[0].closed


def test_cancelled_probe_does_not_remember_a_negative_capability(tmp_path):
    sdk = SDK()
    cancelled = threading.Event()
    sdk.before_probe = cancelled.set
    result = client(tmp_path, sdk)
    stats = {}
    with pytest.raises(AskCancelled):
        call(result, cancel_event=cancelled, call_stats=stats, bypass_cache=True)
    assert stats["status"] == "cancelled" and stats["attempts"] == 1
    assert sdk.streams[0].closed
    sdk.before_probe = None
    assert json.loads(call(result, bypass_cache=True))["answer"] == "answer"
    assert probe_count(sdk) == 2
    assert sdk.calls[-1]["response_format"]["type"] == "json_schema"


def test_probe_deadline_closes_a_read_stalled_after_a_chunk(tmp_path):
    class StalledStream:
        closed = False
        read_cancelled = False

        async def __aiter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="{"), finish_reason=None,
            )], usage=None)
            try:
                await asyncio.Event().wait()
            finally:
                self.read_cancelled = True

        async def close(self):
            self.closed = True

    class StalledReader(AsyncSDKAdapter):
        async def create(self, **kwargs):
            sdk.calls.append(kwargs)
            return stalled

    sdk, stalled = SDK(), StalledStream()
    probe = StalledReader(sdk)
    result = client(tmp_path, sdk)
    result._probe_client = lambda timeout: probe
    stats = {}
    assert json.loads(call(result, timeout=0.01, call_stats=stats, bypass_cache=True))["answer"] == "answer"
    assert stalled.closed and stalled.read_cancelled and probe.closed
    assert stats["attempts"] == 2
    assert result._json_schema_capability == "unverified"
    assert sdk.calls[-1]["response_format"]["type"] == "json_object"
    call(result, bypass_cache=True)
    assert probe_count(sdk) == 1


def test_probe_deadline_cancels_stalled_opening_and_closes_async_client(tmp_path):
    class StalledOpening(AsyncSDKAdapter):
        opening_cancelled = False

        async def create(self, **kwargs):
            self.sdk.calls.append(kwargs)
            try:
                await asyncio.Event().wait()
            finally:
                self.opening_cancelled = True

    sdk = SDK()
    probe = StalledOpening(sdk)
    result = client(tmp_path, sdk)
    result._probe_client = lambda timeout: probe
    stats = {}
    assert json.loads(call(result, timeout=0.01, call_stats=stats, bypass_cache=True))["answer"] == "answer"
    assert probe.closed and probe.opening_cancelled
    assert stats["attempts"] == 2
    assert probe_count(sdk) == 1
    assert result._json_schema_capability == "unverified"
    assert sdk.calls[-1]["response_format"]["type"] == "json_object"


def test_timed_out_probe_does_not_give_stalled_cleanup_another_budget(tmp_path):
    class StalledClose:
        close_attempted = False
        close_cancelled = False

        async def __aiter__(self):
            yield SimpleNamespace(choices=[], usage=None)
            await asyncio.Event().wait()

        async def close(self):
            self.close_attempted = True
            try:
                await asyncio.Event().wait()
            finally:
                self.close_cancelled = True

    class StreamWithStalledClose(AsyncSDKAdapter):
        async def create(self, **kwargs):
            self.sdk.calls.append(kwargs)
            return stream

    sdk, stream = SDK(), StalledClose()
    probe = StreamWithStalledClose(sdk)
    result = client(tmp_path, sdk)
    result._probe_client = lambda timeout: probe
    assert json.loads(call(result, timeout=0.01, bypass_cache=True))["answer"] == "answer"
    assert stream.close_attempted and stream.close_cancelled and probe.closed
    assert result._json_schema_capability == "unverified"


def test_usage_option_fallback_keeps_the_original_probe_deadline(tmp_path, monkeypatch):
    deadlines = []
    original_timeout = asyncio.timeout

    def timeout_scope(delay):
        scope = original_timeout(delay)
        deadlines.append(scope)
        return scope

    class RejectUsageThenStall(AsyncSDKAdapter):
        opening_cancelled = False

        async def create(self, **kwargs):
            self.sdk.calls.append(kwargs)
            if "stream_options" in kwargs:
                raise ValueError("stream_options.include_usage unsupported")
            # The second request is already inside the first request's total
            # timeout scope, rather than acquiring another whole timeout.
            assert len(deadlines) == 1 and not deadlines[0].expired()
            try:
                await asyncio.Event().wait()
            finally:
                self.opening_cancelled = True

    monkeypatch.setattr(llm.asyncio, "timeout", timeout_scope)
    sdk = SDK()
    probe = RejectUsageThenStall(sdk)
    result = client(tmp_path, sdk)
    result._probe_client = lambda timeout: probe
    stats = {}
    assert json.loads(call(result, timeout=0.01, call_stats=stats, bypass_cache=True))["answer"] == "answer"
    assert len(deadlines) == 1 and deadlines[0].expired()
    assert probe.closed and probe.opening_cancelled
    assert stats["attempts"] == 3
    assert probe_count(sdk) == 2
    assert result._stream_usage_options_supported is False
    assert "stream_options" not in sdk.calls[1]
    assert result._json_schema_capability == "unverified"


def test_cancellation_interrupts_probe_while_opening_without_remembering_status(tmp_path):
    entered, cancelled = threading.Event(), threading.Event()

    class CancellableOpening(AsyncSDKAdapter):
        opening_cancelled = False

        async def create(self, **kwargs):
            self.sdk.calls.append(kwargs)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.opening_cancelled = True

    sdk = SDK()
    probe = CancellableOpening(sdk)
    result = client(tmp_path, sdk)
    result._probe_client = lambda timeout: probe
    stats = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, result, cancel_event=cancelled, call_stats=stats, bypass_cache=True)
        assert entered.wait(30)
        cancelled.set()
        with pytest.raises(AskCancelled):
            pending.result(timeout=30)
    assert probe.closed and probe.opening_cancelled
    assert stats["status"] == "cancelled" and stats["attempts"] == 1
    assert result._json_schema_capability is None
    assert len(sdk.calls) == 1


def test_rejected_schema_is_remembered_without_disabling_other_schemas(tmp_path):
    sdk, cache = SDK(), Cache()
    sdk.reject_answer_schema = True
    result = client(tmp_path, sdk, cache=cache)
    assert json.loads(call(result, response_validator=lambda _: True))["answer"] == "answer"
    assert cache.writes == []  # fallback is never cached under the strict key
    assert json.loads(call(result, response_validator=lambda _: True))["answer"] == "answer"
    assert len(cache.writes) == 1
    result.chat_json([], '{"query":""}', bypass_cache=True)
    assert probe_count(sdk) == 1
    formats = [row.get("response_format", {}).get("type") for row in sdk.calls]
    assert formats == ["json_schema", "json_schema", "json_object", "json_object", "json_schema"]


def test_schema_cache_is_distinct_and_revalidates_cached_content(tmp_path):
    sdk, cache = SDK(), Cache()
    legacy = client(tmp_path, sdk, enabled=False, cache=cache)
    call(legacy, response_validator=lambda _: True)
    strict = client(tmp_path, sdk, cache=cache)
    call(strict, response_validator=lambda _: True)
    assert len(set(cache.writes)) == 2
    before = len(sdk.calls)
    stats = {}
    call(strict, response_validator=lambda _: True, call_stats=stats)
    assert len(sdk.calls) == before
    assert stats["status"] == "cache_hit" and stats["attempts"] == 0
    cache.values[cache.writes[-1]] = '{"answer":[],"grounded":true}'
    call(strict, response_validator=lambda _: True)
    assert len(sdk.calls) == before + 1
    assert probe_count(sdk) == 1


def test_strict_truncation_preserves_finish_reason_and_billed_usage(tmp_path):
    sdk = SDK()
    sdk.business_response = response('{"answer":"unfinished', "length", SimpleNamespace(
        prompt_tokens=11, completion_tokens=7, total_tokens=18,
    ))
    result, stats = client(tmp_path, sdk), {}
    with pytest.raises(StructuredModelResponseError):
        call(result, call_stats=stats, bypass_cache=True)
    assert stats["finish_reason"] == "length"
    assert stats["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert stats["attempts"] == 2


def test_completed_schema_failures_downgrade_only_that_format_without_caching(tmp_path):
    sdk, cache = SDK(), Cache()
    result = client(tmp_path, sdk, cache=cache)
    events = []
    result._json_schema_observer = lambda *event: events.append(event)
    sdk.business_response = response({"answer": "usable partial answer"})
    for _ in range(result.settings.model_json_schema_failure_threshold):
        with pytest.raises(StructuredModelResponseError):
            call(result, response_validator=lambda _: True)
    assert cache.writes == []
    assert json.loads(call(result, response_validator=lambda _: True)) == {"answer": "usable partial answer"}
    assert sdk.calls[-1]["response_format"]["type"] == "json_object"
    assert len(cache.writes) == 1
    sdk.business_response = None
    result.chat_json([], '{"query":""}', bypass_cache=True)
    assert sdk.calls[-1]["response_format"]["type"] == "json_schema"
    assert probe_count(sdk) == 1
    assert [event[:2] for event in events] == [
        ("supported", "schema_enforced"), ("unsupported", "schema_not_enforced"),
    ]


def test_live_schema_success_resets_consecutive_failures(tmp_path):
    sdk = SDK()
    result = client(tmp_path, sdk)
    sdk.business_response = response({"answer": "missing grounded"})
    with pytest.raises(StructuredModelResponseError):
        call(result, bypass_cache=True)
    sdk.business_response = None
    call(result, bypass_cache=True)
    sdk.business_response = response({"answer": "missing grounded"})
    for _ in range(result.settings.model_json_schema_failure_threshold):
        with pytest.raises(StructuredModelResponseError):
            call(result, bypass_cache=True)
    call(result, bypass_cache=True)
    assert sdk.calls[-1]["response_format"]["type"] == "json_object"
    assert probe_count(sdk) == 1


@pytest.mark.parametrize("raw", [
    '[{"answer":"valid","grounded":true}]',
    'prose {"answer":"valid","grounded":true} prose',
    '```json\n{"answer":"valid","grounded":true}\n```',
])
def test_strict_validation_does_not_extract_an_object_from_invalid_raw_reply(tmp_path, raw):
    sdk, cache = SDK(), Cache()
    sdk.business_response = response(raw)
    result = client(tmp_path, sdk, cache=cache)
    with pytest.raises(StructuredModelResponseError) as caught:
        call(result, response_validator=lambda _: True)
    assert caught.value.content == raw
    assert cache.writes == []


@pytest.mark.parametrize("timeout", [0, 301, "bad"])
def test_probe_timeout_has_validated_deployment_rails(timeout):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_json_schema_probe_timeout_seconds=timeout)


@pytest.mark.parametrize("threshold", [0, 101, "bad", 1.5])
def test_schema_failure_threshold_has_validated_deployment_rails(threshold):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_json_schema_failure_threshold=threshold)
