"""Bounded two-plane retrieval for owner-private notebook Memory."""
from __future__ import annotations

from typing import Iterable, Sequence

from app.models.memory import MemoryHit
from app.repositories.ports import MemoryStorePort
from app.services.embedding import Embedder
from app.core.query_syntax import (
    quoted_phrases,
    strip_accepted_quote_markers,
)
from app.services.retrieval import (
    RELEVANCE_FLOOR,
    _fuse,
    _tokens,
    cosine,
    keyword_basis,
)
from app.services.vector_index import decode_vector


MEMORY_AUTHORITY = {"candidate": 1, "confirmed": 3}
AUTHORITY_RANK = {
    "candidate": 1,
    "personal_source": 2,
    "confirmed_memory": 3,
    "base": 4,
}
MEMORY_SEMANTIC_ELIGIBILITY = 0.80


def authority_rank(kind: str) -> int:
    """Return the fixed conflict rank after relevance eligibility is proven."""
    try:
        return AUTHORITY_RANK[kind]
    except KeyError as exc:
        raise ValueError(f"unknown evidence authority: {kind}") from exc


def rerank_eligible_memory_hits(hits: Iterable[MemoryHit]) -> list[MemoryHit]:
    """Rank by relevance, using authority only for an exact relevance tie."""
    return sorted(
        hits,
        key=lambda hit: (float(hit.score), int(hit.authority), hit.memory_id),
        reverse=True,
    )


class MemoryRetriever:
    LEXICAL_CANDIDATES = 64
    VECTOR_CANDIDATES = 256
    # Memory is supplemental prompt evidence, not an unbounded transcript dump.
    # These deterministic character caps apply after retrieval/ranking; the
    # full provenance and content remain available in the anchor map.
    CONTEXT_HIT_CHAR_LIMIT = 1200
    CONTEXT_TOTAL_CHAR_LIMIT = 6000

    def __init__(self, store: MemoryStorePort, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    def replace_embedder(self, embedder: Embedder) -> None:
        self.embedder = embedder

    def _hits(
        self,
        user_id: str,
        notebook_id: str,
        query: str,
        statuses: Sequence[str],
        limit: int,
    ) -> list[MemoryHit]:
        clean_query = (query or "").strip()
        limit = max(1, min(int(limit), 50))
        if not clean_query:
            return []
        # Memory's candidate generation probes the WHOLE query as one value
        # (a single FTS phrase on SQLite, the same string on PostgreSQL), so the
        # quotes need handling twice over:
        #   * the markers must go, or the pattern carries characters no memory
        #     contains and the pool comes back empty (codex #410 round-2 P2);
        #   * each accepted phrase must ALSO probe on its own, or a memory
        #     holding the phrase but not the surrounding sentence never becomes
        #     a candidate — the syntax would then work everywhere except here
        #     (codex #410 round-6 P2). These are extra OR-ed terms inside the
        #     one existing bounded query, not extra round trips.
        # Scoring below keeps the ORIGINAL query, so the phrase still has to be
        # matched whole to earn its coverage.
        candidate_query = (
            strip_accepted_quote_markers(clean_query).strip() or clean_query
        )
        rows = self.store.memory_retrieval_rows(
            user_id,
            notebook_id,
            statuses,
            candidate_query,
            lexical_limit=self.LEXICAL_CANDIDATES,
            vector_limit=self.VECTOR_CANDIDATES,
            phrase_queries=quoted_phrases(clean_query),
        )
        try:
            query_vector = self.embedder.embed_query(candidate_query)
        except Exception:
            query_vector = None
        basis = keyword_basis(clean_query)
        hits: list[MemoryHit] = []
        for row in rows:
            item = row["record"]
            text = f"{item.title}\n{item.content_md}\n{' '.join(item.tags)}"
            lexical = basis.coverage(set(_tokens(text)), text)
            vector = decode_vector(row.get("vector"))
            has_vector = query_vector is not None and vector is not None
            semantic = cosine(query_vector, vector.tolist()) if has_vector else 0.0
            # Eligibility is decided from relevance signals before authority is
            # consulted.  The semantic floor also rejects uncalibrated hash-vector
            # background similarity while retaining strong vector-only matches.
            if lexical <= 0.0 and semantic < MEMORY_SEMANTIC_ELIGIBILITY:
                continue
            relevance = _fuse(lexical, semantic, has_vector)
            if relevance < RELEVANCE_FLOOR:
                continue
            hits.append(
                MemoryHit(
                    memory_id=item.id,
                    title=item.title,
                    text=item.content_md,
                    status=item.status,
                    authority=MEMORY_AUTHORITY[item.status],
                    score=relevance,
                    provenance=dict(item.provenance),
                )
            )
        return rerank_eligible_memory_hits(hits)[:limit]

    def notebook_memory_hits(
        self, user_id: str, notebook_id: str, query: str, limit: int = 8
    ) -> list[MemoryHit]:
        return self._hits(user_id, notebook_id, query, ("confirmed",), limit)

    def agent_memory_hits(
        self,
        user_id: str,
        notebook_id: str,
        query: str,
        include_candidates: bool,
        limit: int = 8,
    ) -> list[MemoryHit]:
        statuses = ("confirmed", "candidate") if include_candidates else ("confirmed",)
        return self._hits(user_id, notebook_id, query, statuses, limit)

    @staticmethod
    def context(
        hits: Sequence[MemoryHit], *, id_offset: int = 3000
    ) -> tuple[str, dict[str, dict]]:
        lines: list[str] = []
        id_map: dict[str, dict] = {}
        for index, hit in enumerate(hits, 1):
            key = f"k{id_offset + index}"
            line = f"{key}: [memory][personal][confirmed] {hit.title} — {hit.text}"
            if len(line) > MemoryRetriever.CONTEXT_HIT_CHAR_LIMIT:
                line = line[: MemoryRetriever.CONTEXT_HIT_CHAR_LIMIT - 1] + "…"
            separator_chars = 1 if lines else 0
            remaining = MemoryRetriever.CONTEXT_TOTAL_CHAR_LIMIT - (
                sum(len(item) for item in lines) + max(0, len(lines) - 1)
            )
            if len(line) + separator_chars > remaining:
                break
            lines.append(line)
            id_map[key] = {
                "object_id": hit.memory_id,
                "object_type": "memory",
                "name": hit.title,
                "definition": hit.text,
                "snippet": hit.text[:300],
                "source_title": "",
                "location_label": "Memory",
                "tier": "personal",
                "provenance": dict(hit.provenance),
                "relevance": float(hit.score),
            }
        return ("\n".join(lines) if lines else "(none)"), id_map
