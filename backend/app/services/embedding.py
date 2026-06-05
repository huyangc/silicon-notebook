"""Swappable embedding backends. Dev default: local BGE; prod: dashscope
text-embedding-v4. Tests use FakeEmbedder (deterministic, no network)."""
from __future__ import annotations

import hashlib
import struct
from typing import List, Protocol

from app.core.config import Settings


class Embedder(Protocol):
    dim: int
    def embed_texts(self, texts: List[str]) -> List[List[float]]: ...
    def embed_query(self, text: str) -> List[float]: ...


class FakeEmbedder:
    """Deterministic hash-based vectors for tests — no network, stable."""
    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _vec(self, text: str) -> List[float]:
        out: List[float] = []
        i = 0
        while len(out) < self.dim:
            h = hashlib.sha256(f"{i}:{text}".encode("utf-8")).digest()
            for j in range(0, len(h), 4):
                out.append(struct.unpack("<I", h[j:j + 4])[0] / 2**32)
                if len(out) >= self.dim:
                    break
            i += 1
        return out

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)


def make_embedder(settings: Settings) -> Embedder:
    provider = (settings.embed_provider or "").strip()
    if provider == "dashscope" and settings.embedder_configured:
        from app.services.embedding_dashscope import DashscopeEmbedder
        return DashscopeEmbedder(settings)
    return FakeEmbedder(dim=settings.embed_dim)


def embed_in_chunks(embed_fn, texts, chunk_size=200, logger=None):
    """逐块调用 embed_fn，单块异常则该块全记 None 并继续（不影响其余块）。
    返回与 texts 对齐的列表，元素为向量或 None。embed_fn(list[str]) -> list[vector]。"""
    out = [None] * len(texts)
    for start in range(0, len(texts), chunk_size):
        chunk = texts[start:start + chunk_size]
        try:
            vectors = embed_fn(chunk)
        except Exception as exc:  # noqa: BLE001 — best-effort，单块失败不阻塞全篇
            if logger is not None:
                logger.warning("embed chunk [%s:%s] failed: %s", start, start + len(chunk), exc)
            continue
        for offset, vec in enumerate(vectors):
            out[start + offset] = list(vec)
    return out
