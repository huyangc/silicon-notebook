"""Minimal OpenAI-compatible JSON client, configured from an env prefix so the
gold generator (GOLDGEN_) and product ("") can use different endpoints/models."""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional


class KGClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120):
        self.base_url, self.api_key, self.model, self.timeout = base_url, api_key, model, timeout
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

    def chat_json(self, prompt: str, retries: int = 4) -> str:
        # DeepSeek can intermittently drop the connection on longer requests;
        # retry with linear backoff so a transient blip doesn't silently yield
        # empty extractions. Re-raises after the last attempt.
        import time
        last = None
        for attempt in range(retries):
            try:
                resp = self._ensure().chat.completions.create(
                    model=self.model, temperature=0,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}])
                return resp.choices[0].message.content or "{}"
            except Exception as exc:  # APIConnectionError / timeout / transient 5xx
                last = exc
                self._client = None  # force a fresh connection next attempt
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
        raise last


def make_client(env_prefix: str = "") -> KGClient:
    g = lambda k: os.environ.get(env_prefix + k, "")
    return KGClient(g("OPENAI_COMPAT_BASE_URL"), g("OPENAI_COMPAT_API_KEY"),
                    g("OPENAI_COMPAT_MODEL"))


def safe_json(raw: str) -> dict:
    if not raw:
        return {}
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        out = json.loads(t)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}
