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

One thing the query side does NOT tokenize: a span the user wrapped in ASCII
double quotes. `KeywordBasis` keeps it whole, so it is covered only by a
haystack that carries the entire phrase (see `keyword_basis`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import (
    AbstractSet,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Set,
)

from app.core.query_syntax import quoted_phrases, unquoted_remainder
from app.domain.retrieval import (
    GapRelationRow,
    NeighborExpansion,
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
    RetrievedRelation,
    RetrievalSupport,
    W_KEYWORD,
    W_SEMANTIC,
)
from app.models.common import Evidence  # compatibility re-export
from app.repositories.lexical_query import spaced_model_name_parts
from app.services.source_element_selection import (
    rank_source_chunks,
    rank_source_elements,
)


# --- Tunable scoring constants (kept here so they can be tuned in one place) ---
# Hybrid fusion weights. Renormalized per object by which signals are active, so
# keyword-only objects are not unfairly capped (see `score_knowledge`).
# Candidates below this fused relevance are dropped as noise.
RELEVANCE_FLOOR = 0.12


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


def est_tokens(text: str) -> int:
    """粗估 token(无 tiktoken):中英混排约 3.5 字符/token,向上取整。仅用于预算截断。"""
    return math.ceil(len(text or "") / 3.5)


def truncate_by_tokens(items, key, max_tokens):
    """按序累加 est_tokens(key(item)),首次超 max_tokens 即停(保留之前的);镜像 LightRAG。"""
    out, used = [], 0
    for it in items:
        used += est_tokens(key(it))
        if used > max_tokens and out:
            break
        out.append(it)
    return out


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


def classify_evidence(
    top_hits,
    anchors,
    llm_grounded,
    tau_low,
    tau_high,
    *,
    exact_evidence_keys=None,
):
    """Relevance-aware grounding. Returns (evidence_level, top_relevance).

    - grounded : an answer-CITED ranked hit is strongly relevant (>= tau_high),
                 or the cited key came from a source-backed exact enumeration,
                 AND the LLM self-reported grounded.
    - overview : some relevant hit exists (top relevance >= tau_low) but the
                 answer is largely extrapolated from thin evidence.
    - inferred : no relevant hit / nothing cited — general-knowledge answer.
    """
    exact_keys = set(exact_evidence_keys or ())
    top_rel = max((h.relevance for h in top_hits), default=0.0)
    if anchors:
        ids = {a.object_id for a in anchors}
        anchored_rel = max((h.relevance for h in top_hits if h.object_id in ids), default=0.0)
        exact_anchored = bool({a.key for a in anchors} & exact_keys)
    else:
        anchored_rel = 0.0
        exact_anchored = False
    if llm_grounded and anchors and (
        (top_hits and anchored_rel >= tau_high) or exact_anchored
    ):
        level = "grounded"
    elif anchors and ((top_hits and top_rel >= tau_low) or exact_anchored):
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


def _model_name_aliases(text: str) -> List[str]:
    """Compact mixed letter/digit names across cosmetic separators.

    Product/model names are routinely written both as ``Cosmos3`` and
    ``Cosmos 3`` (also ``GPT-4o``/``GPT4o``).  Only mixed ASCII letter+digit
    shapes qualify; ordinary prose words and section numbers gain no alias.
    """
    return [f"{stem}{suffix}".lower()
            for stem, suffix in spaced_model_name_parts(text)]


def _tokens(text: str) -> List[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    tokens: List[str] = []
    for chunk in cleaned.split():
        tokens.extend(_segment_tokens(chunk))
    # Append-only aliasing: a haystack token set must stay a SUPERSET of the
    # plain segmentation.  Dropping the component runs here would run on BOTH
    # sides of every comparison (documents, evidence spans, relation names, not
    # just queries), so a document saying "Cosmos 3" would stop matching a
    # bare-stem query ("cosmos"), and ordinary prose ("scored 3 points") would
    # lose its verb.  The query-side half of the normalization — counting a
    # spaced model name as ONE requirement — lives in keyword_basis, the only
    # query-side entry point.
    tokens.extend(_model_name_aliases(text))
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


def keyword_score_tokens(query_tokens: AbstractSet[str], haystack_tokens: AbstractSet[str]) -> float:
    """Fraction of (content) query tokens present in a pre-tokenized haystack (0..1)."""
    if not query_tokens:
        return 0.0
    hits = sum(1 for token in query_tokens if token in haystack_tokens)
    return hits / len(query_tokens)


@dataclass(frozen=True)
class KeywordBasis:
    """The query side of keyword coverage: loose tokens + user-quoted phrases.

    A `"..."` span is ONE indivisible unit of the basis. It counts as covered
    only when the haystack carries the whole phrase, and its own words never
    count separately — otherwise a document holding `timing` alone would collect
    a third of the credit for `"static timing analysis"`, which is the dilution
    the quotes exist to forbid.

    Built once per query and reused across a candidate pool: the parse and the
    query-side tokenization are per-query work, not per-candidate work.
    """

    tokens: FrozenSet[str]
    phrases: tuple[str, ...] = ()

    def coverage(self, haystack_tokens: AbstractSet[str], haystack_text: str = "") -> float:
        """Covered fraction of the basis (0..1).

        `haystack_text` is only read when the query actually carries phrases, so
        callers holding a cached token set pay nothing for the parameter until a
        user asks for one.
        """
        total = len(self.tokens) + len(self.phrases)
        if not total:
            return 0.0
        hits = sum(1 for token in self.tokens if token in haystack_tokens)
        if self.phrases:
            haystack = _normalize(haystack_text)
            hits += sum(1 for phrase in self.phrases if phrase in haystack)
        return hits / total


def keyword_basis(query: str, *, honor_quotes: bool = True) -> KeywordBasis:
    """Decompose a query into its keyword-coverage basis.

    Stopwords are dropped so verbose phrasings ("what is X and what are its
    problems") aren't diluted relative to concise ones.

    `honor_quotes=False` is for the callers that compare two stored TEXTS rather
    than scoring a user query — a document that happens to contain a quotation
    is not stating a search constraint.
    """
    phrases = tuple(_normalize(p) for p in quoted_phrases(query)) if honor_quotes else ()
    body = unquoted_remainder(query) if phrases else query
    tokens = {t for t in _tokens(body) if t not in _STOPWORDS}
    # Query-side half of model-name aliasing: "Cosmos 3" is ONE requirement,
    # not two.  _tokens appended the compact alias; dropping the component runs
    # HERE (and only here) keeps the coverage denominator honest while every
    # haystack keeps its stems — this function is never used to tokenize a
    # document, so bare-stem queries still match spaced-name documents.
    components = {
        part.lower()
        for match in spaced_model_name_parts(body)
        for part in match
        if len(part) > 1
    }
    if components:
        tokens -= components
    return KeywordBasis(frozenset(tokens), phrases)


def probe_keyword_basis(terms: Sequence[str]) -> KeywordBasis:
    """Coverage basis for names a channel has ALREADY selected — every one atomic.

    Used by producers that score against the terms they probed rather than
    against the caller's query. Each of those terms earned its chunks by
    matching as a literal substring, so "was this name covered" has exactly one
    honest answer: does the text contain that string. Tokenizing them instead
    credits a chunk that holds none of the names as units — `config.yaml` split
    into `config`/`yaml`, `静态时序分析` into bigrams, `static timing analysis`
    into three loose words (codex #410 rounds 3 and 8).

    Atomicity is a property of the PROBE, not of the string's shape: an earlier
    version inferred it from "contains a space", which silently demoted every
    punctuation-joined and CJK phrase back to tokens. It also cannot zero out a
    legitimate chunk, because this channel only ever returns chunks whose text
    or breadcrumb carried the name that fetched them.
    """
    return KeywordBasis(
        frozenset(),
        tuple(_normalize(term) for term in terms if str(term).strip()),
    )


def keyword_score(query: str, text: str, *, honor_quotes: bool = True) -> float:
    """Fraction of the query's keyword basis present in the text (0..1).

    Thin wrapper over `keyword_basis` for callers scoring a single document; a
    caller looping over candidates should hoist the basis out of the loop.
    """
    return keyword_basis(query, honor_quotes=honor_quotes).coverage(
        set(_tokens(text)), text
    )


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
    keyword_token_sets: Optional[Dict[str, FrozenSet[str]]] = None,
    isolated_ids: Optional[Set[str]] = None,
    w_isolated_penalty: float = 1.0,
) -> List[RetrievedKnowledge]:
    """Score knowledge by keyword + optional semantic similarity.

    Semantic signal is the best cosine between the query and either the object's
    own payload embedding (`knowledge_vectors[object_id]`) or any of its evidence
    element embeddings (`element_vectors[element_id]`). When `query_vector` is
    None (no embedding configured) this degrades to keyword-only.

    `isolated_ids` / `w_isolated_penalty`: 孤立节点(degree-0)排序降权。
    penalty 仅乘入 score(排序用),绝不触碰 relevance([0,1]/tau 不变)。
    与 _EDGE_TYPE_RANK_WEIGHT 同模式:降权仅作用于排序(score),绝不进 relevance。
    默认 isolated_ids=None 或 w_isolated_penalty=1.0 → 精确 no-op,现有调用者不受影响。
    """
    weight = _TYPE_WEIGHT.get(object_type, 0.5)
    basis = keyword_basis(query)
    scored: List[RetrievedKnowledge] = []
    for obj in objects:
        object_id = obj["id"]
        payload = obj.get("payload", {})
        text = _payload_text(payload)
        evidence = obj.get("evidence", [])
        evidence_text = " ".join(e.quoted_span for e in evidence)
        if keyword_token_sets is not None and object_id in keyword_token_sets:
            # The cached set is tokenized from this same haystack, so a quoted
            # phrase can still be checked against the text without giving up the
            # cache. The join is skipped entirely when the query has no phrase,
            # which is the path this cache exists to keep cheap.
            keyword = basis.coverage(
                keyword_token_sets[object_id],
                f"{text} {evidence_text}" if basis.phrases else "",
            )
        else:
            haystack = f"{text} {evidence_text}"
            keyword = basis.coverage(set(_tokens(haystack)), haystack)

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
        # 降权仅作用于排序(score),绝不进 relevance(守 [0,1]/tau)。
        # 与 _EDGE_TYPE_RANK_WEIGHT 同模式(见下方注释)。
        final = relevance * (
            w_isolated_penalty
            if (isolated_ids is not None and object_id in isolated_ids)
            else 1.0
        )
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


# 边类型 rank 乘子:about 是弱结构边(本语料占 ~57%),降权仅作用于排序(score),
# 绝不进 relevance(守 [0,1]/tau)。推理边保持 1.0。
_EDGE_TYPE_RANK_WEIGHT = {"about": 0.5}


def edge_type_rank_weight(edge_type: str) -> float:
    return _EDGE_TYPE_RANK_WEIGHT.get(edge_type, 1.0)


def score_relations(
    query: str,
    relations: List[dict],
    query_vector: Optional[List[float]] = None,
    relation_sims: Optional[Dict[str, float]] = None,
    w_keyword: float = W_KEYWORD,
    w_semantic: float = W_SEMANTIC,
    downweight_edges: bool = False,
) -> List[RetrievedRelation]:
    """关系打分:关键词(关系 text)+ 可选语义(query vs 关系自有向量,来自
    relation_sims)。与 score_knowledge 同尺:max(0,cosine) 经 _fuse → relevance
    ∈[0,1],低于 RELEVANCE_FLOOR 丢弃。relation_sims 是独立关系索引(dual-index
    分离,不与节点矩阵合并)。每个 relations 项: {id, source_object_id,
    target_object_id, edge_type, text}。"""
    basis = keyword_basis(query)
    scored: List[RetrievedRelation] = []
    for rel in relations:
        rid = rel["id"]
        text = rel.get("text", "")
        keyword = basis.coverage(set(_tokens(text)), text)
        semantic = 0.0
        has_vector = False
        if query_vector and relation_sims is not None:
            s = relation_sims.get(rid)
            if s is not None:
                has_vector = True
                semantic = max(semantic, s)
        relevance = _fuse(keyword, semantic, has_vector, w_keyword, w_semantic)
        if relevance < RELEVANCE_FLOOR:
            continue
        rank_mult = edge_type_rank_weight(rel["edge_type"]) if downweight_edges else 1.0
        scored.append(RetrievedRelation(
            relation_id=rid,
            source_object_id=rel["source_object_id"],
            target_object_id=rel["target_object_id"],
            edge_type=rel["edge_type"],
            text=text,
            evidence=rel.get("evidence", []),
            score=relevance * rank_mult,
            relevance=relevance,
            review_status=str(rel.get("review_status") or "pending"),
        ))
    scored.sort(key=lambda it: it.score, reverse=True)
    return scored


def fold_by_canonical(hits, cluster_map):
    """非销毁折叠:同一 canonical_id 只保留打分最高的成员(输入须已按 score 降序),
    其余 drop。无映射的 hit 按自身 object_id(不折)。不改 hit 内容,只去重候选。"""
    seen, out = set(), []
    for h in hits:
        c = cluster_map.get(h.object_id, h.object_id)
        if c in seen:
            continue
        seen.add(c)
        out.append(h)
    return out


def bm25_scores(query: str, docs: Sequence[tuple], k1: float = 1.5,
                b: float = 0.75) -> Dict[str, float]:
    """BM25 Okapi over (id, text) docs, using the CJK-aware `_tokens` tokenizer.

    IDF is computed over THIS doc set (a notebook's objects). Returns {id: score}
    for docs with score > 0 (query-term miss -> absent). Stopwords dropped from
    the query basis (same as keyword_score).

    A user-quoted phrase enters as ONE atomic term whose tf is its occurrence
    count as a contiguous substring, exactly as `KeywordBasis` treats it, and its
    component words are not query terms. Without this, the opt-in RRF path would
    rank purely on the split words: `relevance` there is phrase-aware but it is
    only reported, so a document merely scattering `static`, `timing` and
    `analysis` would out-rank one carrying the phrase — quoting would have no
    effect on the one thing that path decides. Whitespace inside both the phrase
    and the document is normalized here, so a phrase broken across a line break
    still counts (the trigram/ILIKE candidate probes are literal and cannot do
    this — see `split_quoted_phrases`).
    """
    phrases = [_normalize(p) for p in quoted_phrases(query)]
    q_terms = [
        t for t in _tokens(unquoted_remainder(query) if phrases else query)
        if t not in _STOPWORDS
    ]
    if (not q_terms and not phrases) or not docs:
        return {}
    doc_tokens = {did: _tokens(text) for did, text in docs}
    # Only paid for when the user actually quoted something.
    doc_phrase_tf: Dict[str, Dict[str, int]] = {}
    if phrases:
        for did, text in docs:
            haystack = _normalize(text)
            # `\x00` namespaces the phrase away from token keys: the same string
            # can legitimately be both a phrase and a token ("abc" 与 abc 的区别),
            # and sharing one key would let a phrase count overwrite a token
            # count and double-increment that key's df.
            counts = {f"\x00{p}": haystack.count(p) for p in phrases}
            doc_phrase_tf[did] = {p: c for p, c in counts.items() if c > 0}
    n = len(doc_tokens)
    total_len = sum(len(t) for t in doc_tokens.values())
    avgdl = (total_len / n) if n else 1.0
    if avgdl <= 0:
        avgdl = 1.0
    df: Dict[str, int] = {}
    for toks in doc_tokens.values():
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    for counts in doc_phrase_tf.values():
        for phrase in counts:
            df[phrase] = df.get(phrase, 0) + 1
    qset = set(q_terms)
    idf = {
        t: math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
        for t in (qset | {f"\x00{p}" for p in phrases})
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
        tf.update(doc_phrase_tf.get(did, {}))
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


# 非语义元数据字段:仅用于显示/引用,绝不进检索文本(否则污染 embedding/关键词)。
_PAYLOAD_SKIP_KEYS = frozenset({"section_path"})


def _payload_text(payload: Dict[str, object]) -> str:
    parts: List[str] = []
    for key, value in payload.items():
        if str(key).startswith("_") or key in _PAYLOAD_SKIP_KEYS:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
    return " ".join(parts)


def relation_embed_text(src_name: str, edge_type: str, tgt_name: str,
                        evidence_spans: Sequence[str],
                        max_evidence_chars: int = 400) -> str:
    """关系的 embedding/关键词文本。纯检索:只用已有边字段(不依赖抽取改动)。
    格式 '<src> —<edge_type>→ <tgt>. <evidence...>';evidence 截断到上限。"""
    ev = " ".join(s.strip() for s in evidence_spans if s and s.strip())
    if len(ev) > max_evidence_chars:
        ev = ev[:max_evidence_chars]
    head = f"{src_name} —{edge_type}→ {tgt_name}."
    return f"{head} {ev}".strip()


def score_elements(
    query: str,
    elements: List[dict],
    query_vector: Optional[List[float]] = None,
    limit: int = 8,
    element_sims: Optional[Dict[str, float]] = None,
) -> List[RetrievedElement]:
    basis = keyword_basis(query)
    scored: List[RetrievedElement] = []
    for element in elements:
        keyword = basis.coverage(set(_tokens(element["text"])), element["text"])
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
    return rank_source_elements(scored, limit)


_REVIEW_STRICTNESS = {"": 0, "verified": 1, "pending": 2, "rejected": 3}


def merge_retrieval_supports(*groups: Sequence[RetrievalSupport]) -> tuple[RetrievalSupport, ...]:
    merged: Dict[tuple[str, str, str], RetrievalSupport] = {}
    order: List[tuple[str, str, str]] = []
    for group in groups:
        for support in group:
            key = (support.origin, support.support_kind, support.support_id)
            current = merged.get(key)
            if current is None:
                merged[key] = support
                order.append(key)
                continue
            scores = [value for value in (current.score, support.score) if value is not None]
            review = max(
                (current.review_status_snapshot, support.review_status_snapshot),
                key=lambda value: _REVIEW_STRICTNESS.get(value, 2),
            )
            merged[key] = RetrievalSupport(
                origin=current.origin,
                support_kind=current.support_kind,
                support_id=current.support_id,
                score=max(scores) if scores else None,
                review_status_snapshot=review,
            )
    return tuple(merged[key] for key in order)


def add_chunk_supports(
    chunks: Sequence["RetrievedChunk"],
    supports_by_chunk: Dict[str, Sequence[RetrievalSupport]],
) -> List["RetrievedChunk"]:
    for chunk in chunks:
        chunk.retrieval_supports = merge_retrieval_supports(
            chunk.retrieval_supports, supports_by_chunk.get(chunk.chunk_id, ())
        )
    return list(chunks)


def is_graph_only_chunk(chunk: "RetrievedChunk") -> bool:
    origins = {support.origin for support in chunk.retrieval_supports}
    return bool(origins & {"kg_source", "ppr", "relation"}) and not bool(
        origins & {"semantic", "lexical"}
    )


@dataclass(frozen=True)
class ReserveRule:
    """One bounded floor inside the SAME token budget.

    ``holds`` marks the chunks that satisfy this floor — they count toward it
    and, once a rule is active, they are protected from being evicted to make
    room for another rule's candidate. ``admits`` marks the chunks that may be
    pulled in to fill it; it is usually stricter than ``holds`` (a graph-only
    chunk counts as graph coverage even when it is too weak to be worth pulling
    in deliberately).
    """

    reserve: int
    holds: Callable[["RetrievedChunk"], bool]
    admits: Callable[["RetrievedChunk"], bool]


def graph_reserve_rule(
    reserve: int, *, min_graph_score: float = RELEVANCE_FLOOR
) -> ReserveRule:
    """The historical graph-only reserve, unchanged."""

    def _admits(chunk: "RetrievedChunk") -> bool:
        if not is_graph_only_chunk(chunk) or not chunk.element_ids:
            return False
        if any(
            support.origin == "relation"
            and support.review_status_snapshot == "rejected"
            for support in chunk.retrieval_supports
        ):
            return False
        graph_supports = [
            support for support in chunk.retrieval_supports
            if support.origin in {"kg_source", "ppr", "relation"}
            and support.review_status_snapshot != "rejected"
        ]
        best = max(
            (support.score for support in graph_supports if support.score is not None),
            default=chunk.relevance,
        )
        return bool(graph_supports) and best >= min_graph_score

    return ReserveRule(reserve=reserve, holds=is_graph_only_chunk, admits=_admits)


def exact_section_reserve_rule(
    reserve: int, chunk_ids: AbstractSet[str]
) -> ReserveRule:
    """Reserve slots for chunks the exact-identifier channel fetched.

    Membership is an explicit id set rather than a new `RetrievalSupport`
    origin: those chunks legitimately carry a `lexical` support (they ARE
    substring matches), and widening the support vocabulary would reach
    `is_graph_only_chunk` and every other consumer of that literal for no gain.

    Unlike the graph rule this does not demand `element_ids`: exact hits are
    ordinary chunks that the normal lexical path could have surfaced on its
    own, whereas graph-only chunks are indirect evidence that must at least be
    citable to earn a reserved slot.
    """
    ids = frozenset(chunk_ids)

    def _member(chunk: "RetrievedChunk") -> bool:
        return chunk.chunk_id in ids

    return ReserveRule(reserve=reserve, holds=_member, admits=_member)


def is_generated_question_only_chunk(chunk: "RetrievedChunk") -> bool:
    """Whether a chunk exists only because the optional question index hit it.

    A collision with any historical channel is deliberately *not* supplemental:
    support union keeps that chunk in the baseline pool, so enabling the index
    cannot demote a candidate semantic/lexical/KG retrieval already found.
    """
    supports = tuple(chunk.retrieval_supports or ())
    return bool(supports) and all(
        support.origin == "generated_question" for support in supports
    )


def prefer_stronger_chunk_candidate(
    existing: "RetrievedChunk", candidate: "RetrievedChunk"
) -> "RetrievedChunk":
    """Choose one duplicate-text representative and retain all provenance.

    Historical evidence always wins over a generated-question-only supplement;
    otherwise the higher-relevance representative wins and ties keep the
    existing stable position. The chosen mutable retrieval object receives the
    union of both support sets.
    """
    existing_optional = is_generated_question_only_chunk(existing)
    candidate_optional = is_generated_question_only_chunk(candidate)
    if existing_optional != candidate_optional:
        chosen = candidate if existing_optional else existing
    elif candidate.relevance > existing.relevance:
        chosen = candidate
    else:
        chosen = existing
    chosen.retrieval_supports = merge_retrieval_supports(
        existing.retrieval_supports, candidate.retrieval_supports
    )
    return chosen


def partition_generated_question_chunks(
    chunks: Sequence["RetrievedChunk"],
) -> tuple[List["RetrievedChunk"], List["RetrievedChunk"]]:
    """Split historical candidates from question-index-only supplements."""
    baseline: List["RetrievedChunk"] = []
    supplemental: List["RetrievedChunk"] = []
    for chunk in chunks:
        (supplemental if is_generated_question_only_chunk(chunk) else baseline).append(
            chunk
        )
    return baseline, supplemental


def select_with_reserves(
    ranked: Sequence["RetrievedChunk"],
    max_tokens: int,
    rules: Sequence[ReserveRule],
) -> List["RetrievedChunk"]:
    """Token truncation with bounded reserves, applied in the given order.

    The historical global-first oversize rule is preserved exactly: if the
    highest-ranked chunk alone exceeds the budget it remains the sole result.
    Reserve candidates may evict only lower-ranked direct candidates; they do
    not create a second oversize exception, and they never enlarge the budget.

    With several rules active, none of them may evict a chunk another one has
    actually PULLED IN — otherwise the second reserve would silently undo the
    first. That cross-rule protection covers only rescued chunks (bounded by
    each rule's quota, since a rule inserts at most ``reserve``): a naturally
    selected chunk that merely satisfies another rule's floor stays an
    ordinary evictable candidate, or a large exact section could silently
    defeat the graph floor without any rescue having happened. Within its own
    pass a rule still never evicts chunks it holds, exactly like the
    historical single-rule selector. A rule with ``reserve <= 0`` is inert in
    every respect, so enabling one reserve cannot change what another does
    when it is switched off.
    """
    ranked = list(ranked)
    selected = truncate_by_tokens(ranked, lambda chunk: chunk.text, max_tokens)
    active = [rule for rule in rules if rule.reserve > 0]
    if not active or not ranked or not selected:
        return selected
    first_tokens = est_tokens(selected[0].text)
    if first_tokens > max_tokens:
        return selected

    positions = {chunk.chunk_id: index for index, chunk in enumerate(ranked)}
    rescued: set[str] = set()

    for rule_index, rule in enumerate(active):
        completed = active[:rule_index]   # earlier rules: floors already settled
        already = sum(1 for chunk in selected if rule.holds(chunk))
        need = max(0, rule.reserve - already)
        if need == 0:
            continue
        selected_ids = {chunk.chunk_id for chunk in selected}
        inserted = 0
        for candidate in ranked:
            if inserted >= need:
                break
            if candidate.chunk_id in selected_ids or not rule.admits(candidate):
                continue
            candidate_tokens = est_tokens(candidate.text)
            if candidate_tokens > max_tokens:
                continue
            trial = list(selected)
            used = sum(est_tokens(chunk.text) for chunk in trial)
            # Rule order is priority order: a rule whose pass already COMPLETED
            # has settled its floor, and this rule may not undo it — protect
            # that rule's quota holders (its first `reserve` held chunks in
            # ranked order, natural or rescued). Protecting only `rescued` let
            # a later rule evict a naturally selected graph-only chunk and
            # silently break CHUNK_GRAPH_RESERVE (codex #399); protecting every
            # ACTIVE rule's holders is the opposite bug — a not-yet-run rule's
            # natural holders must stay evictable, because demands can exceed
            # the budget (exact reserve 4 + graph reserve 1 in 3 seats) and
            # that rule will rescue its own quota in its own pass afterwards.
            quota_held: set[str] = set()
            for other in completed:
                count = 0
                for chunk in trial:
                    if other.holds(chunk):
                        quota_held.add(chunk.chunk_id)
                        count += 1
                        if count >= other.reserve:
                            break
            removable = sorted(
                (chunk for chunk in trial[1:]
                 if chunk.chunk_id not in rescued
                 and chunk.chunk_id not in quota_held
                 and not rule.holds(chunk)),
                key=lambda chunk: positions.get(chunk.chunk_id, len(ranked)),
                reverse=True,
            )
            while used + candidate_tokens > max_tokens and removable:
                victim = removable.pop(0)
                trial.remove(victim)
                used -= est_tokens(victim.text)
            if used + candidate_tokens > max_tokens:
                continue
            trial.append(candidate)
            trial.sort(key=lambda chunk: positions.get(chunk.chunk_id, len(ranked)))
            selected = trial
            selected_ids.add(candidate.chunk_id)
            rescued.add(candidate.chunk_id)
            inserted += 1
    return selected


def select_with_reserves_baseline_first(
    ranked: Sequence["RetrievedChunk"],
    max_tokens: int,
    rules: Sequence[ReserveRule],
) -> List["RetrievedChunk"]:
    """Run historical token/reserve selection first, then spend leftovers.

    Generated-question retrieval is an optional low-recall supplement.  Its
    candidates therefore never enter the historical reserve competition and
    can neither evict nor reorder a candidate the same request would select
    with the feature disabled.
    """
    baseline, supplemental = partition_generated_question_chunks(ranked)
    if not baseline:
        return select_with_reserves(supplemental, max_tokens, rules)

    selected = select_with_reserves(baseline, max_tokens, rules)
    used = sum(est_tokens(chunk.text) for chunk in selected)
    if used >= max_tokens:
        return selected
    for candidate in supplemental:
        candidate_tokens = est_tokens(candidate.text)
        if used + candidate_tokens > max_tokens:
            continue
        selected.append(candidate)
        used += candidate_tokens
    return selected


def select_with_graph_reserve(
    ranked: Sequence["RetrievedChunk"],
    max_tokens: int,
    *,
    reserve: int = 1,
    min_graph_score: float = RELEVANCE_FLOOR,
) -> List["RetrievedChunk"]:
    """Token truncation with a bounded reserve for genuinely graph-only hits.

    Retained as the single-rule spelling even though the ask path now passes its
    rules explicitly: this is the shape the historical behaviour was specified
    in, and the suite that pins that behaviour still drives it. Keeping it is
    what makes "the generalisation changed nothing" a checked claim rather than
    an assertion.
    """
    return select_with_reserves(
        ranked,
        max_tokens,
        (graph_reserve_rule(reserve, min_graph_score=min_graph_score),),
    )


def score_chunks(
    query: str,
    chunks: List[dict],
    query_vector: Optional[List[float]] = None,
    chunk_sims: Optional[Dict[str, float]] = None,
    limit: int = 150,
) -> List[RetrievedChunk]:
    """Keyword + 可选语义(预算好的 chunk_sims)融合打分 chunk;大召回(默认
    top-150)。与 score_elements 同构,但作用于合并后的检索 chunk。"""
    basis = keyword_basis(query)
    scored: List[RetrievedChunk] = []
    for c in chunks:
        keyword = basis.coverage(set(_tokens(c["text"])), c["text"])
        semantic = 0.0
        has_vector = bool(
            query_vector
            and chunk_sims is not None
            and c["chunk_id"] in chunk_sims
        )
        if has_vector:
            semantic = chunk_sims.get(c["chunk_id"], 0.0)
        score = _fuse(keyword, semantic, has_vector)
        if score < RELEVANCE_FLOOR:
            continue
        scored.append(RetrievedChunk(
            chunk_id=c["chunk_id"], source_id=c["source_id"],
            source_title=c.get("source_title", ""), section_path=c.get("section_path", ""),
            text=c["text"], element_ids=c.get("element_ids", []),
            score=score, relevance=score,
        ))
    return rank_source_chunks(scored, limit)


def quota_fuse(collected, per_query, top_n, relevance=lambda h: h.relevance):
    """复合查询配额 round-robin。collected: {id: item}; per_query: List[{id: scored_item}]
    (第 i 个=第 i 个子查询的命中,scored_item 须有 relevance)。每个候选归到 relevance
    最高的子查询组,组内降序,跨组轮流取队首;全未命中归兜底组最后轮转。
    返回 (result, counts): counts[i]=第 i 子查询贡献数, counts[-1]=兜底组。"""
    groups = [[] for _ in per_query]
    fallback = []
    for oid, item in collected.items():
        best_i, best_h = -1, None
        for i, scored in enumerate(per_query):
            h = scored.get(oid)
            if h is not None and (best_h is None or relevance(h) > relevance(best_h)):
                best_i, best_h = i, h
        if best_i >= 0:
            groups[best_i].append(best_h)
        else:
            fallback.append(item)
    for g in groups:
        g.sort(key=relevance, reverse=True)
    queues = groups + [fallback]
    idx = [0] * len(queues)
    result, seen, sources = [], set(), []
    while len(result) < top_n:
        progressed = False
        for qi in range(len(queues)):
            if len(result) >= top_n:
                break
            while idx[qi] < len(queues[qi]):
                h = queues[qi][idx[qi]]; idx[qi] += 1
                oid = getattr(h, "object_id", None) or getattr(h, "chunk_id", None) or id(h)
                if oid not in seen:
                    seen.add(oid); result.append(h); sources.append(qi); progressed = True
                    break
        if not progressed:
            break
    counts = [sources.count(i) for i in range(len(queues))]
    return result, counts


def quota_fuse_baseline_first(
    collected, per_query, top_n, relevance=lambda h: h.relevance
):
    """Preserve historical quota-fuse output; fill only unused seats.

    Question-index-only candidates remain grouped by their originating query,
    but are considered only after the complete baseline round-robin result.
    """
    baseline = {
        oid: item for oid, item in collected.items()
        if not is_generated_question_only_chunk(item)
    }
    supplemental = {
        oid: item for oid, item in collected.items()
        if is_generated_question_only_chunk(item)
    }
    baseline_per_query = [
        {
            oid: item for oid, item in rows.items()
            if oid in baseline and not is_generated_question_only_chunk(item)
        }
        for rows in per_query
    ]
    selected, counts = quota_fuse(
        baseline, baseline_per_query, top_n, relevance=relevance
    )
    remaining = max(0, top_n - len(selected))
    if not remaining or not supplemental:
        return selected, counts
    supplemental_per_query = [
        {oid: item for oid, item in rows.items() if oid in supplemental}
        for rows in per_query
    ]
    extra, extra_counts = quota_fuse(
        supplemental, supplemental_per_query, remaining, relevance=relevance
    )
    return selected + extra, [
        count + extra_counts[index] for index, count in enumerate(counts)
    ]
