"""Hybrid retrieval over notebook knowledge.

Combines keyword matching with optional embedding cosine similarity, then an
optional structured-scenario boost. Vectors live in `element_embeddings` /
`knowledge_embeddings` as JSON; cosine is computed in Python so the local beta
needs no pgvector. When no embeddings exist the search degrades gracefully to
keyword-only.

Tokenization is CJK-aware: runs of Chinese characters are turned into character
bi-grams (single CJK chars become uni-grams) so a Chinese-first corpus is
actually searchable by keyword. Latin/digit runs keep word-level tokens. This
tokenizer is reused by extraction (evidence binding) and the scenario boost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.models.schemas import Evidence


# --- Tunable scoring constants (kept here so they can be tuned in one place) ---
# Hybrid fusion weights. Renormalized per object by which signals are active, so
# keyword-only objects are not unfairly capped (see `score_knowledge`).
W_KEYWORD = 0.4
W_SEMANTIC = 0.6
# Candidates below this fused relevance are dropped as noise.
RELEVANCE_FLOOR = 0.12
# Structured-scenario boost strength: final = relevance * (1 + ALPHA * boost).
SCENARIO_BOOST_ALPHA = 0.5

# Scenario fields used for structured matching against rule applies_to/condition.
SCENARIO_FIELDS = (
    "domain",
    "block_type",
    "package_type",
    "design_stage",
    "signal_type",
    "concern",
    "constraint",
    "process_or_node",
    "application",
)


@dataclass
class RetrievedKnowledge:
    object_id: str
    object_type: str
    payload: Dict[str, object]
    evidence: List[Evidence] = field(default_factory=list)
    score: float = 0.0
    # Fused relevance before type weight / scenario boost (0..1).
    relevance: float = 0.0
    # Type authority weight, kept separate so it does not pollute relevance
    # ranking; callers may use it for cross-type tie-breaking / grouping.
    weight: float = 0.0
    status: str = "approved"
    owner: str = ""
    last_reviewed: str = ""


@dataclass
class RetrievedElement:
    element_id: str
    source_id: str
    source_title: str
    location_label: str
    element_type: str
    text: str
    score: float = 0.0


# KG node-type authority weights: claim/formula are primary knowledge carriers;
# procedure is process-oriented; concept is definitional/supporting.
# Used for cross-type tie-breaking / grouping only, NOT multiplied into
# within-type relevance ranking.
_TYPE_WEIGHT = {
    "claim": 1.0,
    "formula": 1.0,
    "procedure": 0.7,
    "concept": 0.5,
}


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # Extension A
        or 0xF900 <= code <= 0xFAFF  # Compatibility Ideographs
        or 0x3040 <= code <= 0x30FF  # Hiragana + Katakana
    )


def _segment_tokens(chunk: str) -> List[str]:
    """Tokenize a single alnum chunk that may mix CJK and latin/digit runs.

    CJK run of length 1 -> the uni-gram; length >= 2 -> sliding bi-grams.
    Latin/digit run -> the whole run if longer than one char.
    """
    tokens: List[str] = []
    i = 0
    n = len(chunk)
    while i < n:
        if _is_cjk(chunk[i]):
            j = i
            while j < n and _is_cjk(chunk[j]):
                j += 1
            run = chunk[i:j]
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[k : k + 2] for k in range(len(run) - 1))
            i = j
        else:
            j = i
            while j < n and not _is_cjk(chunk[j]):
                j += 1
            run = chunk[i:j]
            if len(run) > 1:
                tokens.append(run)
            i = j
    return tokens


def _tokens(text: str) -> List[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    tokens: List[str] = []
    for chunk in cleaned.split():
        tokens.extend(_segment_tokens(chunk))
    return tokens


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


_STOPWORDS = {
    # en
    "the","a","an","is","are","was","were","be","of","to","in","on","for","and",
    "or","what","which","how","why","its","it","this","that","these","those","do",
    "does","with","as","by","at","from","has","have","can","you","your","i","we",
    # zh (function words)
    "的","了","是","有","和","与","它","这","那","什么","怎么","哪些","以及","并",
    "吗","呢","在","对","把","及","或",
}


def keyword_score(query: str, text: str) -> float:
    """Fraction of (content) query tokens present in the text (0..1).

    Stopwords are dropped from the query basis so verbose phrasings ("what is
    X and what are its problems") aren't diluted relative to concise ones.
    """
    query_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    if not query_tokens:
        return 0.0
    haystack = set(_tokens(text))
    hits = sum(1 for token in query_tokens if token in haystack)
    return hits / len(query_tokens)


def token_overlap(span: str, text: str) -> float:
    """Fraction of `span` tokens present in `text` (0..1). Used for evidence binding."""
    span_tokens = set(_tokens(span))
    if not span_tokens:
        return 0.0
    haystack = set(_tokens(text))
    return sum(1 for token in span_tokens if token in haystack) / len(span_tokens)


def _fuse(keyword: float, semantic: float, has_vector: bool) -> float:
    """Weighted-sum fusion, renormalized by active signals so keyword-only
    objects are scored on the same 0..1 scale instead of being capped at W_KEYWORD."""
    semantic = max(0.0, semantic)
    denom = W_KEYWORD + (W_SEMANTIC if has_vector else 0.0)
    if denom <= 0:
        return 0.0
    return (W_KEYWORD * keyword + (W_SEMANTIC * semantic if has_vector else 0.0)) / denom


def structured_boost(scenario: Optional[Dict[str, str]], payload: Dict[str, object]) -> float:
    """Fraction of provided scenario fields whose tokens overlap the rule's
    applies_to / condition / title (0..1). 0 when no scenario or no targets."""
    if not scenario:
        return 0.0
    targets: List[str] = []
    for key in ("applies_to", "condition", "title", "use_when"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            targets.extend(str(v) for v in value)
        elif value:
            targets.append(str(value))
    target_tokens = set(_tokens(" ".join(targets)))
    if not target_tokens:
        return 0.0
    provided = 0
    hits = 0
    for key in SCENARIO_FIELDS:
        value = scenario.get(key)
        if not (isinstance(value, str) and value.strip()):
            continue
        provided += 1
        if set(_tokens(value)) & target_tokens:
            hits += 1
    return hits / provided if provided else 0.0


def score_knowledge(
    query: str,
    objects: List[dict],
    object_type: str,
    query_vector: Optional[List[float]] = None,
    element_vectors: Optional[Dict[str, List[float]]] = None,
    knowledge_vectors: Optional[Dict[str, List[float]]] = None,
    scenario: Optional[Dict[str, str]] = None,
) -> List[RetrievedKnowledge]:
    """Score knowledge by keyword + optional semantic similarity + optional
    structured-scenario boost.

    Semantic signal is the best cosine between the query and either the object's
    own payload embedding (`knowledge_vectors[object_id]`) or any of its evidence
    element embeddings (`element_vectors[element_id]`). When `query_vector` is
    None (no embedding configured) this degrades to keyword-only.
    """
    weight = _TYPE_WEIGHT.get(object_type, 0.5)
    scored: List[RetrievedKnowledge] = []
    for obj in objects:
        object_id = obj["id"]
        payload = obj.get("payload", {})
        text = _payload_text(payload)
        evidence = obj.get("evidence", [])
        evidence_text = " ".join(e.quoted_span for e in evidence)
        keyword = keyword_score(query, f"{text} {evidence_text}")

        semantic = 0.0
        has_vector = False
        if query_vector:
            payload_vec = knowledge_vectors.get(object_id) if knowledge_vectors else None
            if payload_vec:
                has_vector = True
                semantic = max(semantic, cosine(query_vector, payload_vec))
            if element_vectors:
                for ev in evidence:
                    vector = element_vectors.get(getattr(ev, "element_id", "") or "")
                    if vector:
                        has_vector = True
                        semantic = max(semantic, cosine(query_vector, vector))

        relevance = _fuse(keyword, semantic, has_vector)
        if relevance < RELEVANCE_FLOOR:
            continue
        boost = structured_boost(scenario, payload)
        final = relevance * (1.0 + SCENARIO_BOOST_ALPHA * boost)
        scored.append(
            RetrievedKnowledge(
                object_id=object_id,
                object_type=object_type,
                payload=payload,
                evidence=evidence,
                score=final,
                relevance=relevance,
                weight=weight,
                status=str(obj.get("status", "approved")),
                owner=str(obj.get("owner", "")),
                last_reviewed=str(obj.get("last_reviewed", "")),
            )
        )
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def _payload_text(payload: Dict[str, object]) -> str:
    parts: List[str] = []
    for key, value in payload.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
    return " ".join(parts)


def score_elements(
    query: str,
    elements: List[dict],
    query_vector: Optional[List[float]] = None,
    limit: int = 8,
) -> List[RetrievedElement]:
    scored: List[RetrievedElement] = []
    for element in elements:
        keyword = keyword_score(query, element["text"])
        semantic = 0.0
        vector = element.get("vector")
        has_vector = bool(query_vector and vector)
        if has_vector:
            semantic = cosine(query_vector, vector)
        score = _fuse(keyword, semantic, has_vector)
        if score < RELEVANCE_FLOOR:
            continue
        scored.append(
            RetrievedElement(
                element_id=element["element_id"],
                source_id=element["source_id"],
                source_title=element.get("source_title", ""),
                location_label=element.get("location_label", ""),
                element_type=element.get("element_type", ""),
                text=element["text"],
                score=score,
            )
        )
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[:limit]
