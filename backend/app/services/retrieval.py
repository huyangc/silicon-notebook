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

# Process/flow-intent overrides: a "what are the steps / 展开流程" question wants
# procedures surfaced, not buried. Used INSTEAD of _TYPE_WEIGHT for such queries.
_PROCESS_TYPE_WEIGHT = {
    "procedure": 1.0,
    "claim": 0.9,
    "formula": 0.9,
    "concept": 0.6,
}

# Substring markers signalling the user wants a process/flow/steps answer.
_PROCESS_MARKERS = (
    "流程", "步骤", "怎么", "如何", "展开", "阶段", "画成", "过程", "顺序", "先后",
    "flow", "step", "procedure", "process", "pipeline", "stage", "walkthrough",
)


def is_process_query(text: str) -> bool:
    """True when the question is about a process/flow/steps (intent signal)."""
    t = (text or "").lower()
    return any(m in t for m in _PROCESS_MARKERS)


def type_weight(object_type: str, process_intent: bool) -> float:
    """Cross-type authority weight; process-intent questions stop penalising
    procedures (and slightly favour them)."""
    table = _PROCESS_TYPE_WEIGHT if process_intent else _TYPE_WEIGHT
    return table.get(object_type, 0.5)


def ensure_procedure_quota(scored_all, top_n, min_proc, key):
    """Take the top_n of an already-sorted `scored_all`, but guarantee at least
    `min_proc` procedures when the pool has them — back-fill from the remainder
    and evict the weakest non-procedure items. Never evicts a procedure; result
    is re-sorted by `key` descending and hard-capped at top_n (so a misconfigured
    min_proc > top_n can't grow the result past top_n)."""
    top = scored_all[:top_n]
    procs = [h for h in top if h.object_type == "procedure"]
    if len(procs) >= min_proc:
        return top
    have_ids = {h.object_id for h in top}
    extra = [h for h in scored_all[top_n:]
             if h.object_type == "procedure" and h.object_id not in have_ids]
    extra = extra[: min_proc - len(procs)]
    if not extra:
        return top
    non_proc = [h for h in top if h.object_type != "procedure"]
    drop_ids = {h.object_id for h in non_proc[len(non_proc) - len(extra):]}
    kept = [h for h in top if h.object_id not in drop_ids]
    return sorted(kept + extra, key=key, reverse=True)[:top_n]


def classify_evidence(top_hits, anchors, llm_grounded, tau_low, tau_high):
    """Relevance-aware grounding. Returns (evidence_level, top_relevance).

    - grounded : an answer-CITED hit is strongly relevant (>= tau_high) AND the
                 LLM self-reported grounded. (Can't fake grounding on junk.)
    - overview : some relevant hit exists (top relevance >= tau_low) but the
                 answer is largely extrapolated from thin evidence.
    - inferred : no relevant hit / nothing cited — general-knowledge answer.
    """
    top_rel = max((h.relevance for h in top_hits), default=0.0)
    if anchors:
        ids = {a.object_id for a in anchors}
        anchored_rel = max((h.relevance for h in top_hits if h.object_id in ids), default=0.0)
    else:
        anchored_rel = 0.0
    if top_hits and llm_grounded and anchors and anchored_rel >= tau_high:
        level = "grounded"
    elif top_hits and anchors and top_rel >= tau_low:
        level = "overview"
    else:
        level = "inferred"
    return level, top_rel


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


def cosine_sims(query_vector, id_to_vec):
    """一次矩阵运算算出 query 对一批向量的余弦相似度。返回 {id: sim}。
    等价于对每个 id 调 cosine(query_vector, vec)，但用 numpy 批量计算。"""
    import numpy as np

    if not query_vector or not id_to_vec:
        return {}
    ids = list(id_to_vec.keys())
    mat = np.asarray([id_to_vec[i] for i in ids], dtype=np.float64)
    q = np.asarray(query_vector, dtype=np.float64)
    if mat.ndim != 2 or mat.shape[1] != q.shape[0]:
        return {i: cosine(query_vector, id_to_vec[i]) for i in ids}
    qn = float(np.linalg.norm(q))
    row_norms = np.linalg.norm(mat, axis=1)
    denom = row_norms * qn
    dots = mat @ q
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(denom > 0, dots / denom, 0.0)
    return {i: float(s) for i, s in zip(ids, sims)}


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


def _fuse(keyword: float, semantic: float, has_vector: bool,
          w_keyword: float = W_KEYWORD, w_semantic: float = W_SEMANTIC) -> float:
    """Weighted-sum fusion, renormalized by active signals so keyword-only
    objects are scored on the same 0..1 scale instead of being capped at the
    keyword weight. Weights default to the module constants; the reasoning
    retriever overrides them per sub-query (prefer=keyword/semantic/balanced)."""
    semantic = max(0.0, semantic)
    denom = w_keyword + (w_semantic if has_vector else 0.0)
    if denom <= 0:
        return 0.0
    return (w_keyword * keyword + (w_semantic * semantic if has_vector else 0.0)) / denom


def score_knowledge(
    query: str,
    objects: List[dict],
    object_type: str,
    query_vector: Optional[List[float]] = None,
    element_vectors: Optional[Dict[str, List[float]]] = None,
    knowledge_vectors: Optional[Dict[str, List[float]]] = None,
    element_sims: Optional[Dict[str, float]] = None,
    knowledge_sims: Optional[Dict[str, float]] = None,
    w_keyword: float = W_KEYWORD,
    w_semantic: float = W_SEMANTIC,
) -> List[RetrievedKnowledge]:
    """Score knowledge by keyword + optional semantic similarity.

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
            if knowledge_sims is not None:
                s = knowledge_sims.get(object_id)
                if s is not None:
                    has_vector = True
                    semantic = max(semantic, s)
            elif knowledge_vectors:
                payload_vec = knowledge_vectors.get(object_id)
                if payload_vec:
                    has_vector = True
                    semantic = max(semantic, cosine(query_vector, payload_vec))
            for ev in evidence:
                eid = getattr(ev, "element_id", "") or ""
                if element_sims is not None:
                    s = element_sims.get(eid)
                    if s is not None:
                        has_vector = True
                        semantic = max(semantic, s)
                elif element_vectors:
                    vector = element_vectors.get(eid)
                    if vector:
                        has_vector = True
                        semantic = max(semantic, cosine(query_vector, vector))

        relevance = _fuse(keyword, semantic, has_vector, w_keyword, w_semantic)
        if relevance < RELEVANCE_FLOOR:
            continue
        final = relevance
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


def bm25_scores(query: str, docs: Sequence[tuple], k1: float = 1.5,
                b: float = 0.75) -> Dict[str, float]:
    """BM25 Okapi over (id, text) docs, using the CJK-aware `_tokens` tokenizer.

    IDF is computed over THIS doc set (a notebook's objects). Returns {id: score}
    for docs with score > 0 (query-term miss -> absent). Stopwords dropped from
    the query basis (same as keyword_score).
    """
    q_terms = [t for t in _tokens(query) if t not in _STOPWORDS]
    if not q_terms or not docs:
        return {}
    doc_tokens = {did: _tokens(text) for did, text in docs}
    n = len(doc_tokens)
    total_len = sum(len(t) for t in doc_tokens.values())
    avgdl = (total_len / n) if n else 1.0
    if avgdl <= 0:
        avgdl = 1.0
    df: Dict[str, int] = {}
    for toks in doc_tokens.values():
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    qset = set(q_terms)
    idf = {
        t: math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
        for t in qset
    }
    scores: Dict[str, float] = {}
    for did, toks in doc_tokens.items():
        if not toks:
            continue
        dl = len(toks)
        tf: Dict[str, int] = {}
        for t in toks:
            if t in qset:
                tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t, f in tf.items():
            s += idf.get(t, 0.0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scores[did] = s
    return scores


def rrf_fuse(rankings: Sequence[Dict[str, float]], k: int = 60) -> Dict[str, float]:
    """Reciprocal Rank Fusion. Each ranking maps id->score (higher=better).

    Returns id->fused score = sum over rankings of 1/(k + rank), rank from 1.
    """
    fused: Dict[str, float] = {}
    for ranking in rankings:
        ordered = sorted(ranking.items(), key=lambda kv: kv[1], reverse=True)
        for rank, (did, _s) in enumerate(ordered, start=1):
            fused[did] = fused.get(did, 0.0) + 1.0 / (k + rank)
    return fused


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
    element_sims: Optional[Dict[str, float]] = None,
) -> List[RetrievedElement]:
    scored: List[RetrievedElement] = []
    for element in elements:
        keyword = keyword_score(query, element["text"])
        semantic = 0.0
        vector = element.get("vector")
        has_vector = bool(query_vector and (element_sims is not None or vector))
        if has_vector:
            if element_sims is not None:
                semantic = element_sims.get(element["element_id"], 0.0)
            else:
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
