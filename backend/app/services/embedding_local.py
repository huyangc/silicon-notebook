"""Local BGE via sentence-transformers — lazy singleton load, batch encode."""
from __future__ import annotations
from typing import List
from app.core.config import Settings


def _load_model(name: str):
    from sentence_transformers import SentenceTransformer  # heavy import, deferred
    return SentenceTransformer(name)


class LocalBGEEmbedder:
    _model = None  # process-wide singleton (model load is expensive)

    def __init__(self, settings: Settings):
        self.dim = settings.embed_dim
        self.model_name = settings.embed_model or "BAAI/bge-m3"

    def _model_or_load(self):
        if LocalBGEEmbedder._model is None:
            LocalBGEEmbedder._model = _load_model(self.model_name)
        return LocalBGEEmbedder._model

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vecs = self._model_or_load().encode(
            [t[:2000] for t in texts], normalize_embeddings=True, batch_size=64
        )
        return [list(map(float, v)) for v in vecs]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]
