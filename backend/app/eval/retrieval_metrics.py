"""Retrieval-quality metrics: recall@k and MRR over a notebook's KG retrieval.
Ground truth comes from an optional `gold_object_ids` field on each question;
questions without it are skipped (retrieval recall needs a labeled hit set, which
questions.yaml does not carry by default)."""
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
    """For each question carrying `gold_object_ids`, run KG retrieval and score
    recall@k + MRR. Uses _retrieve_scored (keyword + semantic if an embedder is
    configured); no LLM answer call, so it is cheap to run."""
    rows: List[Dict[str, Any]] = []
    for q in questions:
        gold = q.get("gold_object_ids")
        if not gold:
            continue
        hits = repo._retrieve_scored(notebook_id, q["question"])
        ids = [h.object_id for h in hits]
        rows.append({
            "id": q.get("id", ""),
            "recall_at_k": recall_at_k(ids, gold, k),
            "mrr": mrr(ids, gold),
            "n_gold": len(gold),
            "n_retrieved": len(ids),
        })
    return rows
