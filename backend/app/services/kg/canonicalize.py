"""Merge Concept nodes by normalized name/alias across fragments; rewire edges."""
from __future__ import annotations
import re
import unicodedata
from typing import List, Tuple
from app.services.kg.models import Edge, Node

def _norm(name: str) -> str:
    # NFKC + Unicode \w:CJK/希腊/带音标名参与同文档窗口间合并(旧类清成空名,
    # 中文概念每窗口留一份重复节点)。删除语义保持:非字母数字直接删除(不是替换
    # 空格),下划线显式删除 —— 纯 ASCII 输出与旧 [^a-z0-9 ] 类逐字节相同。
    folded = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]|_", "", folded)).strip()

def canonicalize(nodes: List[Node], edges: List[Edge], doc_id: str) -> Tuple[List[Node], List[Edge]]:
    canon: dict = {}          # normalized name -> canonical Concept node
    proc_canon: dict = {}     # (normalized name, section_path) -> canonical Procedure node
    remap: dict = {}          # every original node id -> final id
    out: List[Node] = []
    cn = 0
    for n in nodes:
        if n.type == "Concept" and _norm(n.name):
            key = _norm(n.name)
            if key in canon:
                c = canon[key]
                c.mentions.extend(n.evidence + n.mentions)
                remap[n.id] = c.id
            else:
                cn += 1
                new_id = f"{doc_id}:C{cn}"
                remap[n.id] = new_id
                n.id = new_id
                n.mentions = list(n.evidence)
                canon[key] = n
                out.append(n)
        elif n.type == "Procedure" and _norm(n.name):
            key = (_norm(n.name), " ".join((n.section_path or "").split()))
            if key in proc_canon:
                c = proc_canon[key]
                c.steps.extend(n.steps)        # concatenate; ordered + deduped below
                remap[n.id] = c.id
            else:
                proc_canon[key] = n
                remap[n.id] = n.id
                out.append(n)
        else:
            remap[n.id] = n.id
            out.append(n)
    # order each merged flow's steps by evidence position and drop name-duplicates
    for n in out:
        if n.type == "Procedure" and n.steps:
            n.steps.sort(key=lambda s: (s.evidence[0].char_start if s.evidence else 1_000_000))
            seen, deduped = set(), []
            for s in n.steps:
                k = _norm(s.name)
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(s)
            n.steps = deduped
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
