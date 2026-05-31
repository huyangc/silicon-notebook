"""Per-stage scorers. Each returns {'score': float0_1, 'prf': {...}, ...details}."""
from . import align, metrics, textnorm
from .config import THRESHOLDS


def _by_id(items):
    return {it["id"]: it for it in (items or [])}


def score_atoms(gold_atoms, pred_atoms):
    gold_atoms = gold_atoms or []
    pred_atoms = pred_atoms or []
    al = align.match_atoms(gold_atoms, pred_atoms, THRESHOLDS["atom_iou"])
    g = _by_id(gold_atoms)
    p = _by_id(pred_atoms)

    type_mismatches = []
    type_ok = 0
    ious = []
    for gid, pid, iou in al["matches"]:
        ious.append(iou)
        if g[gid].get("atom_type") == p[pid].get("atom_type"):
            type_ok += 1
        else:
            type_mismatches.append({
                "gold_id": gid, "pred_id": pid,
                "gold_type": g[gid].get("atom_type"), "pred_type": p[pid].get("atom_type"),
            })

    n_matched = len(al["matches"])
    tp = type_ok  # strict TP requires correct atom_type
    wrong_type = n_matched - type_ok
    fp = len(al["unmatched_pred"]) + wrong_type
    fn = len(al["unmatched_gold"]) + wrong_type
    pr = metrics.prf(tp, fp, fn)
    type_accuracy = (type_ok / n_matched) if n_matched else 1.0
    mean_iou = (sum(ious) / len(ious)) if ious else 1.0
    return {
        "score": pr["f1"],
        "prf": pr,
        "type_accuracy": type_accuracy,
        "mean_iou": round(mean_iou, 4),
        "type_mismatches": type_mismatches,
        "missed": al["unmatched_gold"],
        "spurious": al["unmatched_pred"],
        "alignment": al,
    }


def _map_atoms(atom_ids, p2g):
    """Translate predicted atom ids into gold-atom space; drop unmappable."""
    return {p2g[a] for a in (atom_ids or []) if a in p2g}


def score_chunks(gold_chunks, pred_chunks, atom_p2g):
    gold_chunks = gold_chunks or []
    pred_chunks = pred_chunks or []
    gid = [c["id"] for c in gold_chunks]
    pid = [c["id"] for c in pred_chunks]
    gset = {c["id"]: set(c.get("atom_ids") or []) for c in gold_chunks}
    pset = {c["id"]: _map_atoms(c.get("atom_ids"), atom_p2g) for c in pred_chunks}

    scores = {}
    for g in gid:
        for p in pid:
            j = metrics.jaccard(gset[g], pset[p])
            if j > 0:
                scores[(g, p)] = j
    al = align.greedy(gid, pid, scores, THRESHOLDS["chunk_jaccard"])

    gtype = {c["id"]: c.get("chunk_type") for c in gold_chunks}
    ptype = {c["id"]: c.get("chunk_type") for c in pred_chunks}
    type_ok = 0
    type_mismatches = []
    for g, p, _ in al["matches"]:
        if gtype[g] == ptype[p]:
            type_ok += 1
        else:
            type_mismatches.append({"gold_id": g, "pred_id": p,
                                    "gold_type": gtype[g], "pred_type": ptype[p]})
    n = len(al["matches"])
    wrong = n - type_ok
    pr = metrics.prf(type_ok, len(al["unmatched_pred"]) + wrong, len(al["unmatched_gold"]) + wrong)
    # over/under split heuristics: matched-pred whose atom set is a strict subset/superset
    over_split = 0
    under_split = 0
    for g, p, _ in al["matches"]:
        if pset[p] and pset[p] < gset[g]:
            over_split += 1
        if gset[g] and gset[g] < pset[p]:
            under_split += 1
    over_split += len(al["unmatched_pred"])  # extra chunks are over-splitting symptoms
    return {
        "score": pr["f1"],
        "prf": pr,
        "type_accuracy": (type_ok / n) if n else 1.0,
        "over_split": over_split,
        "under_split": under_split,
        "type_mismatches": type_mismatches,
        "missed": al["unmatched_gold"],
        "spurious": al["unmatched_pred"],
        "alignment": al,
    }


def _payload_field_prf(gold_objs, pred_objs, matches, judge=None):
    """Aggregate payload value-level P/R/F1 over loosely-matched object pairs."""
    g = _by_id(gold_objs)
    p = _by_id(pred_objs)
    tp = fp = fn = 0
    gaps = []
    for gid, pid, _ in matches:
        gvals = textnorm.payload_values(g[gid].get("payload"))
        pvals = textnorm.payload_values(p[pid].get("payload"))
        pool = list(pvals)
        local_tp = 0
        missed_vals = []
        for gv in gvals:
            hit = None
            for i, pv in enumerate(pool):
                if textnorm.text_equiv(gv, pv, judge=judge):
                    hit = i
                    break
            if hit is not None:
                pool.pop(hit)
                local_tp += 1
            else:
                missed_vals.append(gv)
        tp += local_tp
        fn += len(gvals) - local_tp
        fp += len(pool)
        if missed_vals:
            gaps.append({"gold_id": gid, "pred_id": pid, "missing_values": missed_vals})
    # Recall-aware: every gold object the candidate did NOT match contributes its
    # payload fields as misses (fn). Without this, a candidate that extracts no
    # objects has zero matched pairs and metrics.prf(0,0,0)==1.0 — a vacuous
    # perfect score that rewards extracting nothing. (Gold-vs-gold is unaffected:
    # every gold object matches itself, so there are no unmatched gold objects.)
    matched_gold = {gid for gid, _pid, _ in matches}
    for gobj in gold_objs:
        if gobj.get("id") not in matched_gold:
            fn += len(textnorm.payload_values(gobj.get("payload")))
    pr = metrics.prf(tp, fp, fn)
    pr["gaps"] = gaps
    return pr


def score_objects(gold_objs, pred_objs, atom_p2g, judge=None):
    gold_objs = gold_objs or []
    pred_objs = pred_objs or []
    al = align.match_objects(gold_objs, pred_objs, atom_p2g, THRESHOLDS["object_match"])
    g = _by_id(gold_objs)
    p = _by_id(pred_objs)

    type_ok = 0
    type_mismatches = []
    ev_jaccards = []
    for gid, pid, _ in al["matches"]:
        if g[gid].get("type") == p[pid].get("type"):
            type_ok += 1
        else:
            type_mismatches.append({"gold_id": gid, "pred_id": pid,
                                    "gold_type": g[gid].get("type"), "pred_type": p[pid].get("type")})
        gloc = set(g[gid].get("local_evidence_atom_ids") or [])
        ploc = {atom_p2g[a] for a in (p[pid].get("local_evidence_atom_ids") or []) if a in atom_p2g}
        ev_jaccards.append(metrics.jaccard(gloc, ploc))

    n = len(al["matches"])
    wrong = n - type_ok
    pr = metrics.prf(type_ok, len(al["unmatched_pred"]) + wrong, len(al["unmatched_gold"]) + wrong)
    payload = _payload_field_prf(gold_objs, pred_objs, al["matches"], judge=judge)
    # Recall-aware evidence: average local-evidence Jaccard over ALL gold objects
    # (unmatched gold objects contribute 0), so zero predictions -> 0.0 rather
    # than a vacuous 1.0. Gold-vs-gold still scores 1.0 (every gold object
    # matches itself). Empty gold (no objects to evaluate) stays 1.0.
    mean_jac = (sum(ev_jaccards) / len(gold_objs)) if gold_objs else 1.0
    return {
        "score": pr["f1"],
        "prf": pr,
        "type_accuracy": (type_ok / n) if n else 1.0,
        "type_mismatches": type_mismatches,
        "payload": {"precision": payload["precision"], "recall": payload["recall"],
                    "f1": payload["f1"], "gaps": payload["gaps"]},
        "evidence": {"mean_jaccard": round(mean_jac, 4)},
        "missed": al["unmatched_gold"],
        "spurious": al["unmatched_pred"],
        "alignment": al,
    }


def score_relations(gold_rels, pred_rels, obj_g2p, obj_p2g):
    gold_rels = gold_rels or []
    pred_rels = pred_rels or []
    # Index pred relations by their endpoints translated to gold-object space.
    pred_by_endpoints = {}
    for r in pred_rels:
        s = obj_p2g.get(r.get("source_object_id"))
        t = obj_p2g.get(r.get("target_object_id"))
        pred_by_endpoints.setdefault((s, t), []).append(r)

    tp = 0
    type_mismatches = []
    missed = []
    used_pred = set()
    for r in gold_rels:
        key = (r.get("source_object_id"), r.get("target_object_id"))
        cands = [pr for pr in pred_by_endpoints.get(key, []) if id(pr) not in used_pred]
        if not cands:
            missed.append(r["id"])
            continue
        # prefer a candidate with matching relation_type
        match = next((pr for pr in cands if pr.get("relation_type") == r.get("relation_type")), None)
        if match is not None:
            tp += 1
            used_pred.add(id(match))
        else:
            chosen = cands[0]
            used_pred.add(id(chosen))
            type_mismatches.append({"gold_id": r["id"], "pred_id": chosen.get("id"),
                                    "gold_type": r.get("relation_type"),
                                    "pred_type": chosen.get("relation_type")})
    wrong = len(type_mismatches)
    spurious = [pr.get("id") for pr in pred_rels if id(pr) not in used_pred]
    fp = len(spurious) + wrong
    fn = len(missed) + wrong
    pr = metrics.prf(tp, fp, fn)
    return {"score": pr["f1"], "prf": pr, "type_mismatches": type_mismatches,
            "missed": missed, "spurious": spurious}


def score_packages(gold_pkgs, pred_pkgs, pred_objs, chunk_g2p, obj_g2p):
    gold_pkgs = gold_pkgs or []
    if not gold_pkgs:
        return {"score": 1.0, "object_recall": 1.0, "local_field_coverage": 1.0, "details": []}
    pred_obj_by_id = {o["id"]: o for o in (pred_objs or [])}
    total_obj = matched_obj = 0
    total_fields = matched_fields = 0
    details = []
    for pkg in gold_pkgs:
        rec_hits = []
        # An expected object is "recovered" for this package if the candidate produced an
        # aligned object for it. (A package like PKG-HIER may expect objects whose canonical
        # home_package is a later detail package, so we don't gate on home here.)
        for gobj in (pkg.get("expected_objects") or []):
            total_obj += 1
            pobj = obj_g2p.get(gobj)
            if pobj is not None:
                matched_obj += 1
            else:
                rec_hits.append(gobj)
        # expected_local_fields names cross-package objects (homed elsewhere); credit a
        # field when the aligned pred object carries that payload key (no home requirement).
        for _gobj, fields in (pkg.get("expected_local_fields") or {}).items():
            fields = fields or []
            total_fields += len(fields)
            pobj_id = obj_g2p.get(_gobj)
            pobj = pred_obj_by_id.get(pobj_id) if pobj_id is not None else None
            pkeys = set((pobj.get("payload") or {}).keys()) if pobj else set()
            matched_fields += sum(1 for f in fields if f in pkeys)
        if rec_hits:
            details.append({"package": pkg["id"], "missed_expected_objects": rec_hits})
    object_recall = (matched_obj / total_obj) if total_obj else 1.0
    field_cov = (matched_fields / total_fields) if total_fields else 1.0
    score = 0.7 * object_recall + 0.3 * field_cov
    return {"score": score, "object_recall": object_recall,
            "local_field_coverage": field_cov, "details": details}


def score_structure(gold_tree, pred_tree, gold_mentions, pred_mentions, atom_p2g):
    # sections matched by normalized path
    gpaths = {textnorm.norm_text(n.get("path")) for n in (gold_tree or [])}
    ppaths = {textnorm.norm_text(n.get("path")) for n in (pred_tree or [])}
    s_tp = len(gpaths & ppaths)
    s_pr = metrics.prf(s_tp, len(ppaths - gpaths), len(gpaths - ppaths))

    # mentions matched by (mapped atom id, normalized text)
    def mkey(m, mapper):
        aid = m.get("atom_id")
        aid = mapper.get(aid, aid) if mapper else aid
        return (aid, textnorm.norm_text(m.get("text")))

    gset = {mkey(m, None) for m in (gold_mentions or [])}
    pset = {mkey(m, atom_p2g) for m in (pred_mentions or [])}
    m_tp = len(gset & pset)
    m_pr = metrics.prf(m_tp, len(pset - gset), len(gset - pset))
    score = 0.5 * s_pr["f1"] + 0.5 * m_pr["f1"]
    return {"score": score, "sections": s_pr, "mentions": m_pr}


_IDENTITY_KEYS = ("name", "statement", "claim", "term", "label", "title")


def _dne_surfaces(doc):
    """Entity surfaces that signal 'this text was extracted as a knowledge entity':
    mention texts + object identity fields. NOT payload prose / atom spans (those
    legitimately reference figures/citations)."""
    surf = set()
    for m in (doc.get("mentions") or []):
        surf.add(textnorm.norm_text(m.get("text")))
    for o in (doc.get("objects") or []):
        p = o.get("payload") or {}
        for k in _IDENTITY_KEYS:
            if isinstance(p.get(k), str):
                surf.add(textnorm.norm_text(p[k]))
    return {s for s in surf if s}


def score_do_not_extract(gold_dne, pred, gold=None):
    gold_dne = gold_dne or []
    pred_surf = _dne_surfaces(pred)
    # Over-extraction is RELATIVE to gold: a forbidden item the candidate names as an
    # entity is only a violation if gold does not also legitimately extract it (e.g.
    # "Operational Amplifier" is both a figure label AND a real circuit object). In
    # gold-vs-gold this exemption makes the score a perfect 1.0 by construction.
    gold_surf = _dne_surfaces(gold) if gold else set()

    def named_in(n, surfaces):
        return any(n == s or n in s for s in surfaces)

    def forbidden_texts(entry):
        if entry.get("text"):
            return [entry["text"]]
        return entry.get("examples") or []

    total = violations = 0
    hits = []
    for entry in gold_dne:
        for ft in forbidden_texts(entry):
            total += 1
            n = textnorm.norm_text(ft)
            if n and named_in(n, pred_surf) and not named_in(n, gold_surf):
                violations += 1
                hits.append({"forbidden": ft, "kind": entry.get("kind")})
    suppression = 1.0 if total == 0 else (total - violations) / total
    return {"score": suppression, "violations": violations, "total": total, "hits": hits}
