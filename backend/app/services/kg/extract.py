"""Window -> LLM KG fragment -> grounded nodes/edges. Local ids are kept so the
caller can wire edges; the LLM's evidence quotes are located verbatim in the
window (drop ungroundable). Node types constrained to the 4; edges to the vocab."""
from __future__ import annotations
import re
from typing import Any, List, Optional, Tuple
from app.services.kg.client import safe_json
from app.services.kg.models import Edge, Evidence, Node

NODE_TYPES = {"Concept", "Claim", "Formula", "Procedure"}
EDGE_TYPES = {"defines", "part_of", "composed_of", "contrasts_with", "kind_of",
              "about", "supports", "derived_from", "depends_on", "prerequisite_of",
              "used_in", "precedes"}

_KG_SCHEMA_HINT = (
    '{"nodes":[{"local_id":"","type":"Concept|Claim|Formula|Procedure",'
    '"name":"","evidence":""}],'
    '"edges":[{"type":"about|supports|...","source":"","target":"","evidence":""}]}'
)

def _locate(window: str, quote: str) -> Optional[Tuple[int, str]]:
    if not quote or len(quote.strip()) < 3:
        return None
    i = window.find(quote)
    if i >= 0:
        return i, quote
    pat = r"\s+".join(re.escape(t) for t in quote.split())
    m = re.search(pat, window)
    return (m.start(), m.group(0)) if m else None

def _prompt(window_text: str, section_path: str, doc_type: str) -> str:
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure (see definitions: Concept=named entity; Claim=truth-evaluable assertion
about concepts; Formula=equation; Procedure=ordered process). Edges (source->target):
defines(Claim->Concept), about(Claim|Formula->Concept), supports(Claim|Formula->Claim),
part_of/composed_of/contrasts_with/kind_of(Concept->Concept), derived_from(Formula->
Formula), depends_on/prerequisite_of, used_in(Formula->Procedure), precedes.

Every node and edge MUST include "evidence": an EXACT verbatim substring copied from
the passage. Give each node a "local_id" you reuse in edges. "name" carries the node's
text (Concept/Procedure name, Claim statement, Formula expression). Skip narrative/filler.

Passage:
\"\"\"{window_text}\"\"\"

Return JSON ONLY:
{{"nodes":[{{"local_id":"..","type":"..","name":"..","evidence":"<verbatim>"}}],
 "edges":[{{"type":"..","source":"<local_id>","target":"<local_id>","evidence":"<verbatim>"}}]}}
"""

def extract_window(client: Any, source_text: str, win_start: int, win_end: int,
                   section_path: str, doc_type: str) -> Tuple[List[Node], List[Edge]]:
    window = source_text[win_start:win_end]
    try:
        # OpenAICompatibleClient.chat_json takes (messages, response_schema_hint).
        raw = client.chat_json(
            [{"role": "user", "content": _prompt(window, section_path, doc_type)}],
            _KG_SCHEMA_HINT,
        )
        data = safe_json(raw)
    except Exception:
        return [], []
    nodes: List[Node] = []
    by_local = {}
    for it in (data.get("nodes") or []):
        if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
            continue
        loc = _locate(window, str(it.get("evidence", "")))
        if loc is None:
            continue
        local, matched = loc
        cstart = win_start + local
        line = source_text.count("\n", 0, cstart) + 1
        nid = f"L{win_start}-{len(nodes)}"
        ev = Evidence(file="", char_start=cstart, char_end=cstart + len(matched),
                      line_start=line, line_end=source_text.count("\n", 0, cstart + len(matched)) + 1,
                      quote=matched)
        nodes.append(Node(id=nid, type=it["type"], name=str(it.get("name", "")),
                          section_path=section_path, evidence=[ev]))
        if it.get("local_id"):
            by_local[str(it["local_id"])] = nid
    edges: List[Edge] = []
    for it in (data.get("edges") or []):
        if not isinstance(it, dict) or it.get("type") not in EDGE_TYPES:
            continue
        s = by_local.get(str(it.get("source"))); t = by_local.get(str(it.get("target")))
        if not s or not t or s == t:
            continue
        edges.append(Edge(id=f"E{win_start}-{len(edges)}", type=it["type"],
                          source_id=s, target_id=t))
    return nodes, edges
