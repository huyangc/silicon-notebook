"""Minimal OpenAI-compatible JSON client, configured from an env prefix so the
gold generator (GOLDGEN_) and product ("") can use different endpoints/models."""
from __future__ import annotations

import os
from typing import Dict, List, Optional


class KGClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120,
                 max_tokens: int = 0):
        self.base_url, self.api_key, self.model, self.timeout = base_url, api_key, model, timeout
        self.max_tokens = max_tokens   # 单次输出上限;<=0 则不传,由服务端默认
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=self.timeout)
        return self._client

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        response_schema_hint: str = "",
        retries: int = 4,
    ) -> str:
        # Stream the response: slow/reasoning models (e.g. deepseek-v4-pro) produce
        # long replies that the server drops on non-streaming requests (~120s cap);
        # streaming keeps the connection alive chunk-by-chunk. Retry with backoff so
        # a transient blip doesn't silently yield empty extractions.
        #
        # If a response_schema_hint is provided, prepend a system message (mirrors
        # OpenAICompatibleClient) so the model knows to return JSON matching the schema.
        import time
        if response_schema_hint:
            full_messages: List[Dict[str, str]] = [
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
        else:
            full_messages = list(messages)
        last = None
        for attempt in range(retries):
            try:
                _extra = ({"max_tokens": self.max_tokens}
                          if self.max_tokens and self.max_tokens > 0 else {})
                stream = self._ensure().chat.completions.create(
                    model=self.model, temperature=0,
                    response_format={"type": "json_object"}, stream=True,
                    messages=full_messages, **_extra)
                parts = []
                for chunk in stream:
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            parts.append(delta.content)
                return "".join(parts) or "{}"
            except Exception as exc:  # APIConnectionError / timeout / transient 5xx
                last = exc
                self._client = None  # force a fresh connection next attempt
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
        raise last


def make_client(env_prefix: str = "") -> KGClient:
    g = lambda k: os.environ.get(env_prefix + k, "")
    try:
        max_tokens = int(g("OPENAI_COMPAT_MAX_TOKENS") or "8192")
    except ValueError:
        max_tokens = 8192
    return KGClient(g("OPENAI_COMPAT_BASE_URL"), g("OPENAI_COMPAT_API_KEY"),
                    g("OPENAI_COMPAT_MODEL"), max_tokens=max_tokens)
