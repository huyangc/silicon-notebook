"""Negotiation, cancellation, cache identity and fallback at the real transport."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
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


def client(tmp_path, sdk, *, enabled=True, cache=None):
    result = OpenAICompatibleClient(Settings(
        _env_file=None, llm_cache_enabled=False, llm_log_enabled=False,
        storage_dir=str(tmp_path),
    ), base_url="https://model.invalid/v1", api_key="test", model="test",
        enable_json_schema_probe=enabled, cache=cache)
    result._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=sdk.create),
    ))
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
