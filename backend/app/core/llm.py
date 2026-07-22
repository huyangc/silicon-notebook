import httpx
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.core.config import Settings
from app.core.llm_cache import CacheBackend, cache_key
from app.core.llm_logging import LLMInteractionLogger, new_interaction_id
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled, sleep_or_cancel


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


def _usage_dict(response: Any) -> Optional[Dict[str, int]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
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
        self._client: Optional[OpenAI] = None
        self.interaction_logger = LLMInteractionLogger(settings)
        self._cache = None
        if cache is not None:
            self._cache = cache

    def _get_cache(self):
        if self._cache is not None:
            return self._cache
        if not getattr(self.settings, "llm_cache_enabled", False):
            return None
        from pathlib import Path
        from app.core.llm_cache import LLMCache
        path = self.settings.llm_cache_path
        p = Path(path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[3] / path   # anchor to repo root
        self._cache = LLMCache(str(p))
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
    ) -> str:
        raise_if_cancelled(cancel_event)
        call_kwargs: Dict[str, Any] = {**kwargs, **req_kwargs, "stream": True}
        if json_mode:
            call_kwargs["response_format"] = {"type": "json_object"}
        stream = self.client().chat.completions.create(**call_kwargs)
        parts: List[str] = []
        try:
            for chunk in stream:
                raise_if_cancelled(cancel_event)
                if not getattr(chunk, "choices", None):
                    continue
                delta = getattr(chunk.choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    parts.append(content)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        raise_if_cancelled(cancel_event)
        return "".join(parts)

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
        # Best-effort cache lookup: a cache fault must never break the call.
        cache = None
        ckey = ""
        if not bypass_cache:
            try:
                cache = self._get_cache()
                if cache is not None:
                    ckey = cache_key(model, full_messages, response_schema_hint)
                    cached = cache.get(ckey)
                    if cached is not None:
                        return cached
            except Exception:
                cache, ckey = None, ""
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": full_messages,
            "temperature": temperature,
            "top_p": top_p,
        }
        # Cap the completion length. A per-call max_tokens (answer synthesis / KG
        # extraction pass a higher budget) overrides the global default; when both
        # resolve to 0/None the param is omitted so the server default applies.
        # Set here on the shared kwargs dict so it flows into all three
        # create() calls (streaming + the two non-stream paths) uniformly.
        _mt = max_tokens if max_tokens is not None else self.settings.openai_compat_max_tokens
        if _mt and _mt > 0:
            kwargs["max_tokens"] = _mt
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
            streamed_content: Optional[str] = None
            for attempt in range(attempts):
                raise_if_cancelled(cancel_event)
                try:
                    # Prefer native JSON mode; fall back if the server rejects
                    # the param.
                    try:
                        if cancel_event is not None:
                            streamed_content = self._stream_chat_content(
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
                            streamed_content = self._stream_chat_content(
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
            if streamed_content is not None:
                content = strip_json_fences(streamed_content)
            else:
                content = strip_json_fences(response.choices[0].message.content or "")
            # Best-effort write; never cache the empty "{}" fallback so a transient
            # empty/garbage response isn't frozen in for this prompt.
            if cache and ckey and content != "{}":
                try:
                    cache.put(ckey, content)
                except Exception:
                    pass
            record["status"] = "ok"
            record["latency_ms"] = round((time.perf_counter() - start) * 1000)
            usage = _usage_dict(response)
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
