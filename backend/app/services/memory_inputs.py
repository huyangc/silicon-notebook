"""Shared fail-closed normalization and size limits for Memory writes.

These functions are deliberately used by both Pydantic request models and the
Memory service so internal/MCP callers cannot bypass the HTTP boundary.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


MEMORY_TITLE_MAX_CHARS = 80
MEMORY_CONTENT_MAX_CHARS = 40_000
MEMORY_TAG_MAX_COUNT = 20
MEMORY_TAG_MAX_CHARS = 80
MEMORY_REASON_MAX_CHARS = 1_000
MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES = 8_192
MEMORY_EVIDENCE_MAX_COUNT = 50
MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES = 32_768
MEMORY_CLIENT_REQUEST_ID_MAX_CHARS = 200


class MemoryInputError(ValueError):
    """A caller-controlled Memory payload violated the shared contract."""


def normalize_text(
    value: Any, *, field: str, max_chars: int, required: bool = True
) -> str:
    if not isinstance(value, str):
        raise MemoryInputError(f"{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise MemoryInputError(f"{field} must not be blank")
    if len(normalized) > max_chars:
        raise MemoryInputError(f"{field} may contain at most {max_chars} characters")
    return normalized


def normalize_title(value: Any) -> str:
    return normalize_text(
        value, field="title", max_chars=MEMORY_TITLE_MAX_CHARS
    )


def normalize_content(value: Any) -> str:
    return normalize_text(
        value, field="content_md", max_chars=MEMORY_CONTENT_MAX_CHARS
    )


def normalize_reason(value: Any) -> str:
    return normalize_text(
        value or "", field="reason", max_chars=MEMORY_REASON_MAX_CHARS,
        required=False,
    )


def normalize_client_request_id(value: Any) -> str:
    return normalize_text(
        value,
        field="client_request_id",
        max_chars=MEMORY_CLIENT_REQUEST_ID_MAX_CHARS,
    )


def normalize_tags(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MemoryInputError("tags must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = normalize_text(
            raw, field="tag", max_chars=MEMORY_TAG_MAX_CHARS, required=False
        )
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
        if len(normalized) > MEMORY_TAG_MAX_COUNT:
            raise MemoryInputError(
                f"tags may contain at most {MEMORY_TAG_MAX_COUNT} values"
            )
    return normalized


def _json_size(value: Any, *, field: str) -> tuple[Any, int]:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise MemoryInputError(f"{field} must be JSON serializable") from exc
    return value, len(encoded.encode("utf-8"))


def normalize_task_context(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MemoryInputError("task_context must be an object")
    normalized = dict(value)
    _, size = _json_size(normalized, field="task_context")
    if size > MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES:
        raise MemoryInputError(
            "task_context serialized size may not exceed "
            f"{MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES} bytes"
        )
    return normalized


def normalize_evidence_refs(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MemoryInputError("evidence_refs must be a list")
    if len(value) > MEMORY_EVIDENCE_MAX_COUNT:
        raise MemoryInputError(
            f"evidence_refs may contain at most {MEMORY_EVIDENCE_MAX_COUNT} values"
        )
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MemoryInputError("each evidence reference must be an object")
        normalized.append(dict(item))
    _, size = _json_size(normalized, field="evidence_refs")
    if size > MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES:
        raise MemoryInputError(
            "evidence_refs serialized size may not exceed "
            f"{MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES} bytes"
        )
    return normalized
