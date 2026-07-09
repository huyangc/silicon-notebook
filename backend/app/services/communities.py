"""对比检索原语:焦点实体 → 兄弟实体名(横向对比候选)。

两条数据源,统一由 resolve_comparison_peers「共提优先、社区回退」编排:
 · sibling_peers(P2):concept_comentions 跨源共提对,直接查表零 LLM、静默 fail-open;
 · community_peers:base 库 Louvain 社区 + 廉价词法重排,miss 时 emit community_unavailable
   事件(绝不静默零召回)。
两路焦点解析共用 _resolve_focal(lower(canonical_name)==_norm(focal))。"""
from __future__ import annotations
from typing import List, Optional, Tuple


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _resolve_focal(db, notebook_id: str, focal_name: str) -> Optional[str]:
    """focal 名 → canonical_id(共提/社区两路共用的焦点解析:lower(canonical_name)==_norm(focal),
    多簇取成员最多者)。入参空 / 解析不到 → None。**不 emit 事件**——sibling_peers 走静默、
    community_peers 拿到 None 后自行补 community_unavailable 事件。"""
    key = _norm(focal_name)
    if not notebook_id or not key:
        return None
    row = db.execute(
        "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? AND lower(canonical_name)=? "
        "GROUP BY canonical_id ORDER BY COUNT(*) DESC LIMIT 1", (notebook_id, key)).fetchone()
    return row["canonical_id"] if row else None


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
        focal_can = _resolve_focal(db, base_nb, focal_name)
        if not focal_can:
            repo.event_log.emit({"kind": "community_unavailable", "notebook_id": base_nb,
                                 "reason": "focal_unresolved", "focal": focal_name})
            return []
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


def sibling_peers(repo, notebook_id: str, focal_name: str, *,
                  top_k: int = 8) -> List[Tuple[str, int]]:
    """共提兄弟:focal → canonical → concept_comentions 两侧按 bridge_claims 降序取对端名。

    P2 数据源(Task 3 的 concept_comentions,claim 文本确定性抽取的跨源共提对);相比
    Louvain community_peers(实测其把同型号/同族聚在一起,而非「同类可比对象」),共提对更贴
    横向对比语义。**纯查表、零 LLM、版本无关**(直接读表,不依赖任何 rebuild 时点);
    sibling_min_bridge 以下的弱共提对丢弃。

    notebook_id 语义:本原语 notebook 无关——查哪个库的 concept_comentions 由调用方决定。
    resolve_comparison_peers 传的是与 community_peers **同一个 BASE 库 id**(先
    first_base_notebook_id 解析),使共提/社区两路口径一致、prefer/fallback 是 like-for-like;
    未来其它调用方可指向活动库自身。返回 [(canonical_name, bridge_claims), ...] 降序。
    任何异常 / 焦点解析不到 / 无共提数据 → [](静默,不 emit;由调用方回退社区路径兜底文案)。"""
    try:
        min_b = int(getattr(repo.settings, "sibling_min_bridge", 2))
        with repo._connect() as db:
            cid = _resolve_focal(db, notebook_id, focal_name)
            if not cid:
                return []
            rows = db.execute(
                "SELECT canonical_a, canonical_b, bridge_claims FROM concept_comentions "
                "WHERE notebook_id=? AND (canonical_a=? OR canonical_b=?) AND bridge_claims>=? "
                "ORDER BY bridge_claims DESC LIMIT ?",
                (notebook_id, cid, cid, min_b, top_k)).fetchall()
            out: List[Tuple[str, int]] = []
            for r in rows:
                other = r["canonical_b"] if r["canonical_a"] == cid else r["canonical_a"]
                nm = db.execute(
                    "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? "
                    "AND canonical_id=? LIMIT 1", (notebook_id, other)).fetchone()
                if nm and nm["canonical_name"]:
                    out.append((nm["canonical_name"], int(r["bridge_claims"])))
            return out
    except Exception:
        return []


def resolve_comparison_peers(repo, base_nb: str, focal_name: str, query: str, *,
                             top_k: int, candidates: int) -> Tuple[List[str], str]:
    """对比兄弟解析(两处对比调用点共享):共提优先、社区回退。

    返回 (names, source),source ∈ {"comention", "community"}:
      · 先 sibling_peers(共提兄弟,零 LLM 直接查表);非空 → 用其名单,source="comention"。
      · 空 → 回退 community_peers(Louvain 社区),source="community",行为与今日逐字一致。
    两路都查同一个 BASE 库 id(调用方已 first_base_notebook_id 解析)→ 口径一致 like-for-like。
    sibling_peers 内部 fail-open→[] 时自动回退。**不吞 community_peers 异常**——与既有两调用点
    保持一致(expand_community 的 try/except、ask_chunk 的无兜底,都仍在各自调用点)。"""
    sib = sibling_peers(repo, base_nb, focal_name, top_k=top_k)
    if sib:
        return [name for name, _claims in sib], "comention"
    return community_peers(repo, base_nb, focal_name, query,
                           top_k=top_k, candidates=candidates), "community"
