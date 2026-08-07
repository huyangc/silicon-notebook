"""Adapter: source raw text -> KG (nodes/edges/evidence) -> product knowledge.
The ONLY bridge between app.services.kg.* and the product. Extraction model is
the product LLM (deepseek-v4-flash via OPENAI_COMPAT_*)."""
from __future__ import annotations

import bisect
import concurrent.futures as cf
import math
import re
from typing import Any, List, Tuple

from app.services.kg.windowing import windows_with_elements
from app.services.kg.extract import extract_window
from app.services.kg.canonicalize import canonicalize
from app.services.kg.filters import should_extract_window, is_noise_concept, is_meta_claim
from app.services.kg.models import Edge, KnowledgeGraph, Node
from app.services.kg.run_control import KgBuildAborted
from app.services.kg.scheduler import submit_window

DOC_TYPE_MAP = {"academic_paper": "academic", "article": "academic", "textbook": "textbook"}


# ---------------------------------------------------------------------------
# Evidence binding helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _tokens(s: str) -> set:
    return set(re.findall(r"\w+", (s or "").lower()))


def _bind_quote(quote: str, elements, source_id: str, source_title: str) -> dict | None:
    """Return product-Evidence fields for the element that best contains `quote`.

    O(E) per call: re-normalizes/re-tokenizes every element's `.text` from
    scratch on every invocation. Kept verbatim (unused by production code,
    which now goes through `_QuoteBinder` below) as the semantic reference
    for `_QuoteBinder.bind` -- see the differential tests in
    test_kg_ingest.py, which run both over the same fixtures and assert
    byte-identical output. Do not change this function's behavior without
    updating that reference relationship; do not delete it while those
    tests still import it.
    """
    q = _norm(quote)
    if len(q) < 3:
        return None
    for el in elements:                       # exact substring on normalized text
        if q in _norm(el.text):
            return _ev(el, quote, source_id, source_title)
    qt = _tokens(quote)                        # CJK / fuzzy fallback: token overlap >= 0.6
    if qt:
        best, best_ov = None, 0.0
        for el in elements:
            et = _tokens(el.text)
            if not et:
                continue
            ov = len(qt & et) / len(qt)
            if ov > best_ov:
                best, best_ov = el, ov
        if best is not None and best_ov >= 0.6:
            return _ev(best, quote, source_id, source_title)
    return None


# Sentinel distinguishing "quote never looked up" from "looked up and the
# answer was None" in `_QuoteBinder._memo` -- a quote that legitimately
# fails to bind must still short-circuit on repeat lookups (models reuse the
# same quote across nodes/steps/edges routinely) instead of recomputing.
_UNBOUND = object()


class _QuoteBinder:
    """Per-source evidence index. Replaces `_bind_quote`'s O(quotes *
    elements * text_len) scan -- the whole point of this class -- with one
    O(total_normalized_text) build plus, per quote, O(memmem over the
    concatenated text) for the exact phase and O(sum of matching posting
    lists) for the fuzzy fallback, both independent of the element count E.

    This exists because a 21,104-element source (a single large manual) made
    `build_records` burn 1690s+ of single-core CPU re-normalizing/
    re-tokenizing every element for every quote in every node, step and edge
    -- production py-spy caught it live, and it was the only unindexed O(Q*E)
    scan left in the KG ingest path once candidate retrieval elsewhere was
    already indexed.

    Semantics are pinned to `_bind_quote`: same thresholds (`len(q) < 3`,
    fuzzy overlap `>= 0.6`), same exact-before-fuzzy priority, same
    first-element-wins tie-break for both phases. See test_kg_ingest.py's
    differential suite for the equivalence proof exercised against fixtures;
    the *argument* for why it holds is inline below, next to each piece it
    justifies.

    Memory: `_joined` holds the normalized text of every element in this
    source, concatenated once (source-document scale, not corpus scale);
    `_token_sets`/`_postings` are the same order of magnitude. All of it is
    local to one `build_records()` call -- built here, dropped when the
    binder goes out of scope -- and does not grow with notebook/base size,
    only with the size of the single source currently being ingested.
    """

    __slots__ = (
        "elements", "source_id", "source_title",
        "_joined", "_offsets", "_token_sets", "_postings", "_memo",
    )

    def __init__(self, elements, source_id: str, source_title: str) -> None:
        self.elements = elements
        self.source_id = source_id
        self.source_title = source_title

        # Each element's `.text` is read exactly ONCE here -- regardless of
        # how many quotes are later bound against this source (that
        # independence is the whole fix; test_kg_ingest.py pins it with a
        # counting-element guard). One local `text` feeds both `_norm` and
        # `_tokens`, so this is actually 1 read, not the "<=2" the class
        # docstring's contract allows for (2 would still satisfy the
        # invariant the guard test checks: reads bounded by a constant, not
        # by the number of quotes Q).
        norm_texts: List[str] = []
        token_sets: List[set] = []
        for el in elements:
            text = el.text
            norm_texts.append(_norm(text))
            token_sets.append(_tokens(text))
        self._token_sets = token_sets

        # Concatenate every element's normalized text with "\n" as the
        # segment separator, and record each segment's start offset.
        #
        # Equivalence argument (this is the load-bearing part -- MUT-5
        # exercises exactly this): `_norm` collapses every whitespace run
        # (including embedded newlines) down to a single ASCII space, so
        # NEITHER a normalized element text NOR a normalized query `q` can
        # ever contain "\n". A separator character guaranteed absent from
        # both sides of the comparison cannot itself be matched by `q`, so:
        #   1. No occurrence of `q` in the joined string can straddle a
        #      separator -- the character of `q` aligned with that position
        #      would have to be "\n", which never occurs in `q`. Every match
        #      therefore lies wholly inside exactly one segment.
        #   2. Segments appear in the joined string in the same order as
        #      `elements`, at strictly increasing start offsets. So the
        #      FIRST match position in the whole joined string is
        #      necessarily inside the FIRST segment that contains `q` at
        #      all -- any match in a later segment starts at a strictly
        #      larger offset than any match in an earlier one.
        # `str.find` returns exactly that first position, so mapping it back
        # to a segment reproduces `_bind_quote`'s `for el in elements: if q
        # in _norm(el.text): return ...` -- the first element (in list
        # order) whose normalized text contains `q` -- exactly, regardless
        # of where within that element the match falls (the old code never
        # looked at position, only membership).
        #
        # (Any character `_norm` output can never contain would work here;
        # "\n" is simplest and already guaranteed absent by construction.
        # The separator choice is not otherwise load-bearing.)
        offsets: List[int] = []
        pos = 0
        for nt in norm_texts:
            offsets.append(pos)
            pos += len(nt) + 1  # +1 for the "\n" joiner that follows
        self._offsets = offsets
        self._joined = "\n".join(norm_texts)

        # Inverted index for the fuzzy fallback: token -> element indices
        # containing it, built by iterating `elements` in order so every
        # posting list comes out already ascending by index. That ordering
        # is required below: the tie-break ("first index wins on equal
        # overlap") must reproduce `_bind_quote`'s `for el in elements` scan
        # with a strict `>` comparison, which keeps the first max it sees.
        postings: dict = {}
        for idx, toks in enumerate(token_sets):
            for t in toks:
                postings.setdefault(t, []).append(idx)
        self._postings = postings

        self._memo: dict = {}

    def bind(self, quote: str) -> dict | None:
        """Same contract as `_bind_quote(quote, self.elements, ...)`, memoized
        per raw (un-normalized) quote string -- models re-quote the same span
        across multiple nodes/steps/edges within one source routinely, and a
        repeated raw quote always normalizes and binds the same way."""
        cached = self._memo.get(quote, _UNBOUND)
        if cached is not _UNBOUND:
            return cached
        result = self._bind_uncached(quote)
        self._memo[quote] = result
        return result

    def _bind_uncached(self, quote: str) -> dict | None:
        q = _norm(quote)
        if len(q) < 3:
            return None

        idx = self._joined.find(q)
        if idx != -1:
            el_idx = bisect.bisect_right(self._offsets, idx) - 1
            return _ev(self.elements[el_idx], quote, self.source_id, self.source_title)

        qt = _tokens(quote)
        if not qt:
            return None
        # Candidates = union of postings for the query's own tokens, i.e.
        # every element sharing >=1 token with `qt`. An element sharing ZERO
        # tokens has ov == 0, which can never beat `_bind_quote`'s strict
        # `ov > best_ov` starting from best_ov = 0.0 either -- so omitting
        # zero-overlap elements from the candidate set (instead of visiting
        # and rejecting them, as `_bind_quote` does) cannot change which
        # element (if any) wins.
        candidates: set = set()
        for t in qt:
            candidates.update(self._postings.get(t, ()))
        if not candidates:
            return None
        best_idx, best_ov = None, 0.0
        for cand in sorted(candidates):   # ascending index == `elements` order
            et = self._token_sets[cand]
            ov = len(qt & et) / len(qt)
            if ov > best_ov:               # strict >: first max wins on ties,
                best_idx, best_ov = cand, ov  # i.e. lowest index, same as _bind_quote
        if best_idx is not None and best_ov >= 0.6:
            return _ev(self.elements[best_idx], quote, self.source_id, self.source_title)
        return None


def _ev(el, quote: str, source_id: str, source_title: str) -> dict:
    return {
        "source_id": source_id, "source_title": source_title, "element_id": el.id,
        "element_type": el.element_type, "location_label": el.location_label,
        "quoted_span": (quote or "")[:400], "confidence": 1.0,
    }


def build_records(graph: KnowledgeGraph, source_id: str, source_title: str,
                  elements) -> Tuple[List[dict], List[dict]]:
    """KG graph -> (objects, relations) with product evidence bound to elements.
    Nodes whose evidence binds to no element are dropped; edges referencing a
    dropped node are dropped. Each object dict carries `local_id` (= KG node id)
    so the caller can remap edges to DB ids after insert.

    Builds exactly one `_QuoteBinder` for `elements` (this function always
    runs once per source) and reuses it for every quote below -- node
    evidence, step evidence and edge evidence all share the same
    precomputed index instead of each re-scanning `elements`."""
    binder = _QuoteBinder(elements, source_id, source_title)
    kept: set = set()
    objects: List[dict] = []
    for node in graph.nodes:
        bound = []
        for ev in node.evidence:
            fields = binder.bind(ev.quote)
            if fields:
                bound.append(fields)
        if not bound:
            continue
        kept.add(node.id)
        payload = {"name": node.name, "section_path": node.section_path}
        if node.validity_scope:
            payload["validity_scope"] = node.validity_scope
        if node.steps:
            bound_steps = []
            for st in node.steps:
                quote = st.evidence[0].quote if st.evidence else ""
                fields = binder.bind(quote)
                if fields:
                    bound_steps.append({
                        "name": st.name,
                        "element_id": fields["element_id"],
                        "quote": fields["quoted_span"],
                    })
            if bound_steps:
                payload["steps"] = bound_steps
        objects.append({
            "local_id": node.id,
            "object_type": node.type.lower(),
            "payload": payload,
            "evidence": bound,
        })
    relations: List[dict] = []
    for edge in graph.edges:
        if edge.source_id in kept and edge.target_id in kept:
            # Keep the raw quote for graph verification/backward compatibility,
            # and bind it to the same SourceElement evidence shape used by nodes
            # whenever possible.  follow_chain can then expose each ORIGINAL hop
            # as an element-grounded relation anchor; old/unbound rows still
            # degrade to source-level quote evidence rather than being invented.
            edge_evidence = []
            for ev in edge.evidence:
                bound = binder.bind(ev.quote)
                if bound:
                    edge_evidence.append({"quote": ev.quote, **bound})
                elif (ev.quote or "").strip():
                    edge_evidence.append({"quote": ev.quote})
            relations.append({
                "source_local_id": edge.source_id,
                "target_local_id": edge.target_id,
                "edge_type": edge.type,
                "evidence": edge_evidence,
            })
    return objects, relations


def plan_window_size(content_chars: int, workers: int, w_min: int, w_max: int,
                     override: int = 0) -> int:
    """Balanced extraction window size (chars).

    override>0 forces a fixed size (back-compat / manual). Otherwise pick
    level = clamp(content_chars / workers, w_min, w_max), split into
    N = ceil(content_chars / level) windows, and return the BALANCED size
    ceil(content_chars / N) so windows are near-equal (no long-tail runt).

    NOTE: w_min/w_max bound the LEVEL (which sets the window count), not the
    returned size — the balanced result can be below w_min (e.g. 9000 chars
    -> level 4000 -> 3 windows -> 3000 each). That is intended.
    """
    if override > 0:
        return override
    if content_chars <= w_min:
        return max(1, content_chars)
    level = min(w_max, max(w_min, content_chars // max(1, workers)))
    n_windows = max(1, math.ceil(content_chars / level))
    return math.ceil(content_chars / n_windows)


def drop_noise_concepts(nodes: List[Node], edges: List[Edge],
                        whitelist) -> Tuple[List[Node], List[Edge], int]:
    """丢弃噪声 Concept 节点（白名单保护），并移除指向被丢节点的悬空边。
    仅对 Concept 生效；Claim/Formula/Procedure 一律保留。"""
    kept_ids = set()
    kept_nodes: List[Node] = []
    dropped = 0
    for nd in nodes:
        if nd.type == "Concept" and is_noise_concept(nd.name, whitelist)[0]:
            dropped += 1
            continue
        kept_ids.add(nd.id)
        kept_nodes.append(nd)
    kept_edges = [e for e in edges if e.source_id in kept_ids and e.target_id in kept_ids]
    return kept_nodes, kept_edges, dropped


def drop_meta_claims(nodes: List[Node], edges: List[Edge]) -> Tuple[List[Node], List[Edge], int]:
    """丢弃元叙述 Claim(讲文档自身的断言), 并移除悬空边。仅对 Claim 生效。"""
    kept_ids = set()
    kept_nodes: List[Node] = []
    dropped = 0
    for nd in nodes:
        if nd.type == "Claim" and is_meta_claim(nd.name)[0]:
            dropped += 1
            continue
        kept_ids.add(nd.id)
        kept_nodes.append(nd)
    kept_edges = [e for e in edges if e.source_id in kept_ids and e.target_id in kept_ids]
    return kept_nodes, kept_edges, dropped


def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, whitelist=frozenset(),
                  refine: bool = False, gleaning_rounds: int = 0,
                  base_filter: bool = False) -> KnowledgeGraph:
    """Window the text, extract a KG fragment per window concurrently, denoise,
    then canonicalize. 抽取前按 should_extract_window 跳过低价值窗口；抽取后按
    is_noise_concept 丢弃噪声 Concept（连带删悬空边）。Ungroundable nodes are
    dropped inside extract_window."""
    all_pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file,
                                                              None, n, m) if els]
    pairs = []
    windows_skipped = 0
    for w, els in all_pairs:
        keep, _reason = should_extract_window(w.section_path, els, doc_type)
        if keep:
            pairs.append((w, els))
        else:
            windows_skipped += 1
    # Resolve every workload-bound adapter before spawning window workers.
    # Plain legacy/test clients remain valid as a single-client shorthand.
    provider_chat = getattr(client, "chat", None)
    extract_client = provider_chat("kg_extract") if callable(provider_chat) else client
    refine_client = provider_chat("kg_refine") if callable(provider_chat) else client
    glean_client = provider_chat("kg_glean") if callable(provider_chat) else client
    nodes: List[Node] = []
    edges: List[Edge] = []
    failed = 0
    if pairs:
        futs = [submit_window(extract_window, extract_client, els, w.section_path,
                              doc_type, idx, refine=refine,
                              gleaning_rounds=gleaning_rounds,
                              base_filter=base_filter,
                              refine_client=refine_client,
                              glean_client=glean_client)
                for idx, (w, els) in enumerate(pairs)]
        # Production submit_window returns concurrent.futures.Future. Some
        # synchronous compatibility/test schedulers return a minimal
        # result()-only object; preserve that supported path.
        completed = (
            cf.as_completed(futs)
            if all(isinstance(fut, cf.Future) for fut in futs)
            else iter(futs)
        )
        def _cancel_and_drain_windows() -> None:
            for pending in futs:
                cancel = getattr(pending, "cancel", None)
                if cancel is not None:
                    cancel()
            real_futures = [
                pending for pending in futs
                if isinstance(pending, cf.Future)
            ]
            if real_futures:
                cf.wait(real_futures)

        for fut in completed:
            try:
                ns, es = fut.result()
                nodes += ns
                edges += es
            except KgBuildAborted:
                _cancel_and_drain_windows()
                raise
            except (KeyboardInterrupt, SystemExit):
                # 与上一支同一件事,只是中断继承 BaseException 接不到。不排空兄弟窗口的
                # 话,本来源的 future 会先完成,上层(_extract_targets)排空的是**来源**
                # future,于是可能在兄弟窗口仍在调模型、仍会写图时就落终态、放开跨进程
                # 单飞守卫,让新构建与它们重叠。排空必须逐层做。
                _cancel_and_drain_windows()
                raise
            except Exception:
                failed += 1
    nodes, edges, concepts_dropped = drop_noise_concepts(nodes, edges, whitelist)
    nodes, edges, claims_dropped = drop_meta_claims(nodes, edges)
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
    return KnowledgeGraph(doc_id=source_file, doc_type=doc_type, nodes=nodes,
                          edges=edges, total_windows=len(pairs),
                          failed_windows=failed, windows_skipped=windows_skipped,
                          concepts_dropped=concepts_dropped,
                          claims_dropped=claims_dropped)
