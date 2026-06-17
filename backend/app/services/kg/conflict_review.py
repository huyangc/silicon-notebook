"""LLM-based conflict adjudicator (Task 3).

Receives already-fetched candidate pairs with materialised context for both
sides, calls the LLM once per candidate to classify the conflict and decide
a resolution.  DB-free: does NOT query the repo, does NOT apply any changes.

Mirrors the structure of ``app.services.concept_merge_review``:
  - ``_prompt(item)``       — builds the user prompt for one candidate
  - ``review_conflict_candidates(llm_client, items)``  — main entry point
  - robust JSON parsing + safe-fallback on malformed output
  - temperature=0 via ``chat_json``
"""
from __future__ import annotations

import json
from typing import Any, List

# ---------------------------------------------------------------------------
# Schema hint shown to the LLM (mirrors concept_merge_review's _SCHEMA)
# ---------------------------------------------------------------------------
_SCHEMA = (
    '{"conflict_type":"none|mutual|temporal|granularity",'
    '"resolution":"keep|discard|modify",'
    '"winner_ref":"<left_ref or right_ref or null>",'
    '"resolved_payload":null,'
    '"confidence":0.0,'
    '"rationale":""}'
)

_VALID_CONFLICT_TYPES = {"none", "mutual", "temporal", "granularity"}
_VALID_RESOLUTIONS = {"keep", "discard", "modify"}


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _prompt(item: dict) -> str:
    cand = item["candidate"]
    left = item["left"]
    right = item["right"]

    return (
        "You are adjudicating a potential conflict in a bilingual (Chinese/English) "
        "knowledge graph for a technical domain (IC design, engineering, science, etc.).\n\n"
        "Given two knowledge-graph entries that may conflict, classify the conflict type "
        "and decide a resolution.  Use the source passages as evidence.\n\n"
        "Conflict types:\n"
        "  - mutual (互斥): same subject+predicate, mutually exclusive objects "
        "(e.g. birthplace=Shanghai vs birthplace=Beijing).  "
        "Resolution: discard the wrong side; set winner_ref to the ref of the KEPT side.\n"
        "  - temporal (时序): predicate is time-dependent, each claim holds in a different "
        "time window.  "
        "Resolution: modify — produce resolved_payload that scopes the statement with its "
        "time range (e.g. append \"[2000–2005]\").\n"
        "  - granularity (粒度): different specificity but compatible "
        "(e.g. Shanghai vs China).  "
        "Resolution: modify — annotate granularity; both can coexist.\n"
        "  - none (无冲突): not actually a conflict (corroborating/paraphrase/unrelated).  "
        "Resolution: keep, winner_ref null.\n\n"
        "Rules:\n"
        "  - resolved_payload is only relevant for resolution=modify; set to null otherwise.\n"
        "  - winner_ref must be exactly one of the two refs shown below, or null.\n"
        "  - confidence is 0.0–1.0; use lower values when evidence is ambiguous.\n"
        "  - Return a single JSON object matching the schema.  No extra text.\n\n"
        f"Signal: {cand['signal']}\n"
        f"Left ref:  {cand['left_ref']}\n"
        f"Right ref: {cand['right_ref']}\n\n"
        f"--- LEFT (tier={left['tier']}, type={left.get('object_type')}) ---\n"
        f"Statement: {left['text']}\n"
        f"Source:    {left['source_text'] or '(none)'}\n\n"
        f"--- RIGHT (tier={right['tier']}, type={right.get('object_type')}) ---\n"
        f"Statement: {right['text']}\n"
        f"Source:    {right['source_text'] or '(none)'}\n"
    )


# ---------------------------------------------------------------------------
# Safe fallback verdict (mirrors concept_merge_review's default-on-error)
# ---------------------------------------------------------------------------

def _fallback(item: dict) -> dict:
    cand = item["candidate"]
    return {
        "left_ref": cand["left_ref"],
        "right_ref": cand["right_ref"],
        "conflict_type": "none",
        "resolution": "keep",
        "winner_ref": None,
        "resolved_payload": None,
        "confidence": 0.0,
        "rationale": "LLM output could not be parsed; defaulting to no-conflict.",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def review_conflict_candidates(llm_client: Any, items: List[dict]) -> List[dict]:
    """Adjudicate each conflict candidate via the LLM.

    Parameters
    ----------
    llm_client:
        An LLM client with ``configured: bool`` and
        ``chat_json(messages, schema_hint) -> str`` (temperature=0 is
        expected to be the default, matching concept_merge_review).
    items:
        List of dicts in the input contract described in the module docstring.

    Returns
    -------
    List of verdict dicts (one per input item), or empty list when
    ``llm_client.configured`` is False or ``items`` is empty.
    """
    if not getattr(llm_client, "configured", False) or not items:
        return []

    results: List[dict] = []
    for item in items:
        cand = item["candidate"]
        verdict = _fallback(item)

        try:
            raw = llm_client.chat_json(
                [{"role": "user", "content": _prompt(item)}],
                _SCHEMA,
            )
        except Exception:
            # A transient LLM error on one item must not abort the rest of the
            # batch (this runs as a background pass over many candidates).
            results.append(verdict)
            continue

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            results.append(verdict)
            continue

        if not isinstance(data, dict):
            results.append(verdict)
            continue

        conflict_type = str(data.get("conflict_type", "")).strip()
        if conflict_type not in _VALID_CONFLICT_TYPES:
            conflict_type = "none"

        resolution = str(data.get("resolution", "")).strip()
        if resolution not in _VALID_RESOLUTIONS:
            resolution = "keep"

        # winner_ref must be one of the two known refs or None
        raw_winner = data.get("winner_ref")
        if raw_winner in (cand["left_ref"], cand["right_ref"]):
            winner_ref: str | None = raw_winner
        else:
            winner_ref = None

        # resolved_payload only makes sense for modify
        raw_payload = data.get("resolved_payload")
        if resolution == "modify" and isinstance(raw_payload, dict):
            resolved_payload: dict | None = raw_payload
        else:
            resolved_payload = None

        try:
            confidence = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        rationale = str(data.get("rationale", "")).strip()[:500]

        results.append({
            "left_ref": cand["left_ref"],
            "right_ref": cand["right_ref"],
            "conflict_type": conflict_type,
            "resolution": resolution,
            "winner_ref": winner_ref,
            "resolved_payload": resolved_payload,
            "confidence": confidence,
            "rationale": rationale,
        })

    return results
