"""对比检索原语:焦点实体 → 兄弟实体名(横向对比候选)。

两条数据源,统一由 resolve_comparison_peers「共提优先、社区回退」编排:
 · sibling_peers(P2):concept_comentions 跨源共提对,直接查表零 LLM、静默 fail-open;
 · community_peers:base 库 Louvain 社区 + 廉价词法重排,miss 时 emit community_unavailable
   事件(绝不静默零召回)。
两路焦点解析共用同一归一化 + UnifiedKgStore.resolve_focal(Task 13:本模块只保留
编排/重排/事件,持久化读一律走注入的 CommunityQueryService.unified_kg)。"""
from __future__ import annotations
from typing import List, Optional, Tuple


class CommunityQueryService:
    def __init__(self, *, notebooks, unified_kg, event_log,
                 sibling_min_bridge: int = 2) -> None:
        self.notebooks = notebooks
        self.unified_kg = unified_kg
        self.event_log = event_log
        self.sibling_min_bridge = int(sibling_min_bridge)

    def mounted_base_ids(self, active_notebook_id: str) -> List[str]:
        return self.unified_kg.mounted_base_ids(active_notebook_id)

    def community_peers(self, base_notebook_id: str, focal_name: str,
                        question: str, *, top_k: int,
                        candidates: int) -> List[str]:
        from app.services.retrieval import keyword_score
        key = _norm(focal_name)
        if not base_notebook_id or not key:
            return []
        focal = _resolve_focal(
            self.unified_kg, base_notebook_id, focal_name
        )
        if not focal:
            self.event_log.emit({
                "kind": "community_unavailable",
                "notebook_id": base_notebook_id,
                "reason": "focal_unresolved",
                "focal": focal_name,
            })
            return []
        community_id = self.unified_kg.top_community_for(
            base_notebook_id, focal
        )
        if not community_id:
            self.event_log.emit({
                "kind": "community_unavailable",
                "notebook_id": base_notebook_id,
                "reason": "not_built",
                "focal": focal_name,
            })
            return []
        rows = self.unified_kg.community_member_peers(
            base_notebook_id, community_id, focal, candidates
        )
        ranked = sorted(
            rows,
            key=lambda row: (
                keyword_score(question, row["canonical_name"] or ""),
                row["centrality"],
            ),
            reverse=True,
        )
        seen, out = set(), []
        for row in ranked:
            name = (row["canonical_name"] or "").strip()
            normalized = _norm(name)
            if name and normalized not in seen:
                seen.add(normalized)
                out.append(name)
            if len(out) >= top_k:
                break
        return out

    def sibling_peers(self, notebook_id: str, focal_name: str, *,
                      top_k: int = 8) -> List[Tuple[str, int]]:
        try:
            focal = _resolve_focal(
                self.unified_kg, notebook_id, focal_name
            )
            if not focal:
                return []
            return self.unified_kg.comention_peers(
                notebook_id, focal, self.sibling_min_bridge, top_k
            )
        except Exception:
            return []

    def resolve_comparison_peers(self, base_notebook_id: str, focal_name: str,
                                 question: str, *, top_k: int,
                                 candidates: int) -> Tuple[List[str], str]:
        siblings = self.sibling_peers(
            base_notebook_id, focal_name, top_k=top_k
        )
        if siblings:
            return [name for name, _claims in siblings], "comention"
        return self.community_peers(
            base_notebook_id, focal_name, question,
            top_k=top_k, candidates=candidates,
        ), "community"


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _resolve_focal(store, notebook_id: str, focal_name: str) -> Optional[str]:
    """focal 名 → canonical_id(共提/社区两路共用的焦点解析:lower(canonical_name)==_norm(focal),
    多簇取成员最多者)。入参空 / 解析不到 → None。**不 emit 事件**——sibling_peers 走静默、
    community_peers 拿到 None 后自行补 community_unavailable 事件。"""
    key = _norm(focal_name)
    if not notebook_id or not key:
        return None
    return store.resolve_focal(notebook_id, key)


def mounted_base_ids(queries, active_nb: str) -> List[str]:
    return queries.mounted_base_ids(active_nb)


def community_peers(queries, base_nb: str, focal_name: str, query: str, *,
                    top_k: int, candidates: int) -> List[str]:
    return queries.community_peers(
        base_nb, focal_name, query, top_k=top_k, candidates=candidates
    )


def sibling_peers(queries, notebook_id: str, focal_name: str, *,
                  top_k: int = 8) -> List[Tuple[str, int]]:
    """共提兄弟:focal → canonical → concept_comentions 两侧按 bridge_claims 降序取对端名。

    P2 数据源(Task 3 的 concept_comentions,claim 文本确定性抽取的跨源共提对);相比
    Louvain community_peers(实测其把同型号/同族聚在一起,而非「同类可比对象」),共提对更贴
    横向对比语义。**纯查表、零 LLM、版本无关**(直接读表,不依赖任何 rebuild 时点);
    sibling_min_bridge 以下的弱共提对丢弃。

    notebook_id 语义:本原语 notebook 无关——查哪个库的 concept_comentions 由调用方决定。
    resolve_comparison_peers 传的是与 community_peers **同一个 BASE 库 id**(调用方对
    mounted_base_ids 的结果逐个传入),使共提/社区两路口径一致、prefer/fallback 是 like-for-like;
    未来其它调用方可指向活动库自身。返回 [(canonical_name, bridge_claims), ...] 降序。
    任何异常 / 焦点解析不到 / 无共提数据 → [](静默,不 emit;由调用方回退社区路径兜底文案)。"""
    return queries.sibling_peers(
        notebook_id, focal_name, top_k=top_k
    )


def resolve_comparison_peers(queries, base_nb: str, focal_name: str, query: str, *,
                             top_k: int, candidates: int) -> Tuple[List[str], str]:
    """对比兄弟解析(两处对比调用点共享):共提优先、社区回退。

    返回 (names, source),source ∈ {"comention", "community"}:
      · 先 sibling_peers(共提兄弟,零 LLM 直接查表);非空 → 用其名单,source="comention"。
      · 空 → 回退 community_peers(Louvain 社区),source="community",行为与今日逐字一致。
    两路都查同一个 BASE 库 id(调用方对 mounted_base_ids 逐个传入)→ 口径一致 like-for-like。
    sibling_peers 内部 fail-open→[] 时自动回退。**不吞 community_peers 异常**——与既有两调用点
    保持一致(expand_community 的 try/except、ask_chunk 的无兜底,都仍在各自调用点)。"""
    siblings = sibling_peers(
        queries, base_nb, focal_name, top_k=top_k
    )
    if siblings:
        return [name for name, _claims in siblings], "comention"
    return community_peers(
        queries, base_nb, focal_name, query,
        top_k=top_k, candidates=candidates,
    ), "community"
