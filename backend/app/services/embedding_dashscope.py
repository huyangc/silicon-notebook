"""dashscope (or any OpenAI-compatible) embeddings — batched + fail-fast."""
from __future__ import annotations
from typing import List
from openai import OpenAI
from app.core.config import Settings

_BATCH = 10  # dashscope text-embedding-v3/v4 hard-cap: batch input <= 10 items


class DashscopeEmbedder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.dim = settings.embed_dim
        self.model = settings.embed_model
        self._client = None

    def _ensure(self):
        if self._client is None:
            self._client = OpenAI(
                api_key=self.settings.embed_api_key,
                base_url=self.settings.embed_base_url,
                timeout=self.settings.openai_compat_timeout_seconds,
                max_retries=0,  # don't amplify a network stall (see llm.py fix)
            )
        return self._client

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        batch = max(1, min(getattr(self.settings, "embed_batch_size", 10), 10))
        trunc = getattr(self.settings, "embed_truncate_chars", 2000)
        out: List[List[float]] = []
        for i in range(0, len(texts), batch):
            chunk = [t[:trunc] for t in texts[i:i + batch]]
            resp = self._ensure().embeddings.create(model=self.model, input=chunk)
            out.extend(list(d.embedding) for d in resp.data)
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]
