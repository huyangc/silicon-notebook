"""dashscope (or any OpenAI-compatible) embeddings — batched + 429 backoff."""
from __future__ import annotations
import time
from typing import List
import httpx
from openai import OpenAI, RateLimitError
from app.core.config import Settings

_BATCH = 10  # dashscope text-embedding-v3/v4 hard-cap: batch input <= 10 items


class DashscopeEmbedder:
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_connections: int | None = None,
    ):
        self.settings = settings
        self.dim = settings.embed_dim
        self.base_url = (base_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.max_connections = max(1, int(max_connections or 1))
        self._client = None

    def _ensure(self):
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
                max_retries=0,  # don't amplify a network stall (see llm.py fix)
                http_client=http_client,
            )
        return self._client

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        batch = max(1, min(getattr(self.settings, "embed_batch_size", 10), 10))
        trunc = getattr(self.settings, "embed_truncate_chars", 2000)
        out: List[List[float]] = []
        for i in range(0, len(texts), batch):
            chunk = [t[:trunc] for t in texts[i:i + batch]]
            out.extend(self._embed_chunk(chunk))
        return out

    def _embed_chunk(self, chunk: List[str]) -> List[List[float]]:
        """Embed one batch with exponential backoff on 429 rate limits.

        Concurrent bulk ingestion easily bursts past the embedding service's
        QPS; a 429 means "retry later", not "give up" — backing off here keeps
        vectors from silently going missing (which then degrades retrieval and
        concept clustering). Non-429 errors propagate immediately."""
        retries = max(0, getattr(self.settings, "embed_rate_limit_retries", 5))
        base = getattr(self.settings, "embed_rate_limit_base_delay", 2.0)
        for attempt in range(retries + 1):
            try:
                resp = self._ensure().embeddings.create(model=self.model, input=chunk)
                return [list(d.embedding) for d in resp.data]
            except RateLimitError:
                if attempt >= retries:
                    raise
                time.sleep(base * (2 ** attempt))
        return []  # unreachable (loop returns or raises)

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()


def build_dashscope_embedder(
    settings: Settings,
    *,
    base_url: str,
    api_key: str,
    model: str,
) -> DashscopeEmbedder:
    """Construct the raw adapter inside its reviewed transport boundary."""
    return DashscopeEmbedder(
        settings,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
