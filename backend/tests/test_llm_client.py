"""Regression tests for the LLM client's fail-fast behavior: a stalled
connection must NOT be amplified into a ~6-minute block by SDK auto-retries or
by the JSON-mode -> plain-mode fallback."""
import threading
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

import app.core.llm as llm_mod
from app.core.cache import llm_key
from app.core.config import Settings
from app.core.llm import (
    OpenAICompatibleClient,
    provider_messages,
    serialize_provider_messages,
)


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


class _Stream:
    def __init__(self, content='{"ok":1}', usage=None, before_usage=None):
        self.closed = False
        self.before_usage = before_usage
        self._chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=content),
                    finish_reason="stop",
                )],
                usage=None,
            ),
            # OpenAI's include_usage trailer has no choices. The production
            # loop must inspect usage before its empty-choice fast path.
            SimpleNamespace(choices=[], usage=usage),
        ]

    def __iter__(self):
        yield self._chunks[0]
        if self.before_usage is not None:
            self.before_usage()
        yield self._chunks[1]

    def close(self):
        self.closed = True


class _RecordingInteractionLogger:
    def __init__(self):
        self.records = []

    def clip(self, value):
        return str(value)

    def log(self, record):
        self.records.append(record)


def _api_status_error(status, message):
    request = httpx.Request("POST", "https://x/chat/completions")
    response = httpx.Response(
        status,
        request=request,
        json={"error": {"message": message}},
    )
    return APIStatusError(message, response=response, body=response.json())


def _make(monkeypatch, create, *, model="m"):
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    c = OpenAICompatibleClient(
        Settings(_env_file=None),
        base_url="https://x",
        api_key="k",
        model=model,
    )
    monkeypatch.setattr(c, "client", lambda: _FakeOpenAI(create))
    return c


class _RecordingCache:
    """Cache double that records the key chat_json resolves (always a miss)."""

    def __init__(self):
        self.gets = []
        self.puts = []

    def get(self, key):
        self.gets.append(key)
        return None

    def put(self, key, value, tag=""):
        self.puts.append((key, value, tag))


def _common_prefix_len(left: bytes, right: bytes) -> int:
    n = 0
    for a, b in zip(left, right):
        if a != b:
            break
        n += 1
    return n


def test_provider_messages_is_wrapper_then_caller_messages():
    """(T-PS1-a) The pure function reproduces the wrapper verbatim.

    The literal wrapper text is asserted here, not paraphrased: this is the
    byte-for-byte record of what the extraction moved. A wrapper reworded by a
    later edit changes every provider cache prefix and every recorded
    ``message_prefix_bytes`` at once, so it must not be able to change silently.
    """
    caller = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "partial"},
    ]

    out = provider_messages(caller, "{'a': 1}")

    assert out == [
        {
            "role": "system",
            "content": (
                "You are the extraction and reasoning engine for "
                "silicon-notebook. Return valid JSON only, no markdown fences. "
                "Schema hint: {'a': 1}"
            ),
        },
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "partial"},
    ]
    # A fresh list, and the caller's own mappings passed through untouched.
    assert out[1] is caller[0]
    assert caller == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "partial"},
    ]


def test_chat_json_sends_exactly_the_pure_functions_messages(monkeypatch):
    """(T-PS1-d) What goes on the wire IS ``provider_messages``' output.

    Mutation: reintroduce a second inline wrapper assembly in ``chat_json`` with
    any wording drift and this equality fails — which is the whole point of the
    extraction, since a measurement layer computing prefixes off the pure
    function would otherwise be describing a request that was never sent.
    """
    create = _FakeCreate([_Resp()])
    client = _make(monkeypatch, create)
    msgs = [{"role": "user", "content": "hi"}]

    client.chat_json(msgs, "{}")

    assert create.calls[0]["messages"] == provider_messages(msgs, "{}")


def test_llm_key_still_keys_on_the_provider_facing_messages(monkeypatch):
    """(T-PS1-d) The cache key keeps eating the wrapped list, not the raw one."""
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    cache = _RecordingCache()
    client = OpenAICompatibleClient(
        Settings(_env_file=None),
        base_url="https://x",
        api_key="k",
        model="m",
        cache=cache,
    )
    monkeypatch.setattr(client, "client", lambda: _FakeOpenAI(_FakeCreate([_Resp()])))
    msgs = [{"role": "user", "content": "hi"}]

    client.chat_json(msgs, "{}", response_validator=lambda _c: True)

    assert cache.gets == [
        llm_key(
            "m",
            provider_messages(msgs, "{}"),
            "{}",
            "https://x",
            temperature=1.0,
            top_p=1.0,
            max_tokens=client.settings.openai_compat_max_tokens,
            thinking_mode=None,
        )
    ]
    assert cache.gets[0] != llm_key(
        "m", msgs, "{}", "https://x",
        temperature=1.0, top_p=1.0,
        max_tokens=client.settings.openai_compat_max_tokens,
        thinking_mode=None,
    )


def test_serialize_provider_messages_is_deterministic_and_utf8():
    """(T-PS1-b) Same input -> same bytes, and non-ASCII counts as UTF-8."""
    msgs = provider_messages([{"role": "user", "content": "中文 body"}], "{}")

    first = serialize_provider_messages(msgs)
    second = serialize_provider_messages(msgs)

    assert first == second
    assert isinstance(first, bytes)
    # Length headers count BYTES, not characters: 中文 is 3 bytes per char.
    assert b"11:" + "中文 body".encode("utf-8") in first
    assert serialize_provider_messages([]) == b""


def test_serialize_provider_messages_frames_cannot_be_forged_from_content():
    """(T-PS1-b) A body that writes the framing syntax cannot move a boundary.

    Two different message lists whose concatenated text is identical must
    serialize differently; with a plain delimiter they would collide and a
    hostile (or merely unlucky) document quotation could make two structurally
    different requests look byte-identical.
    """
    forged = serialize_provider_messages(
        [{"role": "user", "content": "4:userpayload"}]
    )
    genuine = serialize_provider_messages(
        [{"role": "user", "content": ""}, {"role": "user", "content": "payload"}]
    )

    assert forged != genuine


def test_serialize_provider_messages_prefix_covers_every_earlier_message():
    """(T-PS1-c) Changing only the tail content keeps all earlier bytes shared.

    Framing is a plain concatenation of per-message frames, so the head's
    serialization is a LITERAL prefix of the whole; the divergence caused by a
    new tail lands inside the tail's own frame and never earlier. (It lands a
    couple of bytes into that frame, at the length header, rather than exactly
    at the boundary — the header is what makes the framing unforgeable.)
    """
    head = provider_messages([{"role": "user", "content": "stable"}], "{}")
    turn_a = head + [{"role": "assistant", "content": "turn one"}]
    turn_b = head + [{"role": "assistant", "content": "turn two but longer"}]

    head_bytes = serialize_provider_messages(head)
    a_bytes = serialize_provider_messages(turn_a)
    b_bytes = serialize_provider_messages(turn_b)
    shared = _common_prefix_len(a_bytes, b_bytes)

    assert a_bytes.startswith(head_bytes)
    assert b_bytes.startswith(head_bytes)
    assert shared >= len(head_bytes)
    assert shared < len(a_bytes)
    # And an EARLIER change is not absorbed: the shared prefix collapses.
    moved = provider_messages([{"role": "user", "content": "stabl3"}], "{}")
    moved_bytes = serialize_provider_messages(
        moved + [{"role": "assistant", "content": "turn one"}]
    )
    assert _common_prefix_len(a_bytes, moved_bytes) < len(head_bytes)


def test_client_connection_pool_matches_explicit_service_capacity(monkeypatch):
    captured = {}

    class _HttpClient:
        def __init__(self, *, timeout, limits):
            captured.update(timeout=timeout, limits=limits)

    monkeypatch.setattr(llm_mod.httpx, "Client", _HttpClient)
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kwargs: captured.update(openai=kwargs) or object())
    settings = Settings(_env_file=None)
    client = OpenAICompatibleClient(
        settings,
        base_url="https://safe.example/v1",
        api_key="key",
        model="model",
        max_connections=7,
    )

    client.client()

    assert captured["limits"].max_connections == 7
    assert captured["limits"].max_keepalive_connections == 7


def test_raw_client_preserves_empty_success_for_scheduled_classification(monkeypatch):
    create = _FakeCreate([_Resp("")])
    client = _make(monkeypatch, create)
    assert client.chat_json([{"role": "user", "content": "hi"}], "{}") == ""


def test_explicit_non_thinking_mode_is_sent_and_logged(monkeypatch):
    create = _FakeCreate([_Stream()])
    client = _make(monkeypatch, create, model="gateway-model-alias")
    logger = _RecordingInteractionLogger()
    client.interaction_logger = logger

    assert client.chat_json(
        [{"role": "user", "content": "extract"}],
        "{}",
        cancel_event=threading.Event(),
        thinking_mode="disabled",
    ) == '{"ok":1}'

    assert create.calls[0]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert create.calls[0]["stream"] is True
    assert (
        logger.records[-1]["request"]["thinking_mode"] == "disabled"
    )


def test_thinking_mode_is_not_inferred_from_model_name(monkeypatch):
    create = _FakeCreate([_Resp()])
    client = _make(monkeypatch, create, model="gpt-5")
    logger = _RecordingInteractionLogger()
    client.interaction_logger = logger

    client.chat_json(
        [{"role": "user", "content": "answer"}],
        "{}",
        thinking_mode="enabled",
    )

    assert create.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled"}
    }
    assert logger.records[-1]["request"]["thinking_mode"] == "enabled"


def test_provider_default_request_does_not_send_thinking_extension(monkeypatch):
    create = _FakeCreate([_Resp()])
    client = _make(monkeypatch, create)

    client.chat_json([{"role": "user", "content": "answer"}], "{}")

    assert "extra_body" not in create.calls[0]


def test_streaming_requests_and_logs_exact_usage_trailer(monkeypatch):
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
    )
    stream = _Stream(usage=usage)
    create = _FakeCreate([stream])
    client = _make(monkeypatch, create)
    logger = _RecordingInteractionLogger()
    client.interaction_logger = logger

    assert client.chat_json(
        [{"role": "user", "content": "hi"}],
        "{}",
        cancel_event=threading.Event(),
    ) == '{"ok":1}'

    assert len(create.calls) == 1
    assert create.calls[0]["stream"] is True
    assert create.calls[0]["stream_options"] == {"include_usage": True}
    assert stream.closed is True
    assert logger.records[-1]["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }


def test_stream_usage_option_rejection_falls_back_once_and_is_remembered(monkeypatch):
    unsupported = _api_status_error(
        400, "Unsupported parameter: stream_options.include_usage"
    )
    first = _Stream()
    second = _Stream()
    create = _FakeCreate([unsupported, first, second])
    client = _make(monkeypatch, create)

    for prompt in ("first", "second"):
        assert client.chat_json(
            [{"role": "user", "content": prompt}],
            "{}",
            cancel_event=threading.Event(),
        ) == '{"ok":1}'

    assert len(create.calls) == 3
    assert create.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in create.calls[1]
    assert "stream_options" not in create.calls[2]
    assert first.closed is True
    assert second.closed is True


def test_stream_usage_rejection_stays_monotonic_across_concurrent_calls(monkeypatch):
    unsupported = _api_status_error(
        400, "Unsupported parameter: stream_options.include_usage"
    )
    first_started = threading.Event()
    rejection_seen = threading.Event()

    class _ConcurrentCreate:
        def __init__(self):
            self.calls = []
            self.usage_calls = 0
            self.lock = threading.Lock()

        def __call__(self, **kwargs):
            with self.lock:
                self.calls.append(kwargs)
                if "stream_options" in kwargs:
                    usage_call = self.usage_calls
                    self.usage_calls += 1
                else:
                    usage_call = None
            if usage_call == 0:
                first_started.set()
                assert rejection_seen.wait(timeout=2)
                return _Stream()
            if usage_call == 1:
                rejection_seen.set()
                raise unsupported
            if usage_call is not None:
                raise AssertionError("explicit rejection was overwritten")
            return _Stream()

    create = _ConcurrentCreate()
    client = _make(monkeypatch, create)
    results = []
    errors = []

    def call(prompt):
        try:
            results.append(client.chat_json(
                [{"role": "user", "content": prompt}],
                "{}",
                cancel_event=threading.Event(),
            ))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert first_started.wait(timeout=2)
    second = threading.Thread(target=call, args=("second",))
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert len(results) == 2
    assert client._stream_usage_options_supported is False
    assert client.chat_json(
        [{"role": "user", "content": "third"}],
        "{}",
        cancel_event=threading.Event(),
    ) == '{"ok":1}'
    assert create.usage_calls == 2


def test_stream_usage_fallback_rechecks_cancellation_before_second_request(monkeypatch):
    cancel_event = threading.Event()
    unsupported = _api_status_error(
        400, "Unsupported parameter: stream_options.include_usage"
    )

    class _CancelOnReject:
        def __init__(self):
            self.calls = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            cancel_event.set()
            raise unsupported

    create = _CancelOnReject()
    client = _make(monkeypatch, create)

    with pytest.raises(llm_mod.AskCancelled):
        client.chat_json(
            [{"role": "user", "content": "hi"}],
            "{}",
            cancel_event=cancel_event,
        )

    assert len(create.calls) == 1


def test_stream_cancellation_preserves_already_received_usage_trailer(monkeypatch):
    cancel_event = threading.Event()
    usage = SimpleNamespace(
        prompt_tokens=13,
        completion_tokens=5,
        total_tokens=18,
    )
    stream = _Stream(usage=usage, before_usage=cancel_event.set)
    create = _FakeCreate([stream])
    client = _make(monkeypatch, create)
    logger = _RecordingInteractionLogger()
    client.interaction_logger = logger

    with pytest.raises(llm_mod.AskCancelled):
        client.chat_json(
            [{"role": "user", "content": "hi"}],
            "{}",
            cancel_event=cancel_event,
        )

    assert stream.closed is True
    assert logger.records[-1]["status"] == "cancelled"
    assert logger.records[-1]["usage"] == {
        "prompt_tokens": 13,
        "completion_tokens": 5,
        "total_tokens": 18,
    }


def test_kg_llm_limits_have_bounded_defaults(monkeypatch):
    monkeypatch.delenv("KG_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("KG_LLM_MAX_RETRIES", raising=False)
    settings = Settings(_env_file=None)
    assert settings.kg_llm_timeout_seconds == 60
    assert settings.kg_llm_max_retries == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_http_status_uses_bounded_retry(monkeypatch, status):
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "1")
    monkeypatch.setattr(llm_mod, "sleep_or_cancel", lambda *_a, **_k: None)
    err = _api_status_error(status, "upstream unavailable")
    create = _FakeCreate([err, _Resp()])
    client = _make(monkeypatch, create)
    assert client.chat_json([{"role": "user", "content": "hi"}], "{}") == '{"ok":1}'
    assert len(create.calls) == 2
    assert "response_format" in create.calls[1]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_status_does_not_retry_or_plain_fallback(monkeypatch, status):
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "3")
    create = _FakeCreate([_api_status_error(status, "denied")])
    client = _make(monkeypatch, create)
    with pytest.raises(APIStatusError):
        client.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert len(create.calls) == 1


def test_only_explicit_response_format_rejection_falls_back(monkeypatch):
    rejected = _api_status_error(400, "response_format json_object is unsupported")
    create = _FakeCreate([rejected, _Resp()])
    client = _make(monkeypatch, create)
    assert client.chat_json([{"role": "user", "content": "hi"}], "{}") == '{"ok":1}'
    assert len(create.calls) == 2
    assert "response_format" not in create.calls[1]


def test_connection_error_fails_fast_no_fallback(monkeypatch):
    # Pin retries off to isolate the no-plain-mode-fallback invariant: a single
    # connection error must NOT trigger a second (plain-mode) create call.
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "0")
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


def test_connection_error_retries_then_recovers(monkeypatch):
    """Two transient connection errors then a valid response: chat_json returns
    the content and create is called 3 times (1 + 2 retries)."""
    monkeypatch.setattr(
        llm_mod, "sleep_or_cancel", lambda _seconds, _cancel_event: None
    )
    err = APIConnectionError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err, err, _Resp()])
    c = _make(monkeypatch, create)  # default OPENAI_COMPAT_MAX_RETRIES = 2
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert out == '{"ok":1}'
    assert len(create.calls) == 3  # initial + 2 retries


def test_connection_error_retries_exhausted(monkeypatch):
    """Create always raises a connection error: chat_json raises after exactly
    1 + max_retries attempts."""
    monkeypatch.setattr(
        llm_mod, "sleep_or_cancel", lambda _seconds, _cancel_event: None
    )
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "2")
    err = APIConnectionError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err])  # repeats last behavior forever
    c = _make(monkeypatch, create)
    with pytest.raises(APIConnectionError):
        c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert len(create.calls) == 3  # 1 + 2 retries, no more


def test_non_connection_error_does_not_loop(monkeypatch):
    """A non-connection error on json-mode create triggers exactly ONE plain-mode
    retry (existing fallback) and does NOT enter the connection-retry loop."""
    monkeypatch.setattr(
        llm_mod, "sleep_or_cancel", lambda _seconds, _cancel_event: None
    )
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "5")
    create = _FakeCreate([ValueError("response_format unsupported"), _Resp()])
    c = _make(monkeypatch, create)
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert out == '{"ok":1}'
    assert len(create.calls) == 2  # one fallback, NOT 1+max_retries


def test_per_call_timeout_passed_to_create(monkeypatch):
    """(A) timeout=5 must be forwarded to chat.completions.create as timeout=5."""
    create = _FakeCreate([_Resp()])
    c = _make(monkeypatch, create)
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}", timeout=5)
    assert out == '{"ok":1}'
    assert create.calls[0].get("timeout") == 5


def test_no_timeout_omits_kwarg_default_path_unchanged(monkeypatch):
    """(A) Default path: when timeout is not passed, create kwargs must NOT
    contain a `timeout` key (proves the default behavior is byte-for-byte
    equivalent — client default timeout is used)."""
    create = _FakeCreate([_Resp()])
    c = _make(monkeypatch, create)
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert out == '{"ok":1}'
    assert "timeout" not in create.calls[0]


def test_per_call_timeout_passed_on_plain_fallback(monkeypatch):
    """(A) The plain-mode fallback create must also carry the per-call timeout."""
    create = _FakeCreate([ValueError("response_format unsupported"), _Resp()])
    c = _make(monkeypatch, create)
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}", timeout=7)
    assert out == '{"ok":1}'
    assert len(create.calls) == 2
    assert create.calls[0].get("timeout") == 7  # json-mode attempt
    assert create.calls[1].get("timeout") == 7  # plain-mode fallback


def test_per_call_max_retries_zero_no_retry(monkeypatch):
    """(B) max_retries=0 -> exactly 1 attempt (no retry) even though the global
    setting allows several; the per-call override wins."""
    monkeypatch.setattr(
        llm_mod, "sleep_or_cancel", lambda _seconds, _cancel_event: None
    )
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "5")  # global allows many
    err = APITimeoutError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err])  # repeats forever
    c = _make(monkeypatch, create)
    with pytest.raises(APITimeoutError):
        c.chat_json([{"role": "user", "content": "hi"}], "{}", max_retries=0)
    assert len(create.calls) == 1  # override forces fail-fast


def test_per_call_max_retries_overrides_global(monkeypatch):
    """(B) max_retries=2 -> 1 + 2 = 3 attempts, independent of the (smaller)
    global setting, confirming the override path is used for the loop bound."""
    monkeypatch.setattr(
        llm_mod, "sleep_or_cancel", lambda _seconds, _cancel_event: None
    )
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "0")  # global says fail-fast
    err = APITimeoutError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err])
    c = _make(monkeypatch, create)
    with pytest.raises(APITimeoutError):
        c.chat_json([{"role": "user", "content": "hi"}], "{}", max_retries=2)
    assert len(create.calls) == 3  # 1 + 2 retries from the override


def test_connection_retry_uses_injected_wait_boundary(monkeypatch):
    waits = []
    monkeypatch.setattr(
        llm_mod,
        "sleep_or_cancel",
        lambda seconds, cancel_event: waits.append((seconds, cancel_event)),
    )
    err = APIConnectionError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err, _Resp()])
    client = _make(monkeypatch, create)

    assert client.chat_json(
        [{"role": "user", "content": "hi"}], "{}"
    ) == '{"ok":1}'
    assert len(waits) == 1
    assert 1 <= waits[0][0] <= 2
    assert waits[0][1] is None


def test_client_built_with_no_sdk_retries(monkeypatch):
    captured = {}

    def fake_openai(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(llm_mod, "OpenAI", fake_openai)
    c = OpenAICompatibleClient(
        Settings(_env_file=None),
        base_url="https://x",
        api_key="k",
        model="m",
    )
    c.client()
    assert captured.get("max_retries") == 0


def test_override_params_win_over_global(monkeypatch):
    """Registry-supplied transport identity drives configured/base_url/model."""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://global")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "gk")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "global-model")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    create = _FakeCreate([_Resp()])
    c = OpenAICompatibleClient(Settings(), base_url="https://reason",
                               api_key="rk", model="reason-model")
    assert c.configured is True
    assert c.base_url == "https://reason" and c.model == "reason-model"
    monkeypatch.setattr(c, "client", lambda: _FakeOpenAI(create))
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert out == '{"ok":1}'
    assert create.calls[0]["model"] == "reason-model"  # 发出的是覆盖后的 model


def test_service_top_p_override_wins_and_is_sent_to_provider(monkeypatch):
    create = _FakeCreate([_Resp()])
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    client = OpenAICompatibleClient(
        Settings(_env_file=None),
        base_url="https://x",
        api_key="k",
        model="fixed-sampling-model",
        top_p_override=0.95,
    )
    monkeypatch.setattr(client, "client", lambda: _FakeOpenAI(create))

    assert client.chat_json(
        [{"role": "user", "content": "hi"}],
        "{}",
        top_p=1.0,
    ) == '{"ok":1}'

    assert create.calls[0]["top_p"] == 0.95


def test_default_params_do_not_fall_back_to_retired_settings(monkeypatch):
    """Raw protocol clients never resolve retired Settings/env endpoints."""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://global")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "gk")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "global-model")
    c = OpenAICompatibleClient(Settings())
    assert c.base_url == ""
    assert c.api_key == ""
    assert c.model == ""
    assert c.configured is False
