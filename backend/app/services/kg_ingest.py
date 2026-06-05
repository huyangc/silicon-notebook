"""Adapter: source raw text -> KG (nodes/edges/evidence) -> product knowledge.
The ONLY bridge between app.services.kg.* and the product. Extraction model is
the product LLM (deepseek-v4-flash via OPENAI_COMPAT_*)."""
from __future__ import annotations

import concurrent.futures as cf
import math
import re
from typing import Any, List, Tuple

from app.services.kg.windowing import windows_with_elements
from app.services.kg.extract import extract_window
from app.services.kg.canonicalize import canonicalize
from app.services.kg.models import Edge, KnowledgeGraph, Node

DOC_TYPE_MAP = {"academic_paper": "academic", "article": "academic", "textbook": "textbook"}
_WORKERS = 16


# ---------------------------------------------------------------------------
# Evidence binding helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _tokens(s: str) -> set:
    return set(re.findall(r"\w+", (s or "").lower()))


def _bind_quote(quote: str, elements, source_id: str, source_title: str) -> dict | None:
    """Return product-Evidence fields for the element that best contains `quote`."""
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
    so the caller can remap edges to DB ids after insert."""
    kept: set = set()
    objects: List[dict] = []
    for node in graph.nodes:
        bound = []
        for ev in node.evidence:
            fields = _bind_quote(ev.quote, elements, source_id, source_title)
            if fields:
                bound.append(fields)
        if not bound:
            continue
        kept.add(node.id)
        payload = {"name": node.name, "section_path": node.section_path}
        if node.steps:
            bound_steps = []
            for st in node.steps:
                quote = st.evidence[0].quote if st.evidence else ""
                fields = _bind_quote(quote, elements, source_id, source_title)
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
            relations.append({
                "source_local_id": edge.source_id,
                "target_local_id": edge.target_id,
                "edge_type": edge.type,
                "evidence": [{"quote": ev.quote} for ev in edge.evidence],
            })
    return objects, relations


def plan_window_size(content_chars: int, workers: int, w_min: int, w_max: int,
                     override: int = 0) -> int:
    """Balanced extraction window size (chars).

    override>0 forces a fixed size (back-compat / manual). Otherwise pick
    level = clamp(content_chars / workers, w_min, w_max), split into
    N = ceil(content_chars / level) windows, and return the BALANCED size
    ceil(content_chars / N) so windows are near-equal (no long-tail runt).
    """
    if override and override > 0:
        return override
    if content_chars <= w_min:
        return max(1, content_chars)
    level = min(w_max, max(w_min, content_chars // max(1, workers)))
    n_windows = max(1, math.ceil(content_chars / level))
    return math.ceil(content_chars / n_windows)


def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, workers: int = _WORKERS) -> KnowledgeGraph:
    """Window the text, extract a KG fragment per window concurrently, then
    canonicalize. Evidence is anchored by element-id markers: each window's
    prose elements are numbered and the LLM emits only an int "ev" per node/edge,
    which extract_window maps back to the element's exact text/offsets.
    Ungroundable nodes are dropped inside extract_window."""
    pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file,
                                                          None, n, m) if els]
    nodes: List[Node] = []
    edges: List[Edge] = []
    failed = 0
    if pairs:
        workers = max(1, min(workers, len(pairs)))
        # pool.submit + per-future .result() (NOT pool.map, which aborts on the
        # first exception): one window's network failure must not abort the rest.
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(extract_window, client, els, w.section_path,
                                doc_type, idx)
                    for idx, (w, els) in enumerate(pairs)]
            for fut in futs:
                try:
                    ns, es = fut.result()
                    nodes += ns
                    edges += es
                except Exception:
                    failed += 1
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
    return KnowledgeGraph(doc_id=source_file, doc_type=doc_type, nodes=nodes,
                          edges=edges, total_windows=len(pairs),
                          failed_windows=failed)
