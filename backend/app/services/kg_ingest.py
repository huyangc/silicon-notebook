"""Adapter: source raw text -> KG (nodes/edges/evidence) -> product knowledge.
The ONLY bridge between app.services.kg.* and the product. Extraction model is
the product LLM (deepseek-v4-flash via OPENAI_COMPAT_*)."""
from __future__ import annotations

import concurrent.futures as cf
from typing import Any, List, Tuple

from app.services.kg.windowing import make_windows
from app.services.kg.extract import extract_window
from app.services.kg.canonicalize import canonicalize
from app.services.kg.models import Edge, KnowledgeGraph, Node

DOC_TYPE_MAP = {"academic_paper": "academic", "article": "academic", "textbook": "textbook"}
_WORKERS = 16


def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450) -> KnowledgeGraph:
    """Window the text, extract a KG fragment per window concurrently, then
    canonicalize. Ungroundable nodes/edges are already dropped inside
    extract_window (evidence located verbatim in the window)."""
    wins = make_windows(raw_text, source_file, None, n, m)
    nodes: List[Node] = []
    edges: List[Edge] = []
    if wins:
        workers = max(1, min(_WORKERS, len(wins)))
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for ns, es in pool.map(
                lambda w: extract_window(client, raw_text, w.char_start, w.char_end,
                                         w.section_path, doc_type),
                wins,
            ):
                nodes += ns
                edges += es
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
    return KnowledgeGraph(doc_id=source_file, doc_type=doc_type, nodes=nodes, edges=edges)
