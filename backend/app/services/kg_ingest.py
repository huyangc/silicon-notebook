"""Adapter: source raw text -> KG (nodes/edges/evidence) -> product knowledge.
The ONLY bridge between app.services.kg.* and the product. Extraction model is
the product LLM (deepseek-v4-flash via OPENAI_COMPAT_*)."""
from __future__ import annotations

import bisect
import concurrent.futures as cf
import math
import re
from typing import Any, List, Tuple

from app.core.llm import cap_kwargs
from app.domain.indexing_pipeline import (
    IndexingKgEdgeProposal,
    IndexingKgFragment,
    IndexingKgMessage,
    IndexingKgObjectProposal,
    IndexingKgPrompt,
    IndexingKgStepProposal,
    IndexingPipelineKgLimits,
)
from app.services.kg.windowing import windows_with_elements
from app.services.kg.extract import extract_window
from app.services.kg.extract import _parse_validity_scope
from app.services.kg.canonicalize import canonicalize
from app.services.kg.filters import should_extract_window, is_noise_concept, is_meta_claim
from app.services.kg.models import Edge, Evidence, KnowledgeGraph, Node, Step
from app.services.kg.run_control import KgBuildAborted
from app.services.kg.scheduler import submit_window
from app.services.kg.edge_schema import VALID_EDGE_TYPES, is_queryable_edge_pair

DOC_TYPE_MAP = {"academic_paper": "academic", "article": "academic", "textbook": "textbook"}
_CORE_PLUGIN_NODE_TYPES = {
    "concept": "Concept",
    "claim": "Claim",
    "formula": "Formula",
    "procedure": "Procedure",
}


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
    elements * text_len) Python-level rescan -- the whole point of this
    class -- with a one-time O(total_normalized_text) build, plus per quote:
    an O(len(joined text)) C-speed `str.find` for the exact phase (its cost
    grows with the source's total text length, same as the build itself, but
    it is a single C-speed scan, not a Python loop re-normalizing every
    element), and for the fuzzy fallback, worst case O(candidates) <= O(E)
    dict increments sourced from a prebuilt inverted index (C-speed dict
    lookup-and-increment per candidate, not a Python-level re-tokenize-and-
    intersect per element). What this rewrite eliminates is the Q*E
    Python-level recomputation, not big-O independence from the element
    count E -- see the differential tests in test_kg_ingest.py for the
    equivalence proof exercised against fixtures; the *argument* for why it
    holds is inline below, next to each piece it justifies.

    This exists because a 21,104-element source (a single large manual) made
    `build_records` burn 1690s+ of single-core CPU re-normalizing/
    re-tokenizing every element for every quote in every node, step and edge
    -- production py-spy caught it live, and it was the only unindexed O(Q*E)
    scan left in the KG ingest path once candidate retrieval elsewhere was
    already indexed.

    Semantics are pinned to `_bind_quote`: same thresholds (`len(q) < 3`,
    fuzzy overlap `>= 0.6`), same exact-before-fuzzy priority, same
    first-element-wins tie-break for both phases.

    Memory: `_joined` holds the normalized text of every element in this
    source, concatenated once -- the same order of magnitude as the
    source's raw text (source-document scale, not corpus scale). The only
    other structure retained past construction is `_postings` (the fuzzy
    fallback's inverted index); it no longer keeps a full token SET per
    element (`_token_sets` was removed -- see the counting rewrite in
    `_bind_uncached`). Measured on a ~30MB source: binder resident memory
    dropped from ~438MB (with `_token_sets` also retained) to ~99MB
    (postings only) after this change. All of it is local to one
    `build_records()` call -- built here, dropped when the binder goes out
    of scope -- and does not grow with notebook/base size, only with the
    size of the single source currently being ingested.
    """

    __slots__ = (
        "elements", "source_id", "source_title",
        "_joined", "_offsets", "_postings", "_memo",
    )

    def __init__(self, elements, source_id: str, source_title: str) -> None:
        self.elements = elements
        self.source_id = source_id
        self.source_title = source_title

        # Each element's `.text` is read exactly ONCE here -- regardless of
        # how many quotes are later bound against this source (that
        # independence is the whole fix; test_kg_ingest.py pins it with a
        # counting-element guard). One local `text` feeds both `_norm` and
        # `_tokens` below, so this is 1 read per element (a future change
        # that reads `text` a second time here would still satisfy the
        # regression guard, which only bounds reads by a small constant,
        # not by the number of quotes Q).
        #
        # Memory-friendly construction order (measured -- see the class
        # docstring's memory paragraph): normalize each element and
        # immediately feed BOTH the offset table (this loop) and the
        # postings index (below) from that single normalized/tokenized
        # pass -- there is no second pass re-walking a materialized
        # `norm_texts` list before offsets are known, and no per-element
        # token SET is retained anywhere (the fuzzy fallback in
        # `_bind_uncached` counts postings instead of intersecting stored
        # token sets). `norm_texts` itself is only needed transiently, to
        # be joined once below, and is dropped immediately after.
        norm_texts: List[str] = []
        offsets: List[int] = []
        postings: dict = {}
        pos = 0
        for idx, el in enumerate(elements):
            text = el.text
            nt = _norm(text)
            norm_texts.append(nt)
            offsets.append(pos)
            pos += len(nt) + 1  # +1 for the "\n" joiner that follows

            # Inverted index for the fuzzy fallback: token -> element
            # indices containing it. Iterating `elements` in idx order here
            # means every posting list comes out already ascending by
            # index -- required below: the tie-break ("first index wins on
            # equal overlap") must reproduce `_bind_quote`'s
            # `for el in elements` scan with a strict `>` comparison, which
            # keeps the first max it sees.
            for t in _tokens(text):
                postings.setdefault(t, []).append(idx)
        self._offsets = offsets
        self._postings = postings

        # Concatenate every element's normalized text with "\n" as the
        # segment separator, then drop the transient list.
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
        self._joined = "\n".join(norm_texts)
        del norm_texts

        self._memo: dict = {}

    def bind(self, quote: str) -> dict | None:
        """Same contract as `_bind_quote(quote, self.elements, ...)`, memoized
        per raw (un-normalized) quote string -- models re-quote the same span
        across multiple nodes/steps/edges within one source routinely, and a
        repeated raw quote always normalizes and binds the same way.

        Returns a fresh, independent dict on every call, cache hit or miss:
        the memo keeps its own canonical dict per quote and callers get a
        shallow copy of it, so two callers binding the same quote never end
        up sharing (and accidentally mutating) the same evidence dict."""
        cached = self._memo.get(quote, _UNBOUND)
        if cached is not _UNBOUND:
            return dict(cached) if cached is not None else None
        result = self._bind_uncached(quote)
        self._memo[quote] = result
        return dict(result) if result is not None else None

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
        # Tally, per candidate element, how many of the query's OWN tokens
        # it shares -- `counts[cand]` ends up EXACTLY `len(qt & et)` for
        # that element's token set `et`, without ever materializing `et`:
        # every token `t` in `qt` contributes +1 to every element in
        # `postings[t]`, i.e. every element whose token set contains `t`.
        # Summed over all `t` in `qt`, that is
        # `sum(1 for t in qt if t in et) == len(qt & et)`. Dividing by
        # `len(qt)` below therefore reproduces `_bind_quote`'s
        # `ov = len(qt & et) / len(qt)` exactly -- this replaces the
        # per-candidate set intersection (both `_bind_quote`'s and this
        # method's former `_token_sets`-based version) with cheap dict
        # increments driven off the prebuilt postings.
        #
        # An element sharing ZERO tokens with `qt` never appears in any
        # `postings[t]` for `t` in `qt`, so it never enters `counts` at
        # all -- it has ov == 0, which can never beat `_bind_quote`'s
        # strict `ov > best_ov` starting from `best_ov = 0.0` either, so
        # omitting it (instead of visiting and rejecting it, as
        # `_bind_quote` does) cannot change which element (if any) wins.
        counts: dict = {}
        for t in qt:
            for cand in self._postings.get(t, ()):
                counts[cand] = counts.get(cand, 0) + 1
        if not counts:
            return None
        best_idx, best_ov = None, 0.0
        # `sorted()` here is load-bearing, not cosmetic: `counts`' own
        # insertion order follows the order `qt` (a `set` of strings) is
        # iterated, which is NOT reliably ascending by element index --
        # see test_quote_binder_fuzzy_tie_break_requires_sorted, which pins
        # a concrete case where dropping this `sorted()` flips the winner
        # of a tie to the wrong (higher-index) element.
        for cand in sorted(counts):   # ascending index == `elements` order
            ov = counts[cand] / len(qt)
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


def _window_evidence(el) -> Evidence:
    return Evidence(
        file=el.file,
        char_start=el.char_start,
        char_end=el.char_end,
        line_start=el.line_start,
        line_end=el.line_end,
        quote=el.text,
    )


def _plugin_kg_fragment_to_window(
    fragment: IndexingKgFragment,
    elements,
    *,
    section_path: str,
    win_idx: int,
    object_types: tuple[str, ...],
    limits: IndexingPipelineKgLimits,
) -> Tuple[List[Node], List[Edge]]:
    """Admit one plugin-mapped KG fragment into the core node/edge contract."""
    if type(fragment.objects) is not tuple or type(fragment.edges) is not tuple:
        raise ValueError("invalid plugin KG fragment")
    if (
        len(fragment.objects) > limits.max_objects
        or len(fragment.edges) > limits.max_edges
    ):
        raise ValueError("plugin KG fragment exceeds configured bounds")
    allowed_types = dict(_CORE_PLUGIN_NODE_TYPES)
    ambiguous_types: set[str] = set()
    for item in object_types:
        if type(item) is not str or not item.strip():
            continue
        canonical = item.strip()
        key = canonical.lower()
        existing = allowed_types.get(key)
        if existing is not None and existing != canonical and key not in _CORE_PLUGIN_NODE_TYPES:
            ambiguous_types.add(key)
            continue
        if key not in _CORE_PLUGIN_NODE_TYPES:
            allowed_types[key] = canonical
    for key in ambiguous_types:
        allowed_types.pop(key, None)
    handle_map = {f"e{index}": element for index, element in enumerate(elements)}
    nodes: List[Node] = []
    by_local: dict[str, str] = {}
    type_by_node_id: dict[str, str] = {}
    for proposal in fragment.objects:
        if type(proposal) is not IndexingKgObjectProposal:
            raise ValueError("invalid plugin KG object proposal")
        if (
            type(proposal.local_id) is not str
            or not proposal.local_id.strip()
            or proposal.local_id in by_local
            or type(proposal.object_type) is not str
            or proposal.object_type.strip().lower() not in allowed_types
            or type(proposal.name) is not str
            or not proposal.name.strip()
            or len(proposal.name) > limits.max_name_chars
            or type(proposal.evidence_handles) is not tuple
            or not proposal.evidence_handles
            or len(proposal.evidence_handles) > limits.max_evidence_handles
            or any(type(handle) is not str for handle in proposal.evidence_handles)
        ):
            raise ValueError("invalid plugin KG object proposal")
        canonical_type = allowed_types[proposal.object_type.strip().lower()]
        evidence = []
        for handle in proposal.evidence_handles:
            element = handle_map.get(handle)
            if element is None:
                raise ValueError("invalid plugin KG evidence handle")
            evidence.append(_window_evidence(element))
        node_id = f"W{win_idx}-{len(nodes)}"
        node = Node(
            id=node_id,
            type=canonical_type,
            name=proposal.name.strip(),
            # 插件给的 section_path 套与 name 同一条 max_name_chars 轨(codex #602
            # R2 P2:不设界的话每窗最多 max_objects 个超长值绕过 KG 载荷全部围栏、
            # 直进 staging/库)。超限**回落核心算出的 section_path** 而不是整源否决:
            # 它是展示性元数据,不是身份/证据。
            section_path=(
                proposal.section_path
                if (
                    type(proposal.section_path) is str
                    and proposal.section_path
                    and len(proposal.section_path) <= limits.max_name_chars
                )
                else section_path
            ),
            evidence=evidence,
        )
        if canonical_type in {"Claim", "Formula"}:
            node.validity_scope = _bounded_validity_scope(
                _parse_validity_scope(proposal.validity_scope), limits
            )
        if canonical_type == "Procedure":
            if (
                type(proposal.steps) is not tuple
                or len(proposal.steps) > limits.max_steps_per_object
            ):
                raise ValueError("invalid plugin KG steps")
            steps: list[Step] = []
            for step in proposal.steps:
                if (
                    type(step) is not IndexingKgStepProposal
                    or type(step.name) is not str
                    or not step.name.strip()
                    or len(step.name) > limits.max_name_chars
                    or type(step.evidence_handles) is not tuple
                    or not step.evidence_handles
                    or len(step.evidence_handles) > limits.max_evidence_handles
                    or any(
                        type(handle) is not str
                        for handle in step.evidence_handles
                    )
                ):
                    raise ValueError("invalid plugin KG step")
                step_evidence = []
                for handle in step.evidence_handles:
                    element = handle_map.get(handle)
                    if element is None:
                        raise ValueError("invalid plugin KG evidence handle")
                    step_evidence.append(_window_evidence(element))
                steps.append(Step(name=step.name.strip(), evidence=step_evidence))
            node.steps = steps
        nodes.append(node)
        by_local[proposal.local_id] = node_id
        type_by_node_id[node_id] = canonical_type

    edges: List[Edge] = []
    for proposal in fragment.edges:
        if type(proposal) is not IndexingKgEdgeProposal:
            raise ValueError("invalid plugin KG edge proposal")
        if (
            type(proposal.edge_type) is not str
            or proposal.edge_type not in VALID_EDGE_TYPES
            or type(proposal.source_local_id) is not str
            or type(proposal.target_local_id) is not str
            or type(proposal.evidence_handles) is not tuple
            or not proposal.evidence_handles
            or len(proposal.evidence_handles) > limits.max_evidence_handles
            or any(type(handle) is not str for handle in proposal.evidence_handles)
        ):
            raise ValueError("invalid plugin KG edge proposal")
        source_id = by_local.get(proposal.source_local_id)
        target_id = by_local.get(proposal.target_local_id)
        if not source_id or not target_id or source_id == target_id:
            raise ValueError("invalid plugin KG edge endpoints")
        if not is_queryable_edge_pair(
            proposal.edge_type,
            type_by_node_id.get(source_id),
            type_by_node_id.get(target_id),
        ):
            raise ValueError("invalid plugin KG edge endpoint pair")
        evidence = []
        for handle in proposal.evidence_handles:
            element = handle_map.get(handle)
            if element is None:
                raise ValueError("invalid plugin KG evidence handle")
            evidence.append(_window_evidence(element))
        edges.append(
            Edge(
                id=f"E{win_idx}-{len(edges)}",
                type=proposal.edge_type,
                source_id=source_id,
                target_id=target_id,
                evidence=evidence,
            )
        )
    return nodes, edges


def _bounded_validity_scope(scope: dict, limits: "IndexingPipelineKgLimits") -> dict:
    """插件 mapper 的 validity_scope 套核心围栏(codex #602 R3 P2)。

    `_parse_validity_scope` 是与核心抽取路径共享的归一化,不做长度界;插件产出在
    进 staging/库之前必须有界——列表条数复用 `max_steps_per_object`(同为「每对象
    列表」轨)、每个字符串复用 `max_name_chars`。任一越界即整个丢弃(返回 {}):
    它是论断/公式的补充标注,不是身份或证据,丢标注保对象。
    """
    for key in ("region", "assumptions"):
        items = scope.get(key)
        if items is None:
            continue
        if (
            len(items) > limits.max_steps_per_object
            or any(len(item) > limits.max_name_chars for item in items)
        ):
            return {}
    for key in ("approximation", "range"):
        value = scope.get(key)
        if value is not None and len(value) > limits.max_name_chars:
            return {}
    return scope


def _plugin_extract_window(
    client: Any,
    kg_strategy: Any,
    pipeline_id: str,
    elements,
    section_path: str,
    doc_type: str,
    win_idx: int,
    object_types: tuple[str, ...],
    limits: IndexingPipelineKgLimits,
) -> Tuple[List[Node], List[Edge]]:
    # 刻意不传 response_validator ⇒ 这条路**不进** LLM 内容寻址缓存(缓存是
    # opt-in):插件自定的响应形状 core 无法核验,没有可用的准入 validator。
    # 代价是插件管线的每次重建按全价重跑模型——已登记取舍(CLAUDE.md LLM 缓存条)。
    prompt_result = kg_strategy.build_kg_prompt(
        pipeline_id,
        elements,
        doc_type=doc_type,
        section_path=section_path,
        object_types=object_types,
    )
    prompt = prompt_result.prompt
    if type(prompt) is not IndexingKgPrompt:
        raise ValueError(prompt_result.warning_code or "invalid plugin KG prompt")
    if (
        len(prompt.messages) > limits.max_messages
        or len(prompt.response_schema_hint) > limits.max_schema_hint_chars
        or sum(len(message.content) for message in prompt.messages)
        > limits.max_prompt_chars
    ):
        raise ValueError("plugin KG prompt exceeds configured bounds")
    messages = [
        {"role": message.role, "content": message.content}
        for message in prompt.messages
        if type(message) is IndexingKgMessage
    ]
    if len(messages) != len(prompt.messages):
        raise ValueError("invalid plugin KG prompt message")
    raw = client.chat_json(
        messages,
        prompt.response_schema_hint,
        **cap_kwargs(client, "kg_extract_max_tokens"),
    )
    if type(raw) is not str:
        raise ValueError("invalid plugin KG model response")
    mapped = kg_strategy.map_kg_response(
        pipeline_id,
        raw,
        elements,
        doc_type=doc_type,
        section_path=section_path,
        object_types=object_types,
    )
    fragment = mapped.fragment
    if type(fragment) is not IndexingKgFragment:
        raise ValueError(mapped.warning_code or "invalid plugin KG fragment")
    return _plugin_kg_fragment_to_window(
        fragment,
        elements,
        section_path=section_path,
        win_idx=win_idx,
        object_types=object_types,
        limits=limits,
    )


def build_records(graph: KnowledgeGraph, source_id: str, source_title: str,
                  elements) -> Tuple[List[dict], List[dict]]:
    """KG graph -> (objects, relations) with product evidence bound to elements.
    Nodes whose evidence binds to no element are dropped; edges referencing a
    dropped node are dropped. Each object dict carries `local_id` (= KG node id)
    so the caller can remap edges to DB ids after insert.

    Builds exactly one `_QuoteBinder` for `elements` (this function always
    runs once per source) and reuses it for every quote below -- node
    evidence, step evidence and edge evidence all share the same
    precomputed index instead of each re-scanning `elements`. An empty
    graph short-circuits before that index is ever built: a zero-node graph
    has no quotes to bind, so paying for `_QuoteBinder`'s O(total text)
    construction would be pure waste."""
    if not graph.nodes:
        return [], []
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
                  base_filter: bool = False, kg_strategy: Any | None = None,
                  pipeline_id: str = "",
                  plugin_object_types: tuple[str, ...] = (),
                  plugin_limits: IndexingPipelineKgLimits | None = None,
                  ) -> KnowledgeGraph:
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
        if kg_strategy is not None and pipeline_id:
            if plugin_limits is None:
                raise ValueError("plugin KG extraction requires validated limits")
            futs = [
                submit_window(
                    _plugin_extract_window,
                    extract_client,
                    kg_strategy,
                    pipeline_id,
                    els,
                    w.section_path,
                    doc_type,
                    idx,
                    plugin_object_types,
                    plugin_limits,
                )
                for idx, (w, els) in enumerate(pairs)
            ]
        else:
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
