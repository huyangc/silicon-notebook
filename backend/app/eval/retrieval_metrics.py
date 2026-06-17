"""Retrieval-quality metrics: recall@k and MRR over a notebook's KG retrieval.
Ground truth comes from optional `gold_object_ids` / `gold_relation_ids` fields
on each question; questions without either are skipped."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def recall_at_k(retrieved_ids: Sequence[str], gold_ids: Sequence[str],
                k: int) -> Optional[float]:
    gold = set(gold_ids)
    if not gold:
        return None
    topk = set(list(retrieved_ids)[:k])
    return len(topk & gold) / len(gold)


def mrr(retrieved_ids: Sequence[str], gold_ids: Sequence[str]) -> Optional[float]:
    gold = set(gold_ids)
    if not gold:
        return None   # undefined — consistent with recall_at_k
    for i, rid in enumerate(retrieved_ids):
        if rid in gold:
            return 1.0 / (i + 1)
    return 0.0


def run_recall(repo: Any, notebook_id: str, questions: List[Dict[str, Any]],
               k: int = 12) -> List[Dict[str, Any]]:
    """对带 gold_object_ids 或 gold_relation_ids 的题分别跑节点/关系检索,
    各算 recall@k + MRR。两者皆缺的题跳过。无 LLM 答案调用,便宜。"""
    rows: List[Dict[str, Any]] = []
    for q in questions:
        gold_obj = q.get("gold_object_ids")
        gold_rel = q.get("gold_relation_ids")
        if not gold_obj and not gold_rel:
            continue
        row: Dict[str, Any] = {"id": q.get("id", ""),
                               "track": q.get("track", ""), "bucket": q.get("bucket", "")}
        if gold_obj:
            ids = [h.object_id for h in repo._retrieve_scored(notebook_id, q["question"])]
            row["recall_at_k"] = recall_at_k(ids, gold_obj, k)
            row["mrr"] = mrr(ids, gold_obj)
            row["n_gold"] = len(gold_obj)
        if gold_rel:
            rids = [h.relation_id for h in repo._retrieve_relations_scored(notebook_id, q["question"])]
            row["relation_recall_at_k"] = recall_at_k(rids, gold_rel, k)
            row["relation_mrr"] = mrr(rids, gold_rel)
            row["n_gold_rel"] = len(gold_rel)
        rows.append(row)
    return rows
