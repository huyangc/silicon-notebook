"""Content-based alignment: span IoU + a deterministic greedy matcher."""
from . import metrics as _metrics
from . import textnorm as _textnorm
from .config import OBJECT_MATCH_WEIGHTS

_BIG = 1_000_000.0  # > any line length in these fixtures; encodes (line,char) as a float


def _enc(line, char):
    return float(line) + (float(char) / _BIG)


def span_iou(s1, s2):
    """IoU of two source_span dicts. 0 if files differ or no overlap.

    Position encoded as line + char/1e6 so single-line spans give exact char IoU
    and multi-line spans stay monotonic (assumes line length < 1e6).
    """
    if not s1 or not s2:
        return 0.0
    if s1.get("file") != s2.get("file"):
        return 0.0
    a0 = _enc(s1["line_start"], s1["char_start"])
    a1 = _enc(s1["line_end"], s1["char_end"])
    b0 = _enc(s2["line_start"], s2["char_start"])
    b1 = _enc(s2["line_end"], s2["char_end"])
    lo, hi = max(a0, b0), min(a1, b1)
    overlap = max(0.0, hi - lo)
    union = (a1 - a0) + (b1 - b0) - overlap
    if union <= 0:
        return 1.0 if overlap > 0 or (a1 == a0 and b1 == b0 and a0 == b0) else 0.0
    return overlap / union


def greedy(gold_ids, pred_ids, scores, thresh):
    """Greedy max-score one-to-one matching.

    scores: {(gold_id, pred_id): score}. Pairs with score < thresh are ignored.
    Ties broken by (gold_id, pred_id) lexicographic order for determinism.
    Returns the Alignment dict described in the plan header.
    """
    ranked = sorted(
        ((s, g, p) for (g, p), s in scores.items() if s >= thresh),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    g2p, p2g = {}, {}
    matches = []
    for s, g, p in ranked:
        if g in g2p or p in p2g:
            continue
        g2p[g] = p
        p2g[p] = g
        matches.append((g, p, s))
    unmatched_gold = [g for g in gold_ids if g not in g2p]
    unmatched_pred = [p for p in pred_ids if p not in p2g]
    return {
        "matches": matches,
        "g2p": g2p,
        "p2g": p2g,
        "unmatched_gold": unmatched_gold,
        "unmatched_pred": unmatched_pred,
    }


def match_atoms(gold_atoms, pred_atoms, thresh):
    """Align atoms by source_span IoU."""
    gids = [a["id"] for a in gold_atoms]
    pids = [a["id"] for a in pred_atoms]
    gspan = {a["id"]: a.get("source_span") for a in gold_atoms}
    pspan = {a["id"]: a.get("source_span") for a in pred_atoms}
    scores = {}
    for g in gids:
        for p in pids:
            iou = span_iou(gspan[g], pspan[p])
            if iou > 0:
                scores[(g, p)] = iou
    return greedy(gids, pids, scores, thresh)


def _payload_value_overlap(gp, pp):
    gvals = [_textnorm.norm_text(v) for v in _textnorm.payload_values(gp)]
    pvals = [_textnorm.norm_text(v) for v in _textnorm.payload_values(pp)]
    if not gvals and not pvals:
        return 1.0
    if not gvals or not pvals:
        return 0.0
    matched = 0
    pool = list(pvals)
    for gv in gvals:
        for i, pv in enumerate(pool):
            if gv and pv and (gv == pv or (len(gv) >= 4 and gv in pv) or (len(pv) >= 4 and pv in gv)):
                matched += 1
                pool.pop(i)
                break
    return matched / max(len(gvals), len(pvals))


def object_pair_score(gold_obj, pred_obj, atom_p2g):
    type_s = 1.0 if gold_obj.get("type") == pred_obj.get("type") else 0.0
    gloc = set(gold_obj.get("local_evidence_atom_ids") or [])
    ploc = {atom_p2g[a] for a in (pred_obj.get("local_evidence_atom_ids") or []) if a in atom_p2g}
    ev_s = _metrics.jaccard(gloc, ploc)
    pay_s = _payload_value_overlap(gold_obj.get("payload"), pred_obj.get("payload"))
    w = OBJECT_MATCH_WEIGHTS
    return w["type"] * type_s + w["evidence"] * ev_s + w["payload"] * pay_s


def match_objects(gold_objs, pred_objs, atom_p2g, thresh):
    gids = [o["id"] for o in gold_objs]
    pids = [o["id"] for o in pred_objs]
    gobj = {o["id"]: o for o in gold_objs}
    pobj = {o["id"]: o for o in pred_objs}
    scores = {}
    for g in gids:
        for p in pids:
            s = object_pair_score(gobj[g], pobj[p], atom_p2g)
            if s > 0:
                scores[(g, p)] = s
    return greedy(gids, pids, scores, thresh)
