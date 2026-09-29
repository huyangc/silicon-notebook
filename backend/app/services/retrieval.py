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
    NamedTuple,
    Optional,
    Sequence,
    Set,
)

from app.core.query_syntax import quoted_phrases, unquoted_remainder
from app.domain.citation_origin import foreign_notebook_id
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
                 or the cited key came from DETERMINISTIC, non-ranked evidence
                 (``exact_evidence_keys``: a source-backed exact enumeration
                 row, or an external item a reflect plugin action brought back
                 — neither has a retrieval score to compare against tau),
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

    `alias_units` are the model-name spelling groups ("Cosmos 3" ↔ "cosmos3"):
    each is ONE unit of the basis, covered by max(compact alias present,
    fraction of its component runs present).  The max — not a replacement — is
    what keeps every word-plus-digit phrase at least as covered as its plain
    tokenization: "priority 1" still fully matches a document spelling it
    "priority: 1" through the `priority` component, while the alias arm alone
    lifts the cross-spelling case to full credit (codex #601 R1 P2).

    Built once per query and reused across a candidate pool: the parse and the
    query-side tokenization are per-query work, not per-candidate work.
    """

    tokens: FrozenSet[str]
    phrases: tuple[str, ...] = ()
    alias_units: tuple[tuple[str, FrozenSet[str]], ...] = ()

    def coverage(self, haystack_tokens: AbstractSet[str], haystack_text: str = "") -> float:
        """Covered fraction of the basis (0..1).

        `haystack_text` is only read when the query actually carries phrases, so
        callers holding a cached token set pay nothing for the parameter until a
        user asks for one.
        """
        total = len(self.tokens) + len(self.phrases) + len(self.alias_units)
        if not total:
            return 0.0
        hits: float = sum(1 for token in self.tokens if token in haystack_tokens)
        if self.phrases:
            haystack = _normalize(haystack_text)
            hits += sum(1 for phrase in self.phrases if phrase in haystack)
        for alias, components in self.alias_units:
            if alias in haystack_tokens:
                hits += 1
            elif components:
                hits += sum(
                    1 for part in components if part in haystack_tokens
                ) / len(components)
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
    # not two.  The alias and its component runs move out of the loose tokens
    # into ONE alias unit scored max(alias hit, component fraction) — see
    # KeywordBasis.  Only here: this function is never used to tokenize a
    # document, so haystacks keep their stems and a bare-stem query still
    # matches a spaced-name document.  A stopword stem ("in 3 days") keeps the
    # historical behavior: no unit, the stopword stays dropped.
    units: list[tuple[str, FrozenSet[str]]] = []
    seen_aliases: set[str] = set()
    for stem, suffix in spaced_model_name_parts(body):
        alias = f"{stem}{suffix}".lower()
        # A skipped stopword stem still sheds its _tokens-appended alias below,
        # so "in 3 days" scores exactly as it did before aliasing existed.
        if alias not in seen_aliases and stem.lower() not in _STOPWORDS:
            units.append((alias, frozenset(
                part.lower() for part in (stem, suffix) if len(part) > 1
            )))
        seen_aliases.add(alias)
    if seen_aliases:
        tokens -= seen_aliases
        tokens -= {part for _, components in units for part in components}
    return KeywordBasis(frozenset(tokens), phrases, tuple(units))


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


# 关系 embedding 输入里端点名的截断上限——协议边界的具名常量,live store_kg 与
# staged indexing 发布两条路共用,不许各写一份字面量(codex #602 R12 P2)。
RELATION_NAME_EMBED_CHARS = 80


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

    ``distinct_text``: a candidate whose ``text`` the selection already carries
    -- through ANY copy, another library's included -- is never pulled in.
    The library seats (``_first_copy_rule``) set it, so a seat is one piece of
    evidence, the same identity ``enforce_active_floor`` holds its spare
    candidates to; pulling in a second copy of a selected passage would evict
    a different passage for nothing.  Off (the historical behaviour) for the
    graph and exact rules.
    """

    reserve: int
    holds: Callable[["RetrievedChunk"], bool]
    admits: Callable[["RetrievedChunk"], bool]
    distinct_text: bool = False


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


def library_seats(reserve: int, available: Sequence[tuple]) -> List[tuple]:
    """``[(library, seats), ...]``: ``reserve`` seats shared fairly, no seat idle.

    ``available`` is ``[(library, candidates it has), ...]`` in priority order
    (``libraries_by_best_hit``).  Seats are dealt one at a time, round-robin in
    that order, passing over a library once it holds as many seats as it has
    candidates.  Unconstrained, that is the even split with the remainder going
    one each to the libraries at the front (with more libraries than seats,
    only the first ``reserve`` get one); a library with fewer candidates than
    its share hands the unused seats on, in the same order, to the libraries
    that still have candidates.  The seats therefore sum to
    ``min(reserve, total candidates)``.  Zero-seat libraries are omitted.
    """
    reserve = max(0, int(reserve))
    order: List[str] = []
    capacity: Dict[str, int] = {}
    for library, count in available:
        if library in capacity:
            continue
        order.append(library)
        capacity[library] = max(0, int(count))
    seats = dict.fromkeys(order, 0)
    remaining = min(reserve, sum(capacity.values()))
    while remaining > 0:
        for library in order:
            if remaining == 0:
                break
            if seats[library] < capacity[library]:
                seats[library] += 1
                remaining -= 1
    return [(library, seats[library]) for library in order if seats[library]]


def _chunk_library(chunk: "RetrievedChunk") -> str:
    return str(getattr(chunk, "notebook_id", "") or "")


def libraries_by_best_hit(
    chunks: Sequence["RetrievedChunk"],
    library_of: Callable[["RetrievedChunk"], str] = _chunk_library,
) -> List[tuple]:
    """``[(library, count), ...]`` in seat-priority order.

    The ONE priority order every per-library seat split uses (the exact
    section seats and the peer-mode per-library seats of
    ``library_reserve_rules``): libraries sorted by their best chunk's
    ``relevance``, descending, ties broken by the position of the library's
    first chunk in ``chunks``.  ``count`` is how many of ``chunks`` belong to
    it -- the most seats it can use.  ``library_of`` names a chunk's library;
    the default is its raw ``notebook_id`` stamp.
    """
    best: Dict[str, float] = {}
    count: Dict[str, int] = {}
    first: Dict[str, int] = {}
    for index, chunk in enumerate(chunks):
        library = library_of(chunk)
        score = float(getattr(chunk, "relevance", 0.0) or 0.0)
        if library not in first:
            first[library] = index
            best[library] = score
            count[library] = 0
        best[library] = max(best[library], score)
        count[library] += 1
    ordered = sorted(first, key=lambda library: (-best[library], first[library]))
    return [(library, count[library]) for library in ordered]


def exact_query_groups(
    exact_hits: Sequence["RetrievedChunk"],
) -> List[Dict[str, "RetrievedChunk"]]:
    """``chunk`` mode's ``multi`` per-query groups for the exact hits.

    Hits from one library (every single-notebook ask): exactly one group,
    ``[{chunk_id: hit, ...}]`` in hit order -- the historical shape.  Hits
    from several libraries (the peer-mode federated exact arm): one group per
    library, in ``libraries_by_best_hit`` order, so ``quota_fuse`` gives every
    library's exact section its own quota instead of one shared group.  No
    hits: no group.
    """
    hits = list(exact_hits)
    if not hits:
        return []
    available = libraries_by_best_hit(hits)
    if len(available) <= 1:
        return [{chunk.chunk_id: chunk for chunk in hits}]
    return [
        {
            chunk.chunk_id: chunk for chunk in hits
            if _chunk_library(chunk) == library
        }
        for library, _count in available
    ]


def select_by_library(
    chunks: Sequence["RetrievedChunk"], reserve: int,
) -> List["RetrievedChunk"]:
    """At most ``reserve`` of ``chunks``, shared per library, in input order.

    One library (every single-notebook ask): exactly ``chunks[:reserve]``.
    Several (the peer-mode federated exact arm): each library contributes its
    first ``library_seats`` share of ``chunks``, so one library's large
    section cannot take every seat, and no seat stays idle while another
    library still has candidates.
    """
    ordered = list(chunks)
    available = libraries_by_best_hit(ordered)
    if len(available) <= 1:
        return ordered[: max(0, int(reserve))]
    seats = dict(library_seats(reserve, available))
    used: Dict[str, int] = {}
    picked: List["RetrievedChunk"] = []
    for chunk in ordered:
        library = _chunk_library(chunk)
        if used.get(library, 0) < seats.get(library, 0):
            used[library] = used.get(library, 0) + 1
            picked.append(chunk)
    return picked


def exact_section_reserve_rules(
    reserve: int, exact_hits: Sequence["RetrievedChunk"],
) -> tuple:
    """The exact-section reserve, split per library when hits span several.

    Hits from ONE library (every single-notebook ask, whose hits carry no or
    one ``notebook_id``) give exactly ``(exact_section_reserve_rule(reserve,
    ids),)`` -- today's single rule over the same id set.  Hits from ``k > 1``
    libraries (the peer-mode federated exact arm) give one rule per library,
    the ``reserve`` seats shared by ``library_seats`` in
    ``libraries_by_best_hit`` order, so one library's large section cannot take
    every seat; a library with fewer hits than its share passes the rest on.
    The total stays ``reserve`` (or the hit count, when that is smaller).
    """
    hits = list(exact_hits)
    available = libraries_by_best_hit(hits)
    seats = library_seats(reserve, available)
    if len(available) <= 1 or not seats:
        return (exact_section_reserve_rule(
            reserve, {chunk.chunk_id for chunk in hits}),)
    return tuple(
        exact_section_reserve_rule(count, {
            chunk.chunk_id for chunk in hits
            if _chunk_library(chunk) == library
        })
        for library, count in seats
    )


# --- Library reserve seats (active notebook / peer-mode per library) --------
# One eligibility predicate and one seat-rule shape for every mechanical cut
# that guarantees a library a share of the final evidence: the MMR / quota
# floor (``enforce_active_floor``), the federated merge's withheld set
# (``chunk_federation._qualified_active``) and the mix branch's final cut
# (``mix_reserve_rules``).  Mechanical cuts only -- a model judgement (evidence
# refinement, outline binding) is never overridden by a seat.


def is_active_hit(hit, active_notebook_id: str) -> bool:
    """Whether ``hit`` belongs to the notebook this run answers for.

    Normalised through ``foreign_notebook_id`` rather than read as
    ``not hit.notebook_id``: federated chunk recall leaves the active leg
    unstamped, but the PPR lane and the generated-question hydrate stamp the
    RAW owning id, the active notebook's own included.  The id is REQUIRED,
    never defaulted: with ``""`` an own-id stamp reads as a peer, which both
    miscounts the floor and defeats every "is there any foreign row?" inert
    gate -- the single-notebook byte-identity guarantee.  Every production
    caller passes the run's real active id (pinned by
    ``test_active_notebook_id_threading``).
    """
    return not foreign_notebook_id(
        getattr(hit, "notebook_id", ""), active_notebook_id)


def reserve_eligible(hit) -> bool:
    """Whether ``hit`` may be PULLED INTO a reserved library seat.

    The library-agnostic half of the predicate.  Excluded: generated-question-
    only rows (an optional supplement never enters the reserve competition),
    graph-only rows (their admission belongs to ``graph_reserve_rule``, and
    the KG-overlay source lane's relevance is a constant 0.3, not a relevance
    signal), and rows below ``RELEVANCE_FLOOR`` -- unless the exact-identifier
    channel fetched them, whose keyword-only score measures the name that
    addressed the section rather than the question.
    """
    if is_generated_question_only_chunk(hit) or is_graph_only_chunk(hit):
        return False
    if is_exact_lookup_chunk(hit):
        return True
    return float(getattr(hit, "relevance", 0.0) or 0.0) >= RELEVANCE_FLOOR


def active_reserve_eligible(hit, active_notebook_id: str) -> bool:
    """``is_active_hit`` and ``reserve_eligible``: may take an active seat."""
    return is_active_hit(hit, active_notebook_id) and reserve_eligible(hit)


def _first_copy_rule(
    seats: int,
    rows: Sequence["RetrievedChunk"],
    eligible: Callable[["RetrievedChunk"], bool],
) -> ReserveRule:
    """A seat rule over one library's baseline ``rows`` (in ranked order).

    ``holds`` is the first-ranked copy of each distinct ``text`` among
    ``rows``, so identical text in two sources spends one seat -- the same
    one-seat-per-passage identity ``enforce_active_floor`` uses.  ``admits``
    is ``holds`` and ``eligible``: a weak or graph-only own row that the
    ranking already selected still counts toward the floor, but only an
    eligible one is pulled in.  ``distinct_text`` extends that identity to the
    WHOLE selection: a row whose text is already selected through another
    library's copy is skipped, exactly as the floor skips it.
    """
    first: Dict[str, str] = {}
    for chunk in rows:
        first.setdefault(chunk.text, chunk.chunk_id)
    held = frozenset(first.values())

    def _holds(chunk: "RetrievedChunk") -> bool:
        return chunk.chunk_id in held

    def _admits(chunk: "RetrievedChunk") -> bool:
        return chunk.chunk_id in held and eligible(chunk)

    return ReserveRule(reserve=max(0, int(seats)), holds=_holds, admits=_admits,
                       distinct_text=True)


def active_reserve_rule(
    seats: int,
    ranked: Sequence["RetrievedChunk"],
    active_notebook_id: str,
) -> ReserveRule:
    """The active notebook's seats in the mix branch's final cut.

    ``seats`` comes from ``chunk_federation.active_reserve_seats`` (0 in peer
    mode and when ``CHUNK_FEDERATION_ACTIVE_RESERVE=0``).  The rule is forced
    inert (``reserve=0``) when ``ranked`` holds no FOREIGN baseline row: with
    nothing to compete against, every seat is already the active notebook's,
    and an inert rule is invisible to ``select_with_reserves`` -- that is the
    byte-identity guarantee for single-notebook runs, whose PPR rows carry the
    active notebook's own id.  It must run LAST among the mix rules: rule
    order is priority order, so it may not evict what the graph and exact
    rules settled.  Like every reserve it never enlarges the budget, skips a
    candidate larger than the whole budget instead of creating a second
    oversized-top-1 exception, and stops when the eligible candidates run out.
    """
    baseline = [
        chunk for chunk in ranked if not is_generated_question_only_chunk(chunk)
    ]
    if not any(not is_active_hit(chunk, active_notebook_id) for chunk in baseline):
        seats = 0
    return _first_copy_rule(
        seats,
        [chunk for chunk in baseline if is_active_hit(chunk, active_notebook_id)],
        lambda chunk: active_reserve_eligible(chunk, active_notebook_id),
    )


def library_reserve_rules(
    seats: int,
    ranked: Sequence["RetrievedChunk"],
    active_notebook_id: str,
) -> tuple:
    """Peer-mode per-library seats in the mix branch's final cut.

    A peer (global) run has no subject library, so instead of one active
    floor every participant library with eligible evidence keeps a share:
    ``seats`` (``chunk_federation.peer_library_reserve_seats``, 0 outside
    peer mode) is split by ``library_seats`` in ``libraries_by_best_hit``
    order -- the split ``exact_section_reserve_rules`` uses -- over each
    library's eligible rows that are the FIRST copy of their text across the
    whole ranking (``reserve_eligible``), a library with fewer such rows than
    its share passing the rest on.  Whole-ranking identity, not per library:
    a row whose text a better-ranked copy in another library already carries
    can never fill a seat (``distinct_text`` skips it at selection time), so
    counting it as capacity would leave that seat idle instead of handing it
    to a library that still has candidates.  One ``_first_copy_rule`` per
    library, placed after the exact rules.

    Inert (``()``) with ``seats <= 0`` or when the baseline rows span a
    single library.  A row's library is its stamp, an empty stamp standing for
    ``active_notebook_id`` (the nominal active, whose KG-overlay source rows
    are normalised to ``""``).
    """
    baseline = [
        chunk for chunk in ranked if not is_generated_question_only_chunk(chunk)
    ]

    def _library(chunk: "RetrievedChunk") -> str:
        return _chunk_library(chunk) or str(active_notebook_id or "")

    if seats <= 0 or len({_library(chunk) for chunk in baseline}) <= 1:
        return ()
    rows: Dict[str, List["RetrievedChunk"]] = {}
    for chunk in baseline:
        rows.setdefault(_library(chunk), []).append(chunk)
    first: Dict[str, str] = {}
    for chunk in baseline:
        first.setdefault(chunk.text, chunk.chunk_id)
    eligible = [
        chunk for chunk in baseline
        if first[chunk.text] == chunk.chunk_id and reserve_eligible(chunk)
    ]
    return tuple(
        _first_copy_rule(count, rows[library], reserve_eligible)
        for library, count in library_seats(
            seats, libraries_by_best_hit(eligible, _library))
    )


def _rendered_chars(rows: Sequence["RetrievedChunk"], id_offset: int) -> int:
    """What ``EvidenceContextBuilder.chunk_context`` spends rendering ``rows``
    whole: one ``k{n}: `` prefix plus the text per row, one newline between."""
    return sum(
        len(f"k{index + id_offset}: ") + len(row.text or "")
        for index, row in enumerate(rows, 1)
    ) + max(0, len(rows) - 1)


def spare_reserved_rows(
    rows: Sequence["RetrievedChunk"],
    budget_chars: int,
    rule: ReserveRule,
    *,
    id_offset: int = 0,
) -> List["RetrievedChunk"]:
    """Make a character-budget render cut spare the rows holding a seat.

    ``chunk`` mode's MMR / quota floor (``enforce_active_floor``) swaps the
    active notebook's reserved rows into the TAIL positions of a finished
    selection, and the answer prompt renders that selection in order against
    ``CHUNK_ANSWER_BUDGET_CHARS`` -- so when the rows exceed the budget the
    first ones cut would be exactly the reserved ones.  ``rule`` is the same
    ``active_reserve_rule`` the mix cut uses; its first ``reserve`` holders in
    order are protected.  While the rows up to the last protected one do not
    fit, the lowest-ranked unprotected row before it is dropped (whole, never
    reordered).  Returns ``rows`` itself -- same order, same bytes -- when
    everything fits, when the rule is inert (no seats, or no foreign row), or
    when the protected rows already fit.
    """
    ordered = list(rows)
    if rule.reserve <= 0 or _rendered_chars(ordered, id_offset) <= budget_chars:
        return rows
    protected: Set[int] = set()
    for row in ordered:
        if len(protected) >= rule.reserve:
            break
        if rule.holds(row):
            protected.add(id(row))
    if not protected:
        return rows
    kept = list(ordered)
    while True:
        last = max(i for i, row in enumerate(kept) if id(row) in protected)
        if _rendered_chars(kept[:last + 1], id_offset) <= budget_chars:
            break
        droppable = [i for i in range(last) if id(kept[i]) not in protected]
        if not droppable:
            break
        del kept[droppable[-1]]
    return rows if len(kept) == len(ordered) else kept


def mix_reserve_rules(
    settings,
    ranked: Sequence["RetrievedChunk"],
    exact_hits: Sequence["RetrievedChunk"],
    active_notebook_id: str,
    *,
    active_seats: int,
    library_seats_total: int,
) -> tuple:
    """Every floor of the mix branch's final cut, in priority order.

    1. ``graph_reserve_rule(CHUNK_GRAPH_RESERVE)`` (default 0 / off);
    2. ``exact_section_reserve_rules(EXACT_SECTION_RESERVE, exact_hits)``,
       split per library when the hits span several;
    3. ``active_reserve_rule(active_seats, ...)`` -- the active notebook's
       share against mounted reference libraries;
    4. ``library_reserve_rules(library_seats_total, ...)`` -- peer mode's
       per-library shares.

    3 and 4 are mutually exclusive by construction (``active_seats`` is 0 in
    peer mode, ``library_seats_total`` is 0 outside it).  All share ONE token
    budget, a later rule never evicts what an earlier one settled, and a rule
    with no seats is inert -- see ``select_with_reserves`` -- so
    ``CHUNK_GRAPH_RESERVE=0`` keeps its historical behaviour byte-for-byte.
    Why the exact seats exist at all: the reranker is a general relevance
    model and routinely ranks an Arguments table below prose that merely talks
    about the command, so without a seat the exact section is assembled and
    then truncated away.  The library seats exist because all three mix lanes
    can come back owned by one strong library.
    """
    return (
        graph_reserve_rule(max(0, settings.chunk_graph_reserve)),
        *exact_section_reserve_rules(
            max(0, settings.exact_section_reserve), exact_hits),
        *library_floor_rules(
            ranked, active_notebook_id, active_seats=active_seats,
            library_seats_total=library_seats_total),
    )


def library_floor_rules(
    ranked: Sequence["RetrievedChunk"],
    active_notebook_id: str,
    *,
    active_seats: int,
    library_seats_total: int,
) -> tuple:
    """The library floors -- ``active_reserve_rule`` then
    ``library_reserve_rules`` -- shared by every mechanical cut that holds them.

    The mix branch's token cut (``mix_reserve_rules``) and the reasoning
    passage prefix (``reasoning_passage_order``: Ask synthesis, sectioned
    synthesis, deep-report drafting) build their floors HERE, so the seat
    count, the eligibility predicate, the first-copy-per-text identity, the
    peer-mode per-library split and the "no foreign row -> inert" gate cannot
    drift between them.  At most one of the two halves is non-empty.
    """
    return (
        active_reserve_rule(active_seats, ranked, active_notebook_id),
        *library_reserve_rules(library_seats_total, ranked, active_notebook_id),
    )


def promote_bounded_prefix_by_library(
    chunks: Sequence["RetrievedChunk"],
    holds: Callable[["RetrievedChunk"], bool],
    reserve: int,
) -> List["RetrievedChunk"]:
    """``promote_bounded_prefix`` with its ``reserve`` shared per library.

    When every chunk satisfying ``holds`` belongs to one library (every
    single-notebook ask) this IS ``promote_bounded_prefix``, item for item.
    When they span several (the peer-mode federated exact arm), the held
    chunks to promote are chosen by ``select_by_library`` (seats in
    ``libraries_by_best_hit`` order, unused seats passed on) -- still a stable
    reordering that drops nothing, and still at most ``reserve`` chunks.
    """
    ordered = list(chunks)
    held = [chunk for chunk in ordered if holds(chunk)]
    if len({_chunk_library(chunk) for chunk in held}) <= 1:
        return promote_bounded_prefix(ordered, holds, reserve)
    chosen = {id(chunk) for chunk in select_by_library(held, reserve)}
    if not chosen:
        return ordered
    return (
        [chunk for chunk in ordered if id(chunk) in chosen]
        + [chunk for chunk in ordered if id(chunk) not in chosen]
    )


def promote_bounded_prefix(
    chunks: Sequence["RetrievedChunk"],
    holds: Callable[["RetrievedChunk"], bool],
    reserve: int,
) -> List["RetrievedChunk"]:
    """Stably move at most ``reserve`` chunks satisfying ``holds`` to the front.

    A *reordering*, not a selection: the result is always a permutation of the
    input with every chunk still present, so the caller's downstream budget
    decides what actually survives. Promotion only changes WHICH end of the cut
    a chunk sits on, never how much fits.

    Stability is the whole contract. The promoted chunks keep their relative
    order, the remainder keeps its relative order, and nothing else moves — so
    a run with no match (or ``reserve <= 0``) returns a same-order copy and is
    byte-for-byte neutral for every consumer downstream.

    The ``reserve`` clamp is deliberate rather than "promote everything that
    holds": the exact channel can contribute up to 144 chunks in one reasoning
    run (3 sections x 12 chunks x 4 calls), and hoisting all of them would let
    one identifier's section evict every other retrieval lane from the
    synthesis context. Bounding the prefix buys the first ``reserve`` of them a
    seat and leaves the rest to compete on relevance like anything else.

    Distinct from ``select_with_reserves``, which this deliberately does not
    reuse: that one reserves seats measured in TOKENS against a ranked list
    produced by the reranker, and it drops chunks to make room. This one
    reorders a relevance-sorted list ahead of a CHARACTER budget and drops
    nothing.
    """
    ordered = list(chunks)
    if reserve <= 0:
        return ordered
    promoted: List["RetrievedChunk"] = []
    remainder: List["RetrievedChunk"] = []
    for chunk in ordered:
        if len(promoted) < reserve and holds(chunk):
            promoted.append(chunk)
        else:
            remainder.append(chunk)
    if not promoted:
        return ordered
    return promoted + remainder


def interleave_by_lane(
    ordered: Sequence["RetrievedChunk"],
    in_graph_lane: Callable[["RetrievedChunk"], bool],
    *,
    anchored: Optional[Callable[["RetrievedChunk"], bool]] = None,
) -> List["RetrievedChunk"]:
    """Stably split a relevance-sorted list into two lanes and alternate them.

    ``ordered`` is already sorted by relevance descending. Chunks for which
    ``anchored`` holds are set aside first (see below). Of the rest, the chunks
    for which ``in_graph_lane`` holds form one lane, every other chunk the other;
    each lane keeps its input order. The result takes one chunk from each lane in
    turn, starting with the lane that holds the first non-anchored chunk; once a
    lane runs out, the rest of the other lane follows as one block. If either
    lane is empty the result is a same-order copy of ``ordered`` -- anchored
    chunks included, byte-for-byte neutral downstream. That is what keeps a run
    with no PPR passage (every no-graph run) and a graph run with no retrieved
    passage (the passage-search switch off) exactly on the pre-interleave order.

    Anchored chunks (reasoning passes the exact-lookup passages) do not take
    part in the alternation. Each goes back in front of the first interleaved
    chunk whose relevance is STRICTLY lower than its own; the anchored block is
    already descending, so insertion points never move backwards, and a tie
    keeps the interleaved chunk first -- as the stable sort did, where a 1.0
    PPR passage fetched earlier sat ahead of a 1.0 exact passage fetched later.
    Why: the exact prefix seat (``promote_bounded_prefix_by_library``) only
    protects the first ``reserve`` passages of a named section; the rest of the
    section (a 10-chunk command page against a reserve of 4) has always stayed
    whole because relevance 1.0 sorts it to the front. Alternating those
    passages with the PPR lane would scatter them past the tail of the PPR lane,
    and an overview budget would then cut the Arguments table the channel
    exists to deliver.

    Why: a reasoning run's passage partition mixes two relevance scales that
    cannot be compared. Concept-walk (PPR) passages carry a PER-RUN min-max
    normalized score -- the top one is always exactly 1.0 and the tail decays
    relative to it -- while seeded / searched passages carry an ABSOLUTE fused
    score (0.4 keyword coverage + 0.6 cosine). Measured on eight graph-bearing
    questions (2026-09-29, local corpus): PPR rank 1 was 1.0 in 8/8, the rank-2
    median 0.945, rank-6 median 0.62; seeded passages had median 0.36 and max
    0.78. Sorting the union on one relevance key therefore lets the PPR lane
    systematically evict the retrieval lane: under the overview budget (12k
    characters) 3 of the 8 questions kept at most one seeded passage -- none at
    all, or only the budget-truncated last line -- and under the standard budget
    one question kept 1 of its 8. Interleaved, every one of those runs admits
    its whole seeded pool.

    This is a REORDERING of what the character budget sees, never a rewrite of
    ``relevance``: that value still feeds the grounding thresholds
    (``EVIDENCE_TAU_*``) and ``top_relevance``, where rescaling one lane would
    silently change what counts as grounded. Nothing is dropped. It is the same
    idea as chunk mode's mix branch, which round-robins its producers instead of
    ranking them on one incomparable score.
    """
    items = list(ordered)
    held = [chunk for chunk in items if anchored is not None and anchored(chunk)]
    held_ids = {id(chunk) for chunk in held}
    free = [chunk for chunk in items if id(chunk) not in held_ids]
    graph = [chunk for chunk in free if in_graph_lane(chunk)]
    if not graph or len(graph) == len(free):
        return items
    graph_ids = {id(chunk) for chunk in graph}
    other = [chunk for chunk in free if id(chunk) not in graph_ids]
    first, second = (graph, other) if id(free[0]) in graph_ids else (other, graph)
    mixed: List["RetrievedChunk"] = []
    for index in range(max(len(first), len(second))):
        if index < len(first):
            mixed.append(first[index])
        if index < len(second):
            mixed.append(second[index])
    result: List["RetrievedChunk"] = []
    position = 0
    for chunk in held:
        own = getattr(chunk, "relevance", 0.0)
        while position < len(mixed) and not (
            getattr(mixed[position], "relevance", 0.0) < own
        ):
            result.append(mixed[position])
            position += 1
        result.append(chunk)
    return result + mixed[position:]


def is_exact_lookup_chunk(chunk: "RetrievedChunk") -> bool:
    """Whether a passage came through the exact-identifier channel."""
    return bool(getattr(chunk, "exact_lookup", False))


# Characters one reserved passage is allowed beyond its own text in the
# reasoning chunk segment.  ``chunk_context`` renders each passage as
# ``kN: `` plus its text, one newline between lines, and the segment carries
# one ``[Retrieved chunks]`` heading -- well under 20 characters per passage.
# 120 is a deliberate allowance on top of that (it also absorbs the joiners
# between the structured blocks): over-estimating only hands the structured
# side a few hundred characters fewer, under-estimating is what would let a
# reserved passage fall off the end of the partition.
PASSAGE_FLOOR_LINE_CHARS = 120


class ReasoningPassageOrder(NamedTuple):
    """``reasoning_passage_order``'s result: the order, and its seat prefix.

    ``passages`` is the full reordered partition (nothing dropped).
    ``prefix`` counts the passages at its front that hold a reserved seat --
    the exact prefix plus the active / per-library prefix -- which is what the
    passage floor (``passage_floor``) protects from the structured side.
    """

    passages: List["RetrievedChunk"]
    prefix: int


def passage_floor(
    prefix: Sequence["RetrievedChunk"], partition_chars: int,
) -> int:
    """Characters of a reasoning source partition held back for ``prefix``.

    ``min(partition // 2, sum(len(text) + PASSAGE_FLOOR_LINE_CHARS))`` over
    the reserved-seat passages -- the exact prefix AND the active /
    per-library prefix (J6).  The structured side (Knowhow preview, the
    enumeration sub-budget, document reads, the spreadsheet block) renders
    against ``partition - floor``, so a full structured block can no longer
    leave the passage segment zero characters and void the seats; the half
    cap keeps the structured side's own share.  ``0`` with no reserved seat
    (no passage, no exact hit, a single-notebook run), so those runs render
    byte-for-byte as before.
    """
    if not prefix:
        return 0
    return min(
        max(0, int(partition_chars)) // 2,
        sum(len(chunk.text or "") + PASSAGE_FLOOR_LINE_CHARS for chunk in prefix),
    )


def promote_library_prefix(
    ordered: Sequence["RetrievedChunk"],
    prefix: int,
    rules: Sequence[ReserveRule],
    held: Sequence["RetrievedChunk"] = (),
) -> ReasoningPassageOrder:
    """Stably move each library floor's missing seats behind ``prefix``.

    For every rule (``library_floor_rules``, priority order): the seats it
    still needs are ``reserve`` minus the passages it already ``holds`` among
    ``held`` (passages rendered ahead of ``ordered``, e.g. a report section's
    outline-bound passages) and the first ``prefix`` of ``ordered`` (the exact
    prefix), so an active passage already in the exact prefix spends a seat.
    That many ``admits`` passages from the remainder move, in their current
    order, to directly behind the prefix -- skipping, for a ``distinct_text``
    rule, a passage whose text is already ahead of it (``held``, the prefix or
    an earlier pick, any library's copy): one seat is one piece of evidence.
    A reordering only: nothing is dropped, and a rule with no seats (or none
    missing) moves nothing.
    """
    head, tail = list(ordered[:prefix]), list(ordered[prefix:])
    counted = [*held, *head]
    ahead = {chunk.text for chunk in counted}
    chosen: Set[int] = set()
    for rule in rules:
        need = rule.reserve - sum(1 for chunk in counted if rule.holds(chunk))
        for chunk in tail:
            if need <= 0:
                break
            if id(chunk) in chosen or not rule.admits(chunk):
                continue
            if rule.distinct_text and chunk.text in ahead:
                continue
            chosen.add(id(chunk))
            ahead.add(chunk.text)
            need -= 1
    picked = [chunk for chunk in tail if id(chunk) in chosen]
    rest = [chunk for chunk in tail if id(chunk) not in chosen]
    return ReasoningPassageOrder(head + picked + rest, len(head) + len(picked))


def reasoning_passage_order(
    chunks: Sequence["RetrievedChunk"],
    *,
    exact_reserve: int,
    active_reserve: int,
    library_reserve: int,
    active_notebook_id: str,
    held: Sequence["RetrievedChunk"] = (),
) -> ReasoningPassageOrder:
    """The order a reasoning passage partition is rendered in, against a
    character budget, plus how many passages at its front hold a reserved
    seat. One definition for every consumer: Ask synthesis
    (``AskService._answer_reasoning``, which the sectioned path reuses per
    section over that section's bound passages only) and deep-report section
    drafting (``ReportEngine._section_passage_order``, for the passages no
    outline item bound).  Production callers reach it through
    ``chunk_federation.reasoning_order_for``, which reads the seat counts
    from settings.

    Four steps, and the order carries weight:

    1. relevance descending, stable -- ties keep insertion order (retrieval
       order / document order inside an exact section) instead of a random
       128-bit chunk id deciding who makes the budget;
    2. ``interleave_by_lane`` -- concept-walk passages (per-run normalized
       relevance, ``relevance_on_ppr_scale``) alternate with retrieved passages
       (absolute fused relevance), exact passages anchored at their relevance
       position;
    3. ``promote_bounded_prefix_by_library`` -- the first ``exact_reserve``
       exact passages move to the very front (seats split per library in a
       peer run), so exact passages keep the highest priority; running the
       interleave after it would scatter the promoted block again;
    4. ``promote_library_prefix`` over ``library_floor_rules`` -- directly
       behind the exact prefix, at most ``active_reserve`` eligible active
       passages (``active_reserve_eligible``, first copy per text, current
       order) minus those the exact prefix (and ``held``) already holds; in a
       peer run, where there is no subject library, ``library_reserve`` seats
       shared per library exactly as the mix cut shares them
       (``library_reserve_rules``: best-hit order, first copy per text).  The
       active floor is inert when no FOREIGN passage exists -- which is why
       the real ``active_notebook_id`` must be passed: reasoning's PPR
       passages carry the active notebook's OWN id -- and the per-library
       floor with a single library, so a single-notebook run and a
       one-participant peer run come out exactly as after step 3.

    A reordering only: nothing is dropped, ``relevance`` is never rewritten.
    Mechanical cut only (J4): a model judgement -- evidence refinement,
    outline binding -- is never overridden, so ``held`` passages stay where
    their caller put them and are only counted.
    """
    ordered = sorted(chunks, key=lambda chunk: -chunk.relevance)
    ordered = interleave_by_lane(
        ordered, relevance_on_ppr_scale, anchored=is_exact_lookup_chunk)
    ordered = promote_bounded_prefix_by_library(
        ordered, is_exact_lookup_chunk, exact_reserve)
    # The promoted exact block is exactly the first min(reserve, exact count)
    # passages: ``library_seats`` hands out min(reserve, candidates) seats.
    exact = min(max(0, int(exact_reserve)),
                sum(1 for chunk in ordered if is_exact_lookup_chunk(chunk)))
    rules = library_floor_rules(
        [*held, *ordered], active_notebook_id,
        active_seats=active_reserve, library_seats_total=library_reserve)
    return promote_library_prefix(ordered, exact, rules, held)


def relevance_on_ppr_scale(chunk: "RetrievedChunk") -> bool:
    """Whether ``chunk.relevance`` is a PPR (per-run min-max normalized) value.

    The lane is decided by the SCALE of the relevance the chunk carries, not by
    "PPR ever touched it". Every PPR producer sets ``relevance`` to the very
    value it stores as its ``RetrievalSupport(origin="ppr").score``
    (``graph_retrieval._ppr_retrieve``, ``source_partitioned_ppr``,
    ``source_subgraph_ppr`` / ``source_graph_activation``). When reasoning
    dedups a passage that two producers returned (``take_distinct_chunk_hits``
    -> ``prefer_stronger_chunk_candidate``), the HIGHER-relevance representative
    is kept and the support sets are unioned, with a repeated ppr support
    keeping the max score. So a passage PPR brought back at 0.9 and a seed later
    hit at 0.45 keeps the PPR object (relevance 0.9 == its ppr score): graph
    lane. The reverse -- PPR at 0.3, seed at 0.45 -- keeps the seed object: its
    supports now include a ppr entry, but its relevance (0.45) is an absolute
    fused score, not that ppr score, so it belongs to the retrieval lane.
    Exact float equality is sound because the two numbers are copies of one
    value, never recomputed. ``getattr`` keeps duck-typed test doubles working.
    """
    relevance = getattr(chunk, "relevance", None)
    return any(
        getattr(support, "origin", "") == "ppr"
        and getattr(support, "score", None) is not None
        and support.score == relevance
        for support in (getattr(chunk, "retrieval_supports", ()) or ())
    )


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
    # Union, not overwrite: an exact-channel hit colliding with a semantic hit
    # on the same content key must keep its `exact_lookup` marker even when
    # the semantic representative is the one kept in place.
    chosen.exact_lookup = existing.exact_lookup or candidate.exact_lookup
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
            if rule.distinct_text and any(
                    chunk.text == candidate.text for chunk in selected):
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
    collected, per_query, top_n, relevance=lambda h: h.relevance, *,
    active_notebook_id: str,
):
    """Preserve historical quota-fuse output; fill only unused seats.

    Question-index-only candidates remain grouped by their originating query,
    but are considered only after the complete baseline round-robin result.

    ``collected.active_reserve`` -- an optional attribute a federated candidate
    mapping may carry (``chunk_federation.FederatedCollected``).  When present
    and non-zero, ``enforce_active_floor`` runs on the FINISHED fusion, which is
    the only place a floor on the active notebook's share can actually hold:
    the caller is free to merge further direct-hit producers into ``collected``
    and to append their own quota groups before calling this, so any rule
    expressed in the GROUPS is only a rule about the groups this function was
    handed.  A plain ``dict`` -- every single-library caller, every test double
    -- has no such attribute and takes the historical path unchanged.
    ``active_notebook_id`` is the run's real active notebook, handed to the
    floor so an own-id-stamped row counts as the active notebook's.

    ``counts`` is the fusion's own per-group accounting and is deliberately NOT
    rewritten by that floor: a floor substitution replaces one already-selected
    row with another, so attributing it to a group would invent a contribution
    the round-robin never made.  The one production caller (``ask_chunk``'s
    multi branch) discards ``counts``; ``reasoning_retrieval`` reports its own
    per-sub-query quota from its own fuse call, which carries no reserve.
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
    if remaining and supplemental:
        supplemental_per_query = [
            {oid: item for oid, item in rows.items() if oid in supplemental}
            for rows in per_query
        ]
        extra, extra_counts = quota_fuse(
            supplemental, supplemental_per_query, remaining, relevance=relevance
        )
        selected = selected + extra
        counts = [
            count + extra_counts[index] for index, count in enumerate(counts)
        ]
    return enforce_active_floor(
        selected,
        collected.values(),
        int(getattr(collected, "active_reserve", 0) or 0),
        relevance=relevance,
        active_notebook_id=active_notebook_id,
    ), counts


def enforce_active_floor(
    selected, candidates, floor, relevance=lambda h: h.relevance, *,
    active_notebook_id: str,
):
    """Guarantee the active notebook a minimum share of a FINISHED selection.

    Pure, deterministic, settings-free: the caller supplies ``floor``.  Both
    chunk selection branches end here -- the multi branch through
    ``quota_fuse_baseline_first`` and the single branch through
    ``RetrievalService.select_chunk_candidates`` after MMR -- so the rule has
    exactly one implementation and one place it can be defeated from, which is
    nowhere: by construction it runs on the list that is about to become the
    answer's evidence.

    "Active" is ``is_active_hit(hit, active_notebook_id)``.  Federated recall
    stamps only peer libraries' hits, so an empty origin means the notebook
    being asked about -- the same reading ``evidence_context``,
    ``source_scope`` and ``citation_origin`` already have -- and the REQUIRED
    real active id makes a raw own-id stamp (PPR lane, generated-question
    hydrate, third-party contributor rows) read as active too instead of
    posing as a peer and defeating the inert gate below.  A seat already held
    counts any active baseline row;
    a spare candidate must also pass ``active_reserve_eligible`` (not graph-
    only, relevance at least ``RELEVANCE_FLOOR`` unless exact-lookup) -- the
    one predicate the mix branch's ``active_reserve_rule`` uses.

    Inert, returning the caller's own list object, whenever the floor cannot be
    at stake: ``floor <= 0`` (the feature off), an empty selection, or a
    selection with no peer hit at all (single-library retrieval, or a federated
    ask the active notebook already dominates).  That is what keeps the
    single-library path byte-identical.

    Otherwise the missing seats are taken from the active notebook's strongest
    BASELINE candidates that the selection passed over, and they replace PEER
    rows from the tail inwards -- generated-question-only peer rows first,
    because an optional supplement is the cheapest thing in the list to give
    up, then the lowest-ranked peer evidence.  An active row is never replaced,
    every surviving row keeps its position, and ``len(selected)`` is unchanged:
    the reserve moves WHOSE evidence fills a seat, never how many seats exist.

    Candidates are de-duplicated by ``hit.text`` -- the identity
    ``global_evidence.peer_evidence`` uses -- against each other AND against
    what is already selected.  Without it, two active sources holding the same
    passage (``_fold_library_pool`` keeps both, because its identity includes
    ``source_id``) would spend the reserve twice on one piece of evidence.
    """
    rows = list(selected)

    def _active(hit) -> bool:
        return is_active_hit(hit, active_notebook_id)

    if floor <= 0 or not rows or all(_active(row) for row in rows):
        return selected
    # Seats already held are counted per DISTINCT passage, the same one-seat-
    # per-text contract the spare candidates below are held to.  A direct
    # (keyword / exact-lookup) hit can reintroduce a passage from a second local
    # source -- ``_merge_multi_direct_chunk_hits`` keeps both copies because its
    # identity includes ``source_id`` -- and counting each copy would declare
    # the floor met by four renderings of one piece of evidence.
    local = {
        row.text for row in rows
        if _active(row) and not is_generated_question_only_chunk(row)
    }
    seen_text = {row.text for row in rows}
    seen_ids = {row.chunk_id for row in rows}
    spare: List["RetrievedChunk"] = []
    for candidate in sorted(
        (
            item for item in candidates
            if active_reserve_eligible(item, active_notebook_id)
            and item.chunk_id not in seen_ids
        ),
        key=lambda item: -float(relevance(item) or 0.0),
    ):
        if candidate.text in seen_text:
            continue
        seen_text.add(candidate.text)
        spare.append(candidate)
    need = min(int(floor), len(local) + len(spare)) - len(local)
    if need <= 0 or not spare:
        return selected
    tail = range(len(rows) - 1, -1, -1)
    victims = [
        index for index in tail
        if not _active(rows[index])
        and is_generated_question_only_chunk(rows[index])
    ] + [
        index for index in tail
        if not _active(rows[index])
        and not is_generated_question_only_chunk(rows[index])
    ]
    if not victims:
        return selected
    for index, candidate in zip(victims[:need], spare):
        rows[index] = candidate
    return rows
