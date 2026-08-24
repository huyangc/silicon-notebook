import httpx
import random
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

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


def _usage_dict(response: Any) -> Optional[Dict[str, int]]:
    usage = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
    if usage is None:
        return None
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = (
            usage.get(key)
            if isinstance(usage, dict)
            else getattr(usage, key, None)
        )
        if value is not None:
            out[key] = value
    return out or None

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


def cap_kwargs(client: Any, attr: str) -> Dict[str, Any]:
    """Splat helper for a per-call max_tokens override. Returns
    ``{"max_tokens": N}`` read from the client's Settings budget ``attr`` (e.g.
    "answer_max_tokens"), or ``{}`` when the client exposes no settings — e.g. a
    hand-rolled test double. Callers splat it into ``chat_json`` so answer
    synthesis / KG extraction can request a higher cap than the global default
    without breaking duck-typed clients that don't accept the kwarg. A non-positive
    budget also yields ``{}`` (fall back to chat_json's own default)."""
    settings = getattr(client, "settings", None)
    value = getattr(settings, attr, None) if settings is not None else None
    return {"max_tokens": value} if isinstance(value, int) and value > 0 else {}


class OpenAICompatibleClient:
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

    def _stream_chat_content(
        self,
        kwargs: Dict[str, Any],
        req_kwargs: Dict[str, Any],
        *,
        json_mode: bool,
        cancel_event: CancelEvent,
    ) -> tuple[str, Optional[str], Optional[Dict[str, int]]]:
        """Return ``(content, finish_reason, usage)``.

        finish_reason rides along because the caller needs it to decide whether
        the reply is cacheable: a completion cut off by the token budget
        ("length") must never be frozen into the cache. It arrives on the final
        chunk, so it has to be captured here — by the time the joined string
        gets back to chat_json the stream is already closed. OpenAI-compatible
        usage has the same lifetime and normally arrives on a final chunk whose
        ``choices`` is empty, so it must be captured before that chunk is skipped.
        """
        raise_if_cancelled(cancel_event)
        call_kwargs: Dict[str, Any] = {**kwargs, **req_kwargs, "stream": True}
        if json_mode:
            call_kwargs["response_format"] = {"type": "json_object"}
        request_usage = self._stream_usage_options_supported is not False
        if request_usage:
            call_kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self.client().chat.completions.create(**call_kwargs)
        except Exception as exc:
            if not request_usage or not _is_stream_usage_rejection(exc):
                raise
            self._stream_usage_options_supported = False
            call_kwargs.pop("stream_options", None)
            stream = self.client().chat.completions.create(**call_kwargs)
        else:
            if request_usage:
                self._stream_usage_options_supported = True
        parts: List[str] = []
        finish_reason: Optional[str] = None
        usage: Optional[Dict[str, int]] = None
        try:
            for chunk in stream:
                raise_if_cancelled(cancel_event)
                chunk_usage = _usage_dict(chunk)
                if chunk_usage:
                    usage = chunk_usage
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
        raise_if_cancelled(cancel_event)
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
    ) -> str:
        if not self.configured:
            raise RuntimeError("OpenAI-compatible LLM settings are not configured")
        raise_if_cancelled(cancel_event)
        full_messages = [
            {
                "role": "system",
                "content": (
                    "You are the extraction and reasoning engine for "
                    "silicon-notebook. Return valid JSON only, no markdown fences. "
                    f"Schema hint: {response_schema_hint}"
                ),
            },
            *messages,
        ]
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
            },
        }
        start = time.perf_counter()
        # Per-call overrides (interactive reasoning uses a shorter timeout / fewer
        # retries than the batch-extraction global defaults). When not supplied,
        # behavior is byte-for-byte identical to before: attempts uses the global
        # setting and no `timeout` is passed to .create() (client default applies).
        req_kwargs: Dict[str, Any] = {}
        if timeout is not None:
            req_kwargs["timeout"] = timeout
        try:
            attempts = 1 + (
                max_retries if max_retries is not None
                else self.max_retries
            )
            response = None
            streamed: Optional[
                tuple[str, Optional[str], Optional[Dict[str, int]]]
            ] = None
            for attempt in range(attempts):
                raise_if_cancelled(cancel_event)
                try:
                    # Prefer native JSON mode; fall back if the server rejects
                    # the param.
                    try:
                        if cancel_event is not None:
                            streamed = self._stream_chat_content(
                                kwargs, req_kwargs, json_mode=True, cancel_event=cancel_event)
                        else:
                            response = self.client().chat.completions.create(
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
                                kwargs, req_kwargs, json_mode=False, cancel_event=cancel_event)
                        else:
                            response = self.client().chat.completions.create(**kwargs, **req_kwargs)
                    break
                except AskCancelled:
                    raise
                except Exception as exc:
                    if (
                        not is_transient_llm_error(exc)
                        or attempt + 1 >= attempts
                    ):
                        # Exhausted: propagate so the outer handler logs an error.
                        raise
                    # Visible retry record so blips show up in llm.jsonl.
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
            record["latency_ms"] = round((time.perf_counter() - start) * 1000)
            if usage:
                record["usage"] = usage
            record["response"] = {"content": logger.clip(content)}
            logger.log(record)
            return content
        except AskCancelled:
            record["status"] = "cancelled"
            record["latency_ms"] = round((time.perf_counter() - start) * 1000)
            logger.log(record)
            raise
        except Exception as exc:
            record["status"] = "error"
            record["latency_ms"] = round((time.perf_counter() - start) * 1000)
            record["error"] = f"{type(exc).__name__}: {exc}"
            logger.log(record)
            raise

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
