from __future__ import annotations

import json
from typing import Any, List

_SCHEMA = (
    '{"decisions":[{"candidate_id":"","decision":"merge|keep_separate|unsure",'
    '"canonical_name":"","confidence":0.0,"rationale":""}]}'
)


def _prompt(candidates: List[dict]) -> str:
    lines = []
    for item in candidates:
        lines.append(
            f"- id={item['id']} score={item.get('score', 0):.3f}\n"
            f"  A: {item['canonical_a']}\n"
            f"  B: {item['canonical_b']}"
        )
    return (
        "Review candidate concept merges for a technical/scientific knowledge graph.\n"
        "Merge only when the two names denote the SAME concept, including "
        "acronym/full-name pairs (e.g. 'MoE' and 'Mixture-of-Experts') and trivial "
        "spelling/plural variants.\n"
        "Keep separate when one is a subtype, a different version/size/variant "
        "(e.g. 'V2' vs 'V3', '7B' vs '72B'), a related-but-distinct method, a "
        "parameter, a cause/effect, or a broader/narrower term.\n"
        "Return JSON only.\n\n"
        "Candidates:\n" + "\n".join(lines)
    )


def review_merge_candidates(llm_client: Any, candidates: List[dict]) -> List[dict]:
    if not getattr(llm_client, "configured", False) or not candidates:
        return []
    raw = llm_client.chat_json([{"role": "user", "content": _prompt(candidates)}], _SCHEMA)
    data = json.loads(raw)
    decisions = data.get("decisions") if isinstance(data, dict) else []
    out = []
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", "")).strip()
        if decision not in {"merge", "keep_separate", "unsure"}:
            continue
        out.append({
            "candidate_id": str(item.get("candidate_id", "")).strip(),
            "decision": decision,
            "canonical_name": str(item.get("canonical_name", "")).strip(),
            "confidence": float(item.get("confidence", 0) or 0),
            "rationale": str(item.get("rationale", "")).strip()[:500],
        })
    return [item for item in out if item["candidate_id"]]
