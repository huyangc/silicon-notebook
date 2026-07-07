"""社区感知检索原语:焦点实体 → base 库所在社区 → 同社区兄弟实体名。
纯查表 + 廉价词法重排;任何缺失 fail-open 返回 [] 并 emit 事件(绝不静默零召回)。"""
from __future__ import annotations
from typing import List, Optional


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def first_base_notebook_id(repo, active_nb: str) -> Optional[str]:
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM notebooks WHERE tier='base' AND id != ? ORDER BY updated_at DESC LIMIT 1",
            (active_nb,)).fetchone()
    return row["id"] if row else None


def community_peers(repo, base_nb: str, focal_name: str, query: str, *,
                    top_k: int, candidates: int) -> List[str]:
    from app.services.retrieval import keyword_score
    key = _norm(focal_name)
    if not base_nb or not key:
        return []
    with repo._connect() as db:
        frow = db.execute(
            "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? AND lower(canonical_name)=? "
            "GROUP BY canonical_id ORDER BY COUNT(*) DESC LIMIT 1", (base_nb, key)).fetchone()
        if not frow:
            repo.event_log.emit({"kind": "community_unavailable", "notebook_id": base_nb,
                                 "reason": "focal_unresolved", "focal": focal_name})
            return []
        focal_can = frow["canonical_id"]
        crow = db.execute(
            "SELECT community_id FROM community_members WHERE notebook_id=? AND canonical_id=? "
            "ORDER BY level DESC LIMIT 1", (base_nb, focal_can)).fetchone()
        if not crow:
            repo.event_log.emit({"kind": "community_unavailable", "notebook_id": base_nb,
                                 "reason": "not_built", "focal": focal_name})
            return []
        rows = db.execute(
            "SELECT canonical_name, centrality FROM community_members "
            "WHERE notebook_id=? AND community_id=? AND canonical_id!=? "
            "ORDER BY centrality DESC LIMIT ?", (base_nb, crow["community_id"], focal_can, candidates)
        ).fetchall()
    ranked = sorted(rows, key=lambda r: (keyword_score(query, r["canonical_name"] or ""),
                                         r["centrality"]), reverse=True)
    seen, out = set(), []
    for r in ranked:
        nm = (r["canonical_name"] or "").strip()
        k = _norm(nm)
        if nm and k not in seen:
            seen.add(k); out.append(nm)
        if len(out) >= top_k:
            break
    return out
