"""score_kg(gold, pred) -> node/edge P/R/F1 + evidence grounding."""
from __future__ import annotations
from app.services.kg_eval.match import match_nodes

def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else (1.0 if fn == 0 else 0.0)
    r = tp / (tp + fn) if tp + fn else (1.0 if fp == 0 else 0.0)
    f = 2 * p * r / (p + r) if p + r else (1.0 if tp == fp == fn == 0 else 0.0)
    return {"precision": p, "recall": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}

def score_kg(gold, pred):
    g2p = match_nodes(gold.nodes, pred.nodes)
    n_tp = len(g2p)
    nodes = _prf(n_tp, len(pred.nodes) - n_tp, len(gold.nodes) - n_tp)
    # edges: map gold endpoints to pred via g2p; matched if same pred endpoints+type exist
    pred_edges = {(e.type, e.source_id, e.target_id) for e in pred.edges}
    e_tp = 0
    for e in gold.edges:
        ps, pt = g2p.get(e.source_id), g2p.get(e.target_id)
        if ps and pt and (e.type, ps, pt) in pred_edges:
            e_tp += 1
    edges = _prf(e_tp, len(pred.edges) - e_tp, len(gold.edges) - e_tp)
    return {"nodes": nodes, "edges": edges}
