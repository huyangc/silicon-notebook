"""Maximal Marginal Relevance 选择。检索召回里同一稠密主题会霸占前排,稀释
答案覆盖面;MMR 在"相关度"和"与已选集的新颖度"间折中,逐个挑选,打破通吃。"""
from __future__ import annotations
from typing import Callable, Dict, List


def mmr_rerank(cand_ids: List[str], relevance: Dict[str, float],
               pair_sim: Callable[[str, str], float],
               k: int, lambda_: float = 0.5) -> List[str]:
    """从 cand_ids 选出至多 k 个,平衡相关度与多样性。
    每步选 argmax( λ*rel(c) - (1-λ)*max_{s∈selected} sim(c,s) )。
    λ=1 退化为纯相关度排序;λ=0 为纯多样性。pair_sim(a,b) 返回 [0,1] 余弦。"""
    selected: List[str] = []
    remaining = list(cand_ids)
    while remaining and len(selected) < k:
        best, best_score = None, float("-inf")
        for c in remaining:
            rel = relevance.get(c, 0.0)
            div = max((pair_sim(c, s) for s in selected), default=0.0)
            score = lambda_ * rel - (1.0 - lambda_) * div
            if score > best_score:
                best, best_score = c, score
        selected.append(best)
        remaining.remove(best)
    return selected
