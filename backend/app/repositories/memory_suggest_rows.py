"""Backend-neutral row helpers shared by both ``memory_suggest_store`` modules.

Pure functions over values the two stores have already read: the memory status
whitelist of the neighbour read, the projection of an answer's intent to the
paths a suggestion run may see (the clarification answers a finalized intent
persists), and the raw-cosine ranking of the vector-side
neighbour pool. Keeping them here is what keeps the two backends' answers
identical.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import numpy as np

from app.domain.vector_index import decode_vector

#: ``memory_items.status`` value domain (``app.models.memory.MemoryStatus``).
MEMORY_STATUSES = frozenset({"candidate", "confirmed", "rejected", "deprecated"})


def checked_statuses(statuses: Sequence[str]) -> tuple[str, ...]:
    """De-duplicated statuses, in order; any value outside ``MEMORY_STATUSES``
    raises ``ValueError`` (the neighbour read is called with an explicit set)."""
    values = tuple(dict.fromkeys(str(status) for status in statuses))
    unknown = [status for status in values if status not in MEMORY_STATUSES]
    if unknown:
        raise ValueError(f"unknown memory status: {unknown[0]!r}")
    return values


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def clarification_rows(value: Any, *, question_chars: int) -> list[dict[str, str]]:
    """A finalized intent's clarification answers as ``{id, question, answer}``
    rows (``query_intent.finalize_query_intent`` writes exactly these keys).
    The question is cut to ``question_chars``; the answer is the user's own
    text and stays whole (it is bounded by the ask request rail)."""
    limit = max(1, int(question_chars))
    return [
        {
            "id": str(item.get("id") or ""),
            "question": str(item.get("question") or "")[:limit],
            "answer": str(item.get("answer") or ""),
        }
        for item in _json_list(value)
        if isinstance(item, Mapping)
    ]


def cosine_top(
    query: Any, candidates: Sequence[tuple[str, str, str, Any]], k: int
) -> list[dict[str, Any]]:
    """The ``k`` candidates ``(id, title, status, raw_vector)`` with the highest
    raw cosine to ``query``; malformed or other-dimension vectors are skipped,
    ties break by id."""
    try:
        vector = np.asarray(query, dtype=np.float32)
    except (TypeError, ValueError):
        return []
    norm = float(np.linalg.norm(vector)) if vector.size else 0.0
    if norm == 0.0:
        return []
    scored = []
    for memory_id, title, status, raw in candidates:
        try:
            other = decode_vector(raw)
        except (TypeError, ValueError):
            continue
        if other is None or other.shape != vector.shape:
            continue
        other_norm = float(np.linalg.norm(other))
        if other_norm == 0.0:
            continue
        score = float(np.dot(vector, other) / (norm * other_norm))
        scored.append((-score, memory_id, title, status))
    scored.sort()
    return [
        {"id": memory_id, "title": title, "status": status, "side": "vector", "score": -neg}
        for neg, memory_id, title, status in scored[: max(0, int(k))]
    ]


def dedupe_neighbors(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """First occurrence of each memory id, in order (vector side first)."""
    seen: set[str] = set()
    kept = []
    for item in items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        kept.append(item)
    return kept


def json_list_value(raw: Any) -> list[Any]:
    """A JSON array read out of an answer payload (``citations`` /
    ``anchors``) as a list; anything else -- missing, null, an object, a
    scalar, text that is not JSON -- is ``[]``, the shape
    ``ask_state.answer_memory_source`` gives the manual save."""
    value = raw
    if isinstance(raw, (str, bytes)):
        try:
            value = json.loads(raw)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


def ordered_read_fence_delete_sql(where: str) -> str:
    """Account cleanup uses the same fence-key order as read-loss writes."""
    return (
        "WITH doomed AS MATERIALIZED (SELECT notebook_id, owner_id "
        f"FROM memory_suggest_read_fences WHERE {where} "
        'ORDER BY notebook_id COLLATE "C", owner_id COLLATE "C" FOR UPDATE) '
        "DELETE FROM memory_suggest_read_fences USING doomed "
        "WHERE memory_suggest_read_fences.notebook_id=doomed.notebook_id "
        "AND memory_suggest_read_fences.owner_id=doomed.owner_id"
    )


def ordered_job_delete_sql(where: str) -> str:
    """A multi-row ``DELETE FROM memory_suggest_jobs WHERE {where}`` that
    takes its row locks in primary-key order. Two such deletes over
    overlapping rows (one per notebook from a sharing change, one per owner
    from turning the switch off) would otherwise lock in their own scan
    orders and could deadlock; the ``LockRows`` node sits above the ``Sort``,
    so every caller locks ``(notebook_id, owner_id)`` ascending. ``where``
    names the table in full (``memory_suggest_jobs.owner_id``), never an
    alias.

    PostgreSQL only (SQLite serializes its write transactions and has no
    ``FOR UPDATE``). It lives in this backend-neutral module so the shared
    ``auth_store`` (account merge) can use it too; every bulk delete of the
    table goes through it (``tests/test_memory_suggest_job_delete_guard.py``,
    plan B10)."""
    return (
        "WITH doomed AS MATERIALIZED (SELECT notebook_id, owner_id "
        f"FROM memory_suggest_jobs WHERE {where} "
        "ORDER BY notebook_id, owner_id FOR UPDATE) "
        "DELETE FROM memory_suggest_jobs USING doomed "
        "WHERE memory_suggest_jobs.notebook_id = doomed.notebook_id "
        "AND memory_suggest_jobs.owner_id = doomed.owner_id"
    )
