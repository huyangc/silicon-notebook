import httpx
import random
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.core.config import Settings
from app.core.cache import CacheBackend, is_cacheable_llm_response, llm_key
from app.core.llm_logging import LLMInteractionLogger, new_interaction_id
from app.domain.cancellation import AskCancelled, CancelEvent, raise_if_cancelled, sleep_or_cancel


def llm_status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    return int(value) if isinstance(value, int) else None


def is_transient_llm_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    status = llm_status_code(exc)
    return status == 429 or (status is not None and 500 <= status <= 599)


def is_response_format_rejection(exc: Exception) -> bool:
    status = llm_status_code(exc)
    text = str(exc).lower()
    return status in (400, 422) and (
        "response_format" in text
        or "json_object" in text
        or "json mode" in text
    )


def _is_non_http_response_format_rejection(exc: Exception) -> bool:
    if not isinstance(exc, ValueError):
        return False
    text = str(exc).lower()
    return (
        "response_format" in text
        or "json_object" in text
        or "json mode" in text
    )


def _is_stream_usage_rejection(exc: Exception) -> bool:
    """Whether an OpenAI-compatible server explicitly rejects usage streaming.

    ``stream_options.include_usage`` is additive protocol metadata.  Thin or
    older compatible servers may reject that parameter even though ordinary
    streaming works, so only a parameter-specific 400/422 (or an equivalent
    local ``ValueError``) is safe to remember as unsupported.
    """
    status = llm_status_code(exc)
    text = str(exc).lower()
    names_usage_option = (
        "stream_options" in text
        or "stream options" in text
        or "include_usage" in text
    )
    return names_usage_option and (
        status in (400, 422) or isinstance(exc, ValueError)
    )


def _usage_field(source: Any, key: str) -> Any:
    """Read ``key`` off a usage payload that may be a mapping OR an SDK object."""
    return (
        source.get(key)
        if isinstance(source, dict)
        else getattr(source, key, None)
    )


#: Nested usage counters worth flattening: ``(container field, leaf field)``.
#: ``cached_tokens`` is the provider's own name for the share of the prompt it
#: served from ITS prefix cache, and ``reasoning_tokens`` the billed-but-hidden
#: thinking. Both live one level down in the OpenAI-compatible schema and are
#: optional: plenty of compatible servers never send the details object at all.
_USAGE_DETAIL_FIELDS = (
    ("prompt_tokens_details", "cached_tokens"),
    ("completion_tokens_details", "reasoning_tokens"),
)


def _usage_dict(response: Any) -> Optional[Dict[str, int]]:
    """Flatten a provider usage payload into plain counters, or None.

    A missing counter is an ABSENT KEY, never a zero (design §8.2): a provider
    that does not report ``cached_tokens`` is telling us nothing, while a 0 would
    read downstream as "measured, and it was none" — the single most misleading
    value this dict can carry, because the whole prefix-reuse experiment is a
    comparison of those numbers. A provider that genuinely reports 0 still gets
    its 0 through: this only refuses to INVENT one.
    """
    usage = _usage_field(response, "usage")
    if usage is None:
        return None
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _usage_field(usage, key)
        if value is not None:
            out[key] = value
    for container_key, leaf_key in _USAGE_DETAIL_FIELDS:
        container = _usage_field(usage, container_key)
        if container is None:
            continue
        value = _usage_field(container, leaf_key)
        if value is not None:
            out[leaf_key] = value
    return out or None


class _StreamingAskCancelled(AskCancelled):
    """Cancellation raised after a stream already supplied billed usage."""

    def __init__(self, usage: Dict[str, int]):
        super().__init__()
        self.usage = usage


def _raise_if_cancelled_with_usage(
    cancel_event: CancelEvent,
    usage: Optional[Dict[str, int]],
) -> None:
    try:
        raise_if_cancelled(cancel_event)
    except AskCancelled:
        if usage:
            raise _StreamingAskCancelled(usage) from None
        raise

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
# Reasoning models (e.g. MiniMax-M2.7) emit a chain-of-thought block before the
# JSON, inline in `content`, even under response_format=json_object.
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def strip_json_fences(text: str) -> str:
    """Normalize a model's JSON reply so json.loads won't choke:
    strip <think>...</think> chain-of-thought, drop ```json ... ``` fences, and
    fall back to the outermost {...} if anything still leads the JSON."""
    cleaned = (text or "").strip()
    cleaned = _THINK_RE.sub("", cleaned).strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    # Trim anything outside the outermost JSON object (leading prose, trailing
    # remarks). Reply schemas here are all objects, so locking onto {...} is safe.
    i, j = cleaned.find("{"), cleaned.rfind("}")
    if i != -1 and j > i:
        cleaned = cleaned[i:j + 1]
    return cleaned


def _response_validator_allows(
    validator: Optional[Callable[[str], bool]], content: str
) -> bool:
    """Shape check for a caller-supplied response_validator (content, not
    participation).

    Under opt-in caching the OPT-IN decision is made by the caller (chat_json's
    `response_validator is not None` gates), so in normal flow this helper is
    only ever invoked with a non-None validator. The None -> True branch is kept
    deliberately: it is a defensive no-op AND the pivot a per-gate mutation flips
    (drop a gate's `is not None` and a None validator would leak through here,
    turning the corresponding guard test red). A supplied validator judges the
    reply's shape; a validator that raises is treated as "do not cache" — a cache
    fault (or a buggy validator) must NEVER break the LLM call, and when in doubt
    about a reply's usability the safe move is to skip the write, not freeze a
    possibly bad value for the whole TTL."""
    if validator is None:
        return True
    try:
        return bool(validator(content))
    except Exception:
        return False


def budget_kwargs(
    settings: Any, attr: str, *, multiplier: int = 1
) -> Dict[str, Any]:
    """Splat helper for a per-call max_tokens override read straight off a
    Settings-like object. Returns ``{"max_tokens": N * multiplier}`` for a
    positive integer budget ``attr``, else ``{}`` (fall back to chat_json's own
    default).

    Split out of ``cap_kwargs`` because a caller does not always reach its budget
    through a client: the reasoning retriever owns the ``Settings`` itself and its
    reflect client may be an offline stub carrying none. ``multiplier`` serves the
    one retry that asks for a bigger budget after a completion the provider cut
    off at max_tokens — a factor on the SAME configured number, so a deployment
    that raises the budget raises the retry with it instead of the retry drifting
    onto a second hard-coded ceiling."""
    value = getattr(settings, attr, None) if settings is not None else None
    return ({"max_tokens": value * multiplier}
            if isinstance(value, int) and value > 0 else {})


def cap_kwargs(client: Any, attr: str) -> Dict[str, Any]:
    """Splat helper for a per-call max_tokens override. Returns
    ``{"max_tokens": N}`` read from the client's Settings budget ``attr`` (e.g.
    "answer_max_tokens"), or ``{}`` when the client exposes no settings — e.g. a
    hand-rolled test double. Callers splat it into ``chat_json`` so answer
    synthesis / KG extraction can request a higher cap than the global default
    without breaking duck-typed clients that don't accept the kwarg. A non-positive
    budget also yields ``{}`` (fall back to chat_json's own default)."""
    return budget_kwargs(getattr(client, "settings", None), attr)


#: The wrapper system message this client puts in front of every caller's
#: messages. Spelled once, here, because two places must agree on it byte for
#: byte: what actually goes on the wire, and what any measurement of the
#: provider-facing message sequence reconstructs.
_PROVIDER_WRAPPER_PREFIX = (
    "You are the extraction and reasoning engine for "
    "silicon-notebook. Return valid JSON only, no markdown fences. "
    "Schema hint: "
)


def provider_messages(
    messages: List[Dict[str, str]], response_schema_hint: str
) -> List[Dict[str, str]]:
    """The exact message list this client sends to the provider: the wrapper
    system message (role brief + schema hint) followed by the caller's own
    messages.

    Pulled out of ``chat_json`` as a module-level PURE function — no I/O, no
    client state, no ``self`` — so a measurement layer can reconstruct what a
    call would put on the wire without issuing it, and, more importantly, so
    there is exactly ONE assembly: ``chat_json`` calls this rather than keeping
    a second copy in sync with it. A duplicated wrapper would drift silently and
    every prefix/cache-key number computed off the copy would be measuring a
    message sequence that was never sent.

    Returns a fresh list; the caller's own message mappings are passed through by
    reference (this function never mutates them).
    """
    return [
        {
            "role": "system",
            "content": f"{_PROVIDER_WRAPPER_PREFIX}{response_schema_hint}",
        },
        *messages,
    ]


def serialize_provider_messages(messages: List[Dict[str, str]]) -> bytes:
    """Deterministic UTF-8 serialization of a provider-facing message list,
    for byte-level structural comparison (e.g. how much of turn N's request is a
    literal prefix of turn N+1's).

    Framing is length-SUFFIXED — each field goes out as ``<bytes>:<byte
    length>:`` — rather than delimiter-separated, because the payload is
    untrusted model input: any separator drawn from the text alphabet can be
    written INTO a message body, and a body that forges a boundary would move the
    apparent field split and silently corrupt the numbers computed on top of
    this. A decimal length cannot be forged from inside the bytes it counts,
    because it is only ever read from a position fixed by the parse, not searched
    for in the text.

    The length trails its field rather than leading it, and that ordering is the
    whole point of this shape. The measured question is "how much of turn N's
    request is a literal prefix of turn N+1's", and under the layout this client
    actually sends (one system message plus ONE long user message whose tail is
    the only part that changes per turn) a LEADING length would put a header that depends
    on the tail in front of the thousands of identical bytes it counts: change
    the tail's length by one byte and the common prefix collapses at the header,
    reporting ~0 shared bytes for two requests that share nearly all of them.
    With the length behind its field, divergence can only land where the bytes
    themselves first differ.

    Injectivity is preserved (nothing is lost by moving the length): the byte
    string is uniquely decodable RIGHT to LEFT. The stream ends with ``:``;
    scanning backwards over the decimal digits before it yields ``n``, the ``:``
    before those digits closes the field, and the ``n`` bytes before that ARE the
    field — then the same step repeats for the field to its left. Every
    structural byte is located by counting from an end, never by searching the
    payload, so no content can move a boundary.

    Only ``role`` and ``content`` participate, in that order, in list order; a
    missing field serializes as empty. The result is a plain concatenation of
    per-message frames, so the serialization of any leading slice of ``messages``
    is a literal byte prefix of the whole — which is what makes a common-prefix
    length meaningful. Per message the framing costs a fixed
    ``len(role) + len(str(len(role))) + len(str(len(content))) + 4`` bytes on top
    of the payload; all of it precedes ``content`` except that content's own
    length and the two colons around it, which trail it. This is a client-side
    structural metric only: it is NOT a provider cache key and says nothing about
    what the provider actually reused.

    A non-mapping element raises rather than serializing as empty: for a
    measurement function, silently emitting plausible-looking bytes for input it
    did not understand is worse than failing.
    """
    out = bytearray()
    for message in messages:
        for field in ("role", "content"):
            raw = message.get(field, "")
            blob = ("" if raw is None else str(raw)).encode("utf-8")
            out += blob
            out += b":"
            out += str(len(blob)).encode("ascii")
            out += b":"
    return bytes(out)


#: Name of the optional OUT-parameter a caller may pass to ``chat_json`` to get
#: this call's provider-side outcome back. The value is a caller-owned mutable
#: mapping which ``chat_json`` fills in before returning or raising; see
#: ``_record_call_stats`` for the exact keys (``status``, ``call_wall_ms``,
#: ``attempts``, ``attempts_observed`` on every exit that reaches the sink;
#: ``finish_reason``, ``response_chars`` and ``usage`` when they exist).
#:
#: ``status`` is one of ``ok`` / ``cancelled`` / ``error`` / ``cache_hit``. An
#: EMPTY sink after a call is itself information, and has exactly two causes:
#: either the client did not declare ``supports_call_stats`` (a duck-typed double
#: or a plugin-bound transport — the caller must then treat every field as
#: unknown, NOT as zero), or ``chat_json`` refused the call before doing any work
#: at all (unconfigured settings, or a cancellation already pending on entry) and
#: there is genuinely nothing to report. Both raise or return before any
#: measurement exists; neither is a dropped observation.
#:
#: It exists because ``chat_json`` returns a plain ``str``: an empty completion
#: and a completion the server cut off at ``max_tokens`` arrive as the SAME empty
#: string, and the only thing that tells them apart — ``finish_reason`` — dies
#: inside this function. Downstream that difference decides the remedy (retry
#: with a larger budget vs. treat the model as having produced nothing), so a
#: caller that wants to act on it needs the value, not just a log line. The same
#: argument extends to the measurement fields: duration, real request count and
#: response size are all knowable ONLY here, and a caller that must report per
#: call (an offline experiment rig) cannot recover them from a log file it does
#: not own — while a caller that passes nothing pays for none of it.
#:
#: Spelled as a constant so the reflect layer's splat helper and this signature
#: cannot drift. Callers that do not pass it are byte-for-byte unaffected.
CALL_STATS_KWARG = "call_stats"


class _RequestCount:
    """Real provider requests issued for ONE ``chat_json`` call.

    A counter object rather than a local int because the request sites live in
    two functions: the retry loop in ``chat_json`` and the two ``create()`` calls
    inside ``_stream_chat_content`` (the second one being the ``stream_options``
    rebuild, which today leaves no trace anywhere). ``chat_json`` owns the
    instance and hands it down, so ``attempts`` counts what actually went on the
    wire instead of the number of loop iterations — those differ by exactly the
    silent fallbacks, which is the difference the experiment is trying to see.
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def record(self) -> None:
        self.value += 1


def _record_call_stats(
    sink: Optional[Dict[str, Any]],
    *,
    status: str,
    wall_ms: int,
    attempts: int,
    usage: Optional[Dict[str, int]] = None,
    finish_reason: Optional[str] = None,
    response_chars: Optional[int] = None,
) -> None:
    """Fill a caller's ``call_stats`` out-parameter (see ``CALL_STATS_KWARG``).

    ``sink is None`` — the overwhelmingly common case — returns immediately, so a
    caller that never asked for stats is byte-for-byte unaffected by everything
    below.

    ``status`` / ``call_wall_ms`` / ``attempts`` / ``attempts_observed`` are
    written by every exit that gets here — the four being success, cancellation,
    error and a response-cache hit. Cancellation and error are the ones worth
    naming: a call that died is exactly the one whose duration and request count a
    run-level report must not silently drop (it still consumed wall clock and
    still hit the endpoint). The two exits that never reach this function are the
    pre-flight refusals at the top of ``chat_json`` (unconfigured settings, and a
    cancellation already pending on entry): both raise before any work, so there
    is no duration, no request and nothing to report — see ``CALL_STATS_KWARG``
    for how a consumer reads an empty sink.

    Everything else is written only when it exists: an absent key means "no
    observation", never zero. ``finish_reason`` is the standing exception — the
    ok exit passes ``finish_reason or ""``, so an empty STRING there means "the
    provider did not say", mirroring what llm.jsonl has always recorded. Absent
    and empty therefore differ for that one key: absent means the call never
    produced a completion at all (it was cancelled or it failed).

    ``attempts_observed`` is True because this client counts requests at the
    transport itself. It is not decoration: a duck-typed client that does not
    declare ``supports_call_stats`` leaves the sink empty, and a consumer must
    then treat the request count as unknown rather than as the logical call
    count — the two differ precisely when a silent fallback fired.
    """
    if sink is None:
        return
    sink["status"] = status
    sink["call_wall_ms"] = wall_ms
    sink["attempts"] = attempts
    sink["attempts_observed"] = True
    if finish_reason is not None:
        sink["finish_reason"] = finish_reason
    if response_chars is not None:
        sink["response_chars"] = response_chars
    if usage:
        sink["usage"] = usage


class OpenAICompatibleClient:
    #: This client honours the ``call_stats`` out-parameter (see
    #: ``CALL_STATS_KWARG``). Declared as a class attribute rather than left to
    #: signature reflection: a duck-typed double that spells ``**kwargs`` would
    #: pass a signature probe while silently never filling the sink, and nothing
    #: downstream would fail — it would just quietly stop distinguishing a
    #: budget-truncated reply from an empty one.
    supports_call_stats = True

    def __init__(self, settings: Settings, *, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, model: Optional[str] = None,
                 max_retries: Optional[int] = None,
                 max_connections: Optional[int] = None,
                 top_p_override: Optional[float] = None,
                 cache: Optional[CacheBackend] = None):
        self.settings = settings
        # Transport identity is always supplied by the system service registry.
        self.base_url = (base_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.max_retries = (max_retries if max_retries is not None
                            else settings.openai_compat_max_retries)
        self.max_connections = max(
            1,
            int(max_connections)
            if max_connections is not None
            else 1,
        )
        self.top_p_override = top_p_override
        self._client: Optional[OpenAI] = None
        # None = not probed yet; False is learned only from an explicit
        # parameter rejection. Keeping the result on the physical client avoids
        # doubling every later KG window for providers that support streaming
        # content but not OpenAI's optional usage trailer.
        self._stream_usage_options_supported: Optional[bool] = None
        self.interaction_logger = LLMInteractionLogger(settings)
        self._cache = None
        if cache is not None:
            self._cache = cache

    def _get_cache(self):
        if self._cache is None:
            from app.core.cache import make_cache_backend
            self._cache = make_cache_backend(self.settings)
        return self._cache

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def client(self) -> OpenAI:
        if not (self.base_url and self.api_key):
            raise RuntimeError("OpenAI-compatible API settings are not configured")
        if self._client is None:
            timeout = self.settings.openai_compat_timeout_seconds
            http_client = httpx.Client(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_connections,
                ),
            )
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=timeout,
                # Don't let the SDK silently retry connection errors 2x: a stalled
                # connection would otherwise block ~3x the timeout per call. We
                # fail fast and let the caller (per-window extraction) drop it.
                max_retries=0,
                http_client=http_client,
            )
        return self._client

    def _create(self, requests: _RequestCount, **call_kwargs: Any) -> Any:
        """Issue ONE provider request and count it.

        The single choke point for every ``chat.completions.create`` in this
        class, so a request cannot be added without being counted. The client is
        resolved BEFORE the tally: a configuration error raised by ``client()``
        means nothing was sent, and counting it would inflate the request count
        with a call that never left the process.
        """
        create = self.client().chat.completions.create
        requests.record()
        return create(**call_kwargs)

    def _stream_chat_content(
        self,
        kwargs: Dict[str, Any],
        req_kwargs: Dict[str, Any],
        *,
        json_mode: bool,
        cancel_event: CancelEvent,
        requests: _RequestCount,
    ) -> tuple[str, Optional[str], Optional[Dict[str, int]]]:
        """Return ``(content, finish_reason, usage)``.

        finish_reason rides along because the caller needs it to decide whether
        the reply is cacheable: a completion cut off by the token budget
        ("length") must never be frozen into the cache. It arrives on the final
        chunk, so it has to be captured here — by the time the joined string
        gets back to chat_json the stream is already closed. OpenAI-compatible
        usage has the same lifetime and normally arrives on a final chunk whose
        ``choices`` is empty, so it must be captured before that chunk is skipped.

        ``requests`` is ``chat_json``'s per-call request tally, handed down
        because the ``stream_options`` rebuild below is a second real request
        that leaves no log record of its own; it is a required argument so a new
        request site here cannot forget to be counted.
        """
        raise_if_cancelled(cancel_event)
        call_kwargs: Dict[str, Any] = {**kwargs, **req_kwargs, "stream": True}
        if json_mode:
            call_kwargs["response_format"] = {"type": "json_object"}
        request_usage = self._stream_usage_options_supported is not False
        if request_usage:
            call_kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self._create(requests, **call_kwargs)
        except Exception as exc:
            if not request_usage or not _is_stream_usage_rejection(exc):
                raise
            self._stream_usage_options_supported = False
            call_kwargs.pop("stream_options", None)
            raise_if_cancelled(cancel_event)
            stream = self._create(requests, **call_kwargs)
        parts: List[str] = []
        finish_reason: Optional[str] = None
        usage: Optional[Dict[str, int]] = None
        try:
            for chunk in stream:
                chunk_usage = _usage_dict(chunk)
                if chunk_usage:
                    usage = chunk_usage
                # The iterator has already delivered this chunk. Preserve any
                # exact billed usage it contains before honoring a cancellation
                # that raced with the final usage-only trailer.
                _raise_if_cancelled_with_usage(cancel_event, usage)
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish_reason = reason
                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    parts.append(content)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        _raise_if_cancelled_with_usage(cancel_event, usage)
        return "".join(parts), finish_reason, usage

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        response_schema_hint: str,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        # DeepSeek-V4 官方推荐本地部署采样参数: temperature=1.0, top_p=1.0
        temperature: float = 1.0,
        top_p: float = 1.0,
        max_tokens: Optional[int] = None,
        cancel_event: CancelEvent = None,
        bypass_cache: bool = False,
        response_validator: Optional[Callable[[str], bool]] = None,
        thinking_mode: Optional[
            Literal["enabled", "disabled"]
        ] = None,
        call_stats: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not self.configured:
            raise RuntimeError("OpenAI-compatible LLM settings are not configured")
        raise_if_cancelled(cancel_event)
        # Single assembly point (see provider_messages): what goes on the wire,
        # what feeds the cache key, and what a measurement layer reconstructs are
        # the same list, produced by the same pure function.
        full_messages = provider_messages(messages, response_schema_hint)
        model = self.model
        # Some OpenAI-compatible models accept only one provider-defined
        # nucleus-sampling value (for example top_p=0.95).  A physical-service
        # override is authoritative over per-workload call defaults so every
        # request routed to that service satisfies the same protocol contract.
        effective_top_p = (
            self.top_p_override
            if self.top_p_override is not None
            else top_p
        )
        # Resolve the effective completion budget up front — it feeds BOTH the
        # cache key and the request kwargs, and the two must agree. max_tokens=None
        # falls back to the global default; a non-positive result means "omit the
        # param, let the server default apply". Using this *resolved* value (not
        # the raw arg) in the key keeps max_tokens=None and an explicit
        # equal-to-default value on ONE key (no false miss), while a larger budget
        # — the documented truncation remedy — correctly lands on a different key.
        _mt = max_tokens if max_tokens is not None else self.settings.openai_compat_max_tokens
        effective_max_tokens = _mt if isinstance(_mt, int) and _mt > 0 else None
        # Started BEFORE the cache lookup, not after it, because the lookup is on
        # the clock too: the whole point of measuring a served-from-cache call is
        # to compare its cost against a real one, and a timer that starts after
        # the store has already answered would report the one exit that costs
        # almost nothing as costing exactly nothing. Every later exit reads the
        # same `start`, so the cache probe is inside all of their durations as
        # well — which is correct: the caller waited for it either way.
        start = time.perf_counter()
        # Best-effort cache lookup: a cache fault must never break the call.
        # The response cache is OPT-IN (Codex round 6 / user decision): a caller
        # participates ONLY by supplying a response_validator. cache/ckey are
        # resolved for every non-bypass call so the hit gate and the write gate
        # below stay INDEPENDENTLY exercisable, but a validator-less caller is
        # neither served a hit (the hit gate) nor written (the write gate) — its
        # call simply runs uncached (correctness preserved, only the perf win
        # forgone). This closes an entire poisoning class at once: a validator-
        # less caller (Ask, paper-meta, summaries, schema induction, query
        # rewrite, …) could previously freeze a degenerate []/error reply for the
        # whole TTL and replay it on every retry/reparse. The 93%-cost path (KG
        # extraction) passes validators and keeps caching. See the design doc's
        # safety-valve §1.
        cache = None
        ckey = ""
        if not bypass_cache:
            try:
                cache = self._get_cache()
                # base_url is the service identity: two users can configure the
                # same model name against different endpoints, and this cache is
                # global/cross-user — keying on model alone would hand the second
                # user the first endpoint's response. api_key is deliberately NOT
                # in the key (it leaks via stats()/logs; evict_tag covers a
                # same-endpoint key rotation). Generation params (temperature /
                # top_p / the resolved max_tokens) are in the key too: a different
                # sampling setting or token budget is a semantically different
                # request and must never reuse another call's response.
                ckey = llm_key(
                    model, full_messages, response_schema_hint, self.base_url,
                    temperature=temperature, top_p=effective_top_p,
                    max_tokens=effective_max_tokens,
                    thinking_mode=thinking_mode,
                )
                # Opt-in HIT gate: only a validator-bearing caller may be served a
                # cached reply, and only if the cached value still satisfies THAT
                # validator. The validator is NOT part of the key, so a value
                # written by some other validator-bearing caller at the same key
                # is re-judged here; a reject is treated as a MISS and falls
                # through to the real call, whose fresh response the write gate
                # re-judges. _response_validator_allows keeps a raising validator
                # conservative (treated as reject) so a cache/validator fault
                # never breaks the call (still inside the bypass_cache try).
                if response_validator is not None:
                    cached = cache.get(ckey)
                    if cached is not None and _response_validator_allows(
                        response_validator, cached
                    ):
                        # The fourth exit, and the only one that reaches no
                        # provider at all. It writes no llm.jsonl row (it never
                        # did — there was no interaction to log), but a caller
                        # holding a sink must still be able to tell "served from
                        # the local store" apart from "never called", which an
                        # empty sink cannot express. `attempts=0` is a MEASURED
                        # zero, not an absent one: this exit is exactly the case
                        # where the true request count is known to be none.
                        #
                        # `status="cache_hit"` names THIS application-level
                        # response cache — a reply this process stored earlier
                        # under `llm_key`. It says nothing whatsoever about the
                        # provider's own prefix cache, whose reuse is reported
                        # only by `usage.cached_tokens` on the real exits; the two
                        # must never be read as the same measurement.
                        _record_call_stats(
                            call_stats,
                            status="cache_hit",
                            wall_ms=round((time.perf_counter() - start) * 1000),
                            attempts=0,
                        )
                        return cached
            except Exception:
                cache, ckey = None, ""
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": full_messages,
            "temperature": temperature,
            "top_p": effective_top_p,
        }
        # Cap the completion length with the resolved budget (see above). A
        # per-call max_tokens (answer synthesis / KG extraction pass a higher
        # budget) overrides the global default; when it resolves to None the param
        # is omitted so the server default applies. Set here on the shared kwargs
        # dict so it flows into all three create() calls (streaming + the two
        # non-stream paths) uniformly.
        if effective_max_tokens is not None:
            kwargs["max_tokens"] = effective_max_tokens
        if thinking_mode is not None:
            # The deployment-owned workload policy is authoritative.  The
            # OpenAI SDK carries the configured thinking control through the
            # endpoint's compatible ``extra_body`` request extension.
            kwargs["extra_body"] = {
                "thinking": {"type": thinking_mode}
            }
        logger = self.interaction_logger
        record: Dict[str, Any] = {
            "ts": datetime.now().isoformat(),
            "id": new_interaction_id(),
            "kind": "chat",
            "model": model,
            "request": {
                "messages": [
                    {"role": m.get("role", ""), "content": logger.clip(m.get("content", ""))}
                    for m in full_messages
                ],
                "schema_hint": logger.clip(response_schema_hint),
                **(
                    {
                        "thinking_mode": thinking_mode
                    }
                    if thinking_mode is not None else {}
                ),
            },
        }
        # Owned here, incremented at the transport (see _create): every exit
        # below reports how many requests this ONE logical call really issued.
        requests = _RequestCount()
        # Per-call overrides (interactive reasoning uses a shorter timeout / fewer
        # retries than the batch-extraction global defaults). When not supplied,
        # behavior is byte-for-byte identical to before: the retry budget uses the
        # global setting and no `timeout` is passed to .create() (client default
        # applies).
        req_kwargs: Dict[str, Any] = {}
        if timeout is not None:
            req_kwargs["timeout"] = timeout
        try:
            # NOT the reported `attempts`. This is the CEILING on transient-error
            # retries — a loop bound, decided before anything is sent — while the
            # reported `attempts` (`requests.value`) is how many requests actually
            # went out, which is larger whenever a silent fallback fires and
            # smaller whenever the loop exits early. Two different quantities that
            # shared a name here until the review; the reader who conflates them
            # gets a request count that is really a configuration constant.
            attempt_budget = 1 + (
                max_retries if max_retries is not None
                else self.max_retries
            )
            response = None
            streamed: Optional[
                tuple[str, Optional[str], Optional[Dict[str, int]]]
            ] = None
            for attempt in range(attempt_budget):
                raise_if_cancelled(cancel_event)
                try:
                    # Prefer native JSON mode; fall back if the server rejects
                    # the param.
                    try:
                        if cancel_event is not None:
                            streamed = self._stream_chat_content(
                                kwargs, req_kwargs, json_mode=True,
                                cancel_event=cancel_event, requests=requests)
                        else:
                            response = self._create(
                                requests,
                                **kwargs, **req_kwargs, response_format={"type": "json_object"}
                            )
                    except AskCancelled:
                        raise
                    except Exception as exc:
                        # Server rejected response_format (param unsupported):
                        # retry once in plain mode. NOT a connection error, so it
                        # never enters the bounded connection-retry loop.
                        if not (
                            is_response_format_rejection(exc)
                            or _is_non_http_response_format_rejection(exc)
                        ):
                            raise
                        if cancel_event is not None:
                            streamed = self._stream_chat_content(
                                kwargs, req_kwargs, json_mode=False,
                                cancel_event=cancel_event, requests=requests)
                        else:
                            response = self._create(requests, **kwargs, **req_kwargs)
                    break
                except AskCancelled:
                    raise
                except Exception as exc:
                    if (
                        not is_transient_llm_error(exc)
                        or attempt + 1 >= attempt_budget
                    ):
                        # Exhausted: propagate so the outer handler logs an error.
                        raise
                    # Visible retry record so blips show up in llm.jsonl. Its
                    # `attempt` is this loop's ZERO-BASED index, a third quantity
                    # again distinct from both the budget and the request count —
                    # kept singular on purpose so it cannot be read as the plural
                    # `attempts` the terminal row carries. A retry row deliberately
                    # has NO `attempts`: the tally is still climbing at that point,
                    # and a per-row count would invite summing rows that all
                    # describe the same logical call.
                    logger.log({
                        **record,
                        "status": "retry",
                        "attempt": attempt,
                        "latency_ms": round((time.perf_counter() - start) * 1000),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    # Jittered exponential backoff (cap 30s): a synchronized burst
                    # of rejected calls must NOT retry in lockstep, or it re-storms
                    # the endpoint and gets mass-rejected again.
                    backoff = min(2 ** attempt, 30)
                    sleep_or_cancel(backoff + random.uniform(0, backoff), cancel_event)
            if streamed is not None:
                streamed_content, finish_reason, usage = streamed
                content = strip_json_fences(streamed_content)
            else:
                choice = response.choices[0]
                # getattr: hand-rolled test doubles and thin OpenAI-compatible
                # servers may omit finish_reason entirely; absent == unknown,
                # and is_cacheable_llm_response then falls back to parseability.
                finish_reason = getattr(choice, "finish_reason", None)
                content = strip_json_fences(choice.message.content or "")
                usage = _usage_dict(response)
            # Recorded either way, and handed back to a caller that asked for it
            # at the exit below (see CALL_STATS_KWARG). Without it an empty
            # completion and one the server cut off at max_tokens are the same
            # empty string everywhere downstream, and llm.jsonl cannot tell a
            # token-budget truncation from a model that returned nothing.
            record["finish_reason"] = finish_reason or ""
            # Best-effort write, and only of a reply that is actually usable —
            # the empty "{}" fallback, unparseable JSON and budget-truncated
            # completions are all excluded (see is_cacheable_llm_response for
            # why each gate exists). Writing junk here freezes it for the whole
            # TTL and, because max_tokens is not part of the key, disarms the
            # documented remedy of re-running with a larger budget.
            # NOTE: `cache is not None`, not truthy `cache` — SqliteCacheBackend
            # defines __len__ for entry-count introspection, so a freshly empty
            # cache (0 entries) is falsy under `bool()`. A plain `if cache` would
            # permanently skip every write on a cold cache: it can never accumulate
            # its first entry, `len()` stays 0 forever, and the cache never turns
            # "truthy". Identity check sidesteps that trap entirely.
            #
            # The caller-supplied response_validator is the OPT-IN SWITCH and the
            # fourth door in one. Opt-in (response_validator is not None): a caller
            # without a validator never writes — we can't vouch for its reply's
            # shape, so freezing it for the TTL is the poisoning risk this closes.
            # For validator-bearing callers the fourth door still applies: policy's
            # is_cacheable_llm_response is schema-agnostic (any parseable non-"{}"
            # non-truncated reply passes), but a syntactically valid reply can
            # still violate the CALLER's schema — e.g. KG extraction receiving
            # {"nodes":"invalid"} (nodes must be a list). That parses, produces 0
            # grounded objects, and — cached 90d — freezes that 0 while every
            # re-parse keeps hitting it. The validator runs here (see
            # _response_validator_allows: a validator fault conservatively skips
            # the write rather than crashing the call). `response_validator is not
            # None` is load-bearing, not redundant: cache/ckey are resolved for
            # every non-bypass call (see the hit gate above), so this clause is
            # what actually keeps validator-less replies out of the store.
            if (
                cache is not None
                and ckey
                and response_validator is not None
                and is_cacheable_llm_response(content, finish_reason)
                and _response_validator_allows(response_validator, content)
            ):
                try:
                    # tag=model: evict_tag(model) is how a model-service swap
                    # drops exactly this model's entries. An untagged write
                    # (tag='') can never be evicted that way.
                    cache.put(ckey, content, tag=model)
                except Exception:
                    pass
            record["status"] = "ok"
            # ONE perf_counter difference per exit, shared by the log record and
            # the out-parameter: two readings of the same call must never
            # disagree, and this is already the monotonic clock the log used.
            wall_ms = round((time.perf_counter() - start) * 1000)
            record["latency_ms"] = wall_ms
            record["attempts"] = requests.value
            # Length of the content the caller actually receives, NOT of the
            # clipped copy below: llm.jsonl truncates its body at
            # `llm_log_max_chars`, so a reply read back out of the log understates
            # every long completion. Numbers only — the body itself never leaves
            # the clip.
            record["response_chars"] = len(content)
            if usage:
                record["usage"] = usage
            record["response"] = {"content": logger.clip(content)}
            _record_call_stats(
                call_stats,
                status="ok",
                wall_ms=wall_ms,
                attempts=requests.value,
                usage=usage,
                finish_reason=finish_reason or "",
                response_chars=len(content),
            )
            logger.log(record)
            return content
        except AskCancelled as exc:
            record["status"] = "cancelled"
            wall_ms = round((time.perf_counter() - start) * 1000)
            record["latency_ms"] = wall_ms
            record["attempts"] = requests.value
            # A stream that already delivered its billed usage trailer before the
            # cancellation landed carries it on the exception; that spend is real
            # and must be reported even though no content comes back.
            #
            # Read straight off the exception, NOT through _usage_dict: what
            # _StreamingAskCancelled carries is the ALREADY-FLATTENED dict this
            # module produced from the trailer. Re-flattening it looks harmless
            # and silently drops `cached_tokens`/`reasoning_tokens` — the nested
            # containers those were lifted out of no longer exist by then, so the
            # second pass finds nothing and writes nothing. Those two counters are
            # the entire point of the prefix-reuse measurement, and a cancelled
            # call is a normal outcome for it (the user hit Stop), so the loss
            # would be routine and invisible. A plain AskCancelled has no `usage`
            # at all and yields None.
            usage = getattr(exc, "usage", None)
            if usage:
                record["usage"] = usage
            _record_call_stats(
                call_stats,
                status="cancelled",
                wall_ms=wall_ms,
                attempts=requests.value,
                usage=usage,
            )
            logger.log(record)
            raise
        except Exception as exc:
            record["status"] = "error"
            wall_ms = round((time.perf_counter() - start) * 1000)
            record["latency_ms"] = wall_ms
            record["attempts"] = requests.value
            record["error"] = f"{type(exc).__name__}: {exc}"
            _record_call_stats(
                call_stats,
                status="error",
                wall_ms=wall_ms,
                attempts=requests.value,
            )
            logger.log(record)
            raise

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
