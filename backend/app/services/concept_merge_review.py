from __future__ import annotations

import concurrent.futures
import json
import logging
from typing import Any, List

logger = logging.getLogger(__name__)

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


def _review_chunk(llm_client: Any, chunk: List[dict]) -> List[dict]:
    """Adjudicate one chunk of candidates. NEVER raises.

    Any failure — LLM/transport error, truncated or malformed JSON, non-dict
    payload — is logged and yields zero decisions for this chunk. Those
    candidates simply stay pending (safe, reviewable later); they must never be
    allowed to crash the rebuild.
    """
    try:
        raw = llm_client.chat_json([{"role": "user", "content": _prompt(chunk)}], _SCHEMA)
        data = json.loads(raw)
    except Exception as err:  # noqa: BLE001 — fail-open by design
        logger.warning(
            "merge-review: chunk of %d candidates failed (%s); skipping — they stay pending",
            len(chunk), err,
        )
        return []
    decisions = data.get("decisions") if isinstance(data, dict) else []
    out: List[dict] = []
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


def review_merge_candidates(
    llm_client: Any,
    candidates: List[dict],
    batch_size: int = 30,
    max_workers: int = 1,
) -> List[dict]:
    """LLM-adjudicate concept-merge candidates in bounded chunks. NEVER raises.

    Splits ``candidates`` into chunks of ``batch_size`` so a large candidate set
    can't produce one oversized LLM reply that gets truncated → invalid JSON.
    Each chunk is parsed defensively (:func:`_review_chunk`); a failing chunk
    contributes no decisions and its candidates stay pending. With
    ``max_workers > 1`` and multiple chunks, chunks run concurrently.

    ``llm_client`` is used as-passed inside worker threads — the caller resolves
    any ContextVar-backed client in the main thread; we must not re-resolve it
    from a worker (the request-user ContextVar is not copied into raw threads).
    """
    if not getattr(llm_client, "configured", False) or not candidates:
        return []
    step = max(1, int(batch_size))
    chunks = [candidates[i:i + step] for i in range(0, len(candidates), step)]

    out: List[dict] = []
    if max_workers > 1 and len(chunks) > 1:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(chunks)), thread_name_prefix="kg-review"
        ) as pool:
            futures = [pool.submit(_review_chunk, llm_client, chunk) for chunk in chunks]
            for fut in concurrent.futures.as_completed(futures):
                out.extend(fut.result())
    else:
        for chunk in chunks:
            out.extend(_review_chunk(llm_client, chunk))
    return out
