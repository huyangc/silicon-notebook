import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APITimeoutError, OpenAI

from app.core.config import Settings
from app.core.llm_logging import LLMInteractionLogger, new_interaction_id


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


def strip_json_fences(text: str) -> str:
    """Remove ```json ... ``` / ``` ... ``` markdown fences some models add."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    return cleaned


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[OpenAI] = None
        self.interaction_logger = LLMInteractionLogger(settings)

    @property
    def configured(self) -> bool:
        return self.settings.llm_configured

    def client(self) -> OpenAI:
        if not (self.settings.openai_compat_base_url and self.settings.openai_compat_api_key):
            raise RuntimeError("OpenAI-compatible API settings are not configured")
        if self._client is None:
            self._client = OpenAI(
                api_key=self.settings.openai_compat_api_key,
                base_url=self.settings.openai_compat_base_url,
                timeout=self.settings.openai_compat_timeout_seconds,
                # Don't let the SDK silently retry connection errors 2x: a stalled
                # connection would otherwise block ~3x the timeout per call. We
                # fail fast and let the caller (per-window extraction) drop it.
                max_retries=0,
            )
        return self._client

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        response_schema_hint: str,
    ) -> str:
        if not self.configured:
            raise RuntimeError("OpenAI-compatible LLM settings are not configured")
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
        model = self.settings.openai_compat_model
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": full_messages,
            "temperature": 0.1,
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
            },
        }
        start = time.perf_counter()
        try:
            attempts = 1 + self.settings.openai_compat_max_retries
            response = None
            for attempt in range(attempts):
                try:
                    # Prefer native JSON mode; fall back if the server rejects
                    # the param.
                    try:
                        response = self.client().chat.completions.create(
                            **kwargs, response_format={"type": "json_object"}
                        )
                    except (APIConnectionError, APITimeoutError):
                        # Network stall/timeout: do NOT retry the whole request
                        # without JSON mode (that would double the wait). Re-raise
                        # so the connection-retry loop below handles it.
                        raise
                    except Exception:
                        # Server rejected response_format (param unsupported):
                        # retry once in plain mode. NOT a connection error, so it
                        # never enters the bounded connection-retry loop.
                        response = self.client().chat.completions.create(**kwargs)
                    break
                except (APIConnectionError, APITimeoutError) as exc:
                    if attempt + 1 >= attempts:
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
                    time.sleep(min(2 ** attempt, 4))
            content = strip_json_fences(response.choices[0].message.content or "") or "{}"
            record["status"] = "ok"
            record["latency_ms"] = round((time.perf_counter() - start) * 1000)
            usage = _usage_dict(response)
            if usage:
                record["usage"] = usage
            record["response"] = {"content": logger.clip(content)}
            logger.log(record)
            return content
        except Exception as exc:
            record["status"] = "error"
            record["latency_ms"] = round((time.perf_counter() - start) * 1000)
            record["error"] = f"{type(exc).__name__}: {exc}"
            logger.log(record)
            raise
