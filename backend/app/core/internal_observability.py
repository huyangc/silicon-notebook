"""Keep deployment control state out of public payloads and user-facing logs."""
from __future__ import annotations

from typing import Any, Iterable, Mapping


INTERNAL_EVENT_KINDS = frozenset({"selected_source_graph"})
INTERNAL_TRACE_STEP_TYPES = frozenset({"source_subgraph"})


def is_internal_event(record: Mapping[str, Any] | object) -> bool:
    return isinstance(record, Mapping) and record.get("kind") in INTERNAL_EVENT_KINDS


def public_trace_steps(value: object) -> list[Any]:
    """Return a copy without rollout-only trace steps, including legacy rows."""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    visible: list[Any] = []
    for step in value:
        step_type = (
            step.get("step_type")
            if isinstance(step, Mapping)
            else getattr(step, "step_type", None)
        )
        if step_type not in INTERNAL_TRACE_STEP_TYPES:
            visible.append(step)
    return visible


def public_report_sections(value: object) -> list[Any]:
    """Remove legacy selected-source rollout receipts without mutating storage."""
    if not isinstance(value, list):
        return []
    visible: list[Any] = []
    for section in value:
        if isinstance(section, Mapping):
            visible.append(
                {key: item for key, item in section.items() if key != "source_graph"}
            )
        else:
            visible.append(section)
    return visible


def sanitize_answer_payload(payload: object) -> object:
    if not isinstance(payload, Mapping):
        return payload
    sanitized = dict(payload)
    sanitized.pop("source_graph", None)
    if "reasoning_trace" in sanitized:
        sanitized["reasoning_trace"] = public_trace_steps(
            sanitized.get("reasoning_trace")
        )
    return sanitized


__all__ = [
    "INTERNAL_EVENT_KINDS",
    "INTERNAL_TRACE_STEP_TYPES",
    "is_internal_event",
    "public_report_sections",
    "public_trace_steps",
    "sanitize_answer_payload",
]
