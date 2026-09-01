from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
from typing import Any, Callable, List, Optional

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
        "Two names that are TRANSLATIONS of each other for the SAME concept (e.g. a "
        "Chinese name and its English equivalent or acronym — '低噪声放大器' / 'LNA' / "
        "'Low Noise Amplifier') SHOULD merge. canonical_name MUST be one of the input "
        "candidate strings, kept VERBATIM in its original language — never translate "
        "or invent a new form. Write the rationale in the candidates' language.\n"
        "Keep separate when one is a subtype, a different version/size/variant "
        "(e.g. 'V2' vs 'V3', '7B' vs '72B'), a related-but-distinct method, a "
        "parameter, a cause/effect, or a broader/narrower term.\n"
        "Return JSON only.\n\n"
        "Candidates:\n" + "\n".join(lines)
    )


def _to_float(v: Any) -> float:
    """Coerce an LLM-supplied confidence to a float; anything uncoercible → 0.0.

    LLMs routinely return categorical strings ('high'/'medium'), dicts, or lists
    for a numeric field. This must never raise — the value simply degrades to 0.0
    (below any auto-merge threshold), keeping the candidate pending.
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _review_chunk(llm_client: Any, chunk: List[dict]) -> List[dict]:
    """Adjudicate one chunk of candidates. NEVER raises — for ANY LLM output.

    Any failure — LLM/transport error, truncated or malformed JSON, non-dict
    payload, or a malformed individual decision — is logged/skipped and yields
    zero (or fewer) decisions for this chunk. Those candidates simply stay
    pending (safe, reviewable later); they must never be allowed to crash the
    rebuild or 500 the review endpoint.
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
    # decisions may be any JSON type (LLM deviation, e.g. {"decisions": 5}); only a
    # list is iterable as decisions — anything else contributes nothing.
    if not isinstance(decisions, list):
        decisions = []
    out: List[dict] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        try:
            decision = str(item.get("decision", "")).strip()
            if decision not in {"merge", "keep_separate", "unsure"}:
                continue
            out.append({
                "candidate_id": str(item.get("candidate_id", "")).strip(),
                "decision": decision,
                "canonical_name": str(item.get("canonical_name", "")).strip(),
                "confidence": _to_float(item.get("confidence", 0)),
                "rationale": str(item.get("rationale", "")).strip()[:500],
            })
        except Exception as err:  # noqa: BLE001 — a bad item never sinks the chunk
            logger.warning("merge-review: skipping malformed decision (%s)", err)
            continue
    return [item for item in out if item["candidate_id"]]


def review_merge_candidates(
    llm_client: Any,
    candidates: List[dict],
    batch_size: int = 30,
    max_workers: int = 1,
    on_chunk: Optional[Callable[[List[dict]], None]] = None,
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

    ``on_chunk``, if given, is called once per chunk with that chunk's decisions
    — always in the main thread, even for the concurrent path (callbacks never
    run inside a worker). Intended for incremental checkpointing; any exception
    it raises is swallowed and logged, never affecting the return value.
    """
    if not getattr(llm_client, "configured", False) or not candidates:
        return []
    step = max(1, int(batch_size or 30))
    chunks = [candidates[i:i + step] for i in range(0, len(candidates), step)]

    def _emit(decs: List[dict]) -> None:
        if on_chunk is None:
            return
        try:
            on_chunk(decs)
        except Exception as err:  # noqa: BLE001 — 持久化失败绝不能打断 fail-open 的 review
            logger.warning("merge-review: on_chunk 回调失败,已忽略 (%s)", err)

    out: List[dict] = []
    if max_workers > 1 and len(chunks) > 1:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(chunks)), thread_name_prefix="kg-review"
        ) as pool:
            futures = [
                pool.submit(
                    contextvars.copy_context().run,
                    _review_chunk,
                    llm_client,
                    chunk,
                )
                for chunk in chunks
            ]
            for fut in concurrent.futures.as_completed(futures):
                # _review_chunk is already total, but guard the future boundary too:
                # a worker exception must never re-raise out of this function.
                try:
                    decs = fut.result()
                except Exception:  # noqa: BLE001 — belt-and-suspenders
                    logger.warning("merge-review: chunk failed in worker; skipping")
                    continue
                _emit(decs)
                out.extend(decs)
    else:
        for chunk in chunks:
            decs = _review_chunk(llm_client, chunk)
            _emit(decs)
            out.extend(decs)
    return out
