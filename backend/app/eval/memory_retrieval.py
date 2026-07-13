"""Fixed Memory retrieval metrics and zero-tolerance isolation guards."""
from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


def _dcg(relevances: list[int]) -> float:
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(relevances))


def evaluate_cases(cases: Iterable[Mapping[str, Any]], k: int = 5) -> dict[str, float | int]:
    rows = list(cases)
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    candidate_leaks = cross_user = cross_notebook = 0
    for case in rows:
        expected = {str(item) for item in case.get("expected", [])}
        returned = list(case.get("returned", []))
        ranked_ids = [str(item.get("memory_id", "")) for item in returned[:k]]
        found = expected.intersection(ranked_ids)
        recalls.append(len(found) / len(expected) if expected else 1.0)
        first = next((index for index, item in enumerate(ranked_ids, 1) if item in expected), 0)
        reciprocal_ranks.append(1.0 / first if first else 0.0)
        rels = [1 if item in expected else 0 for item in ranked_ids]
        ideal = [1] * min(len(expected), k)
        denom = _dcg(ideal)
        ndcgs.append(_dcg(rels) / denom if denom else 1.0)
        for item in returned:
            if case.get("plane") == "notebook" and item.get("status") == "candidate":
                candidate_leaks += 1
            if str(item.get("created_by", "")) != str(case.get("user_id", "")):
                cross_user += 1
            if str(item.get("notebook_id", "")) != str(case.get("notebook_id", "")):
                cross_notebook += 1
    count = len(rows) or 1
    return {
        "recall_at_5": sum(recalls) / count,
        "mrr": sum(reciprocal_ranks) / count,
        "ndcg": sum(ndcgs) / count,
        "candidate_to_notebook_leaks": candidate_leaks,
        "cross_user_leaks": cross_user,
        "cross_notebook_leaks": cross_notebook,
    }


def run_fixed_gold(path: str | Path | None = None) -> dict[str, float | int]:
    gold_path = Path(path) if path else Path(__file__).with_name("memory_gold.yaml")
    # JSON is a strict YAML subset and avoids a runtime dependency for this
    # offline safety gate.
    payload = json.loads(gold_path.read_text(encoding="utf-8"))
    return evaluate_cases(payload.get("cases", []), k=5)


if __name__ == "__main__":  # pragma: no cover - manual evaluation entrypoint
    print(json.dumps(run_fixed_gold(), ensure_ascii=False, indent=2))
