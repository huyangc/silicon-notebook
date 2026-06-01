"""Merge Concept nodes by normalized name/alias across fragments; rewire edges."""
from __future__ import annotations
import re
from typing import List, Tuple
from app.services.kg.models import Edge, Node

def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", name.lower())).strip()

def canonicalize(nodes: List[Node], edges: List[Edge], doc_id: str) -> Tuple[List[Node], List[Edge]]:
    canon: dict = {}          # normalized name -> canonical Concept node
    remap: dict = {}          # every original node id -> final id
    out: List[Node] = []
    cn = 0
    for n in nodes:
        if n.type == "Concept" and _norm(n.name):
            key = _norm(n.name)
            if key in canon:
                c = canon[key]
                c.mentions.extend(n.evidence + n.mentions)
                if n.name != c.name and n.name not in (c.attrs.get("aliases") or []):
                    c.attrs.setdefault("aliases", []).append(n.name)
                remap[n.id] = c.id
            else:
                cn += 1
                new_id = f"{doc_id}:C{cn}"
                remap[n.id] = new_id
                n.id = new_id
                n.mentions = list(n.evidence)
                canon[key] = n
                out.append(n)
        else:
            remap[n.id] = n.id
            out.append(n)
    final_edges: List[Edge] = []
    seen = set()
    for e in edges:
        s = remap.get(e.source_id, e.source_id)
        t = remap.get(e.target_id, e.target_id)
        if s == t:
            continue
        key = (e.type, s, t)
        if key in seen:
            continue
        seen.add(key)
        final_edges.append(Edge(id=e.id, type=e.type, source_id=s, target_id=t, evidence=e.evidence))
    return out, final_edges
