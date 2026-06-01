"""Graph matching: align pred nodes to gold by (type + name-sim + evidence overlap)."""
from __future__ import annotations
import re
from difflib import SequenceMatcher

def _norm(s): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (s or "").lower())).strip()

def _span_overlap(a, b):
    inter = max(0, min(a.char_end, b.char_end) - max(a.char_start, b.char_start))
    return inter > 0

def _node_key(n):
    return _norm(n.name)

def node_sim(g, p):
    if g.type != p.type:
        return 0.0
    name = SequenceMatcher(None, _node_key(g), _node_key(p)).ratio()
    ev = 1.0 if any(_span_overlap(ge, pe) for ge in g.evidence for pe in p.evidence) else 0.0
    return max(name, 0.5 * name + 0.5 * ev)

def match_nodes(gold, pred, thresh=0.6):
    pairs = []
    used = set()
    for g in gold:
        best, bi = 0.0, None
        for i, p in enumerate(pred):
            if i in used:
                continue
            s = node_sim(g, p)
            if s > best:
                best, bi = s, i
        if bi is not None and best >= thresh:
            used.add(bi)
            pairs.append((g.id, pred[bi].id))
    return dict(pairs)   # gold_id -> pred_id
