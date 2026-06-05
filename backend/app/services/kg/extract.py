"""Window -> LLM KG fragment -> grounded nodes/edges. Local ids are kept so the
caller can wire edges. Evidence is anchored by element-id markers: the LLM emits
only an integer "ev" label per node/edge, and the backend maps it back to that
source element's exact text/offsets (drop ungroundable nodes). Node types
constrained to the 4; edges to the vocab."""
from __future__ import annotations
from typing import Any, List, Optional, Tuple
from openai import APIConnectionError, APITimeoutError
from app.services.kg.client import safe_json
from app.services.kg.models import Edge, Evidence, Node, Step
from app.services.kg.parsing import SourceElementQ

NODE_TYPES = {"Concept", "Claim", "Formula", "Procedure"}
EDGE_TYPES = {"defines", "part_of", "composed_of", "contrasts_with", "kind_of",
              "about", "supports", "derived_from", "depends_on", "prerequisite_of",
              "used_in", "precedes"}

_KG_SCHEMA_HINT = (
    '{"nodes":[{"local_id":"","type":"Concept|Claim|Formula|Procedure",'
    '"name":"","ev":0,"steps":[{"name":"","ev":0}]}],'
    '"edges":[{"type":"about|supports|...","source":"","target":"","ev":0}]}'
)


def _prompt(labeled_text: str, section_path: str, doc_type: str) -> str:
    return f"""Extract a knowledge-graph fragment from this {doc_type} passage
(section: {section_path}). Use EXACTLY these node types: Concept, Claim, Formula,
Procedure (see definitions: Concept=a NAMED, reusable technical entity — a method,
mechanism, component, named model/structure/distribution; Claim=truth-evaluable
assertion about concepts; Formula=equation; Procedure=ordered process). Edges
(source->target): defines(Claim->Concept), about(Claim|Formula->Concept),
supports(Claim|Formula->Claim), part_of/composed_of/contrasts_with/kind_of(Concept->
Concept), derived_from(Formula->Formula), depends_on/prerequisite_of,
used_in(Formula->Procedure), precedes.

Be SELECTIVE with Concepts: emit a Concept only for a distinctive named entity. Do
NOT emit Concepts for generic/common terms (e.g. training, inference, buffer,
latency, forward pass, backward pass, hidden state, input sequence, host memory) or
for trivial sub-parts of another concept. In contrast, capture EVERY Formula
(equation) and EVERY Procedure (process/phase) present — do not skip those.

The passage is given as numbered elements, one per line, each prefixed with its
integer label like [3]. Every node and edge MUST include "ev": the INTEGER label
of the element that best contains it (NOT a quote, NOT text). Give each node a
"local_id" you reuse in edges. "name" carries the node's text (Concept/Procedure
name, Claim statement, Formula expression). For a Procedure that is an ordered multi-step process/flow, emit it as ONE Procedure node (named after the flow — use the section heading if it names the flow) and list its ordered steps in a `steps` array, each {{"name":..,"ev":..}} where ev is the element label containing that step; prefer this over many separate Procedure nodes. `steps` is the source of truth for order (you may still add `precedes` edges). Skip narrative/filler.

Passage:
\"\"\"{labeled_text}\"\"\"

Return JSON ONLY:
{{"nodes":[{{"local_id":"..","type":"..","name":"..","ev":0}}],
 "edges":[{{"type":"..","source":"<local_id>","target":"<local_id>","ev":0}}]}}
"""


def _resolve(elements: List[SourceElementQ], ev: Any,
             name: str) -> Optional[SourceElementQ]:
    try:
        i = int(ev)
    except Exception:
        i = -1
    if 0 <= i < len(elements):
        return elements[i]
    # fallback: element whose text contains the node name (normalized substring)
    nn = " ".join((name or "").split()).lower()
    if nn:
        for e in elements:
            if nn in " ".join(e.text.split()).lower():
                return e
    return None


def _ev(el: SourceElementQ) -> Evidence:
    return Evidence(file=el.file, char_start=el.char_start, char_end=el.char_end,
                    line_start=el.line_start, line_end=el.line_end, quote=el.text)


def _parse_steps(elements: List[SourceElementQ], raw_steps: Any) -> List[Step]:
    """Resolve an LLM `steps` array into grounded Step objects (drop unbindable)."""
    steps: List[Step] = []
    if not isinstance(raw_steps, list):
        return steps
    for st in raw_steps:
        if not isinstance(st, dict):
            continue
        nm = str(st.get("name", "")).strip()
        if not nm:
            continue
        el = _resolve(elements, st.get("ev"), nm)
        if el is None:
            continue
        steps.append(Step(name=nm, evidence=[_ev(el)]))
    return steps


def extract_window(client: Any, elements: List[SourceElementQ], section_path: str,
                   doc_type: str, win_idx: int = 0) -> Tuple[List[Node], List[Edge]]:
    if not elements:
        return [], []
    labeled = "\n".join(f"[{i}] {e.text}" for i, e in enumerate(elements))
    try:
        # OpenAICompatibleClient.chat_json takes (messages, response_schema_hint).
        raw = client.chat_json(
            [{"role": "user", "content": _prompt(labeled, section_path, doc_type)}],
            _KG_SCHEMA_HINT,
        )
        data = safe_json(raw)
    except (APIConnectionError, APITimeoutError):
        raise            # hard failure: window never processed — caller counts it
    except Exception:
        return [], []    # soft: unparseable/empty — legitimately 0 nodes
    nodes: List[Node] = []
    by_local = {}
    for it in (data.get("nodes") or []):
        if not isinstance(it, dict) or it.get("type") not in NODE_TYPES:
            continue
        el = _resolve(elements, it.get("ev"), str(it.get("name", "")))
        if el is None:
            continue
        nid = f"W{win_idx}-{len(nodes)}"
        node = Node(id=nid, type=it["type"], name=str(it.get("name", "")),
                    section_path=section_path, evidence=[_ev(el)])
        if it["type"] == "Procedure":
            node.steps = _parse_steps(elements, it.get("steps"))
        nodes.append(node)
        if it.get("local_id"):
            by_local[str(it["local_id"])] = nid
    edges: List[Edge] = []
    for it in (data.get("edges") or []):
        if not isinstance(it, dict) or it.get("type") not in EDGE_TYPES:
            continue
        s = by_local.get(str(it.get("source"))); t = by_local.get(str(it.get("target")))
        if not s or not t or s == t:
            continue
        el = _resolve(elements, it.get("ev"), "")
        ev = [_ev(el)] if el is not None else []
        edges.append(Edge(id=f"E{win_idx}-{len(edges)}", type=it["type"],
                          source_id=s, target_id=t, evidence=ev))
    return nodes, edges
