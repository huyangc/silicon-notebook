"""Content-free Deep Report operational events.

Only stable stage names, opaque report ids, indices, counts, booleans and wall
clock durations are accepted here.  User questions, section titles, retrieval
queries, source ids and evidence must never reach this boundary.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

from app.services.cancellation import AskCancelled


_TIMING_META = frozenset({
    "section_index",
    "sections",
    "topics",
    "failed",
    "cancelled",
})
_ATTEMPT_STATUS = frozenset({"success", "empty", "error", "cancelled"})
_STAGES = frozenset({
    "planning_intent",
    "planning_corpus_profile",
    "planning_intent_probe",
    "planning_corpus_map",
    "planning_outline_model",
    "planning_sufficiency_probe",
    "planning_sufficiency_judge",
    "retrieve",
    "synthesis",
    "draft",
    "final_editor",
})


def emit_stage_timing(event_log: Any, *, report_id: str, stage: str,
                      started: float, **metadata: Any) -> None:
    """Emit a fail-open report stage timing with a narrow payload allowlist."""
    event = {
        "kind": "report_stage_timing",
        "report_id": str(report_id),
        "stage": stage if stage in _STAGES else "unknown",
        "ms": max(0, int((time.monotonic() - started) * 1000)),
    }
    for key, value in metadata.items():
        if key not in _TIMING_META:
            continue
        if isinstance(value, bool):
            event[key] = value
        elif isinstance(value, int):
            event[key] = value
    try:
        event_log.emit(event)
    except Exception:
        pass


def emit_section_attempt(event_log: Any, *, report_id: str,
                         section_index: int, attempt: int, status: str,
                         started: float) -> None:
    """Record one outer section-generation attempt without model/user text."""
    safe_status = status if status in _ATTEMPT_STATUS else "error"
    try:
        event_log.emit({
            "kind": "report_section_attempt",
            "report_id": str(report_id),
            "section_index": int(section_index),
            "attempt": int(attempt),
            "status": safe_status,
            "ms": max(0, int((time.monotonic() - started) * 1000)),
        })
    except Exception:
        pass


@contextmanager
def observe_stage(event_log: Any, *, report_id: str, stage: str,
                  **metadata: Any):
    """Time one stage, retaining the original success/exception semantics."""
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        emit_stage_timing(
            event_log,
            report_id=report_id,
            stage=stage,
            started=started,
            **(
                {"cancelled": True}
                if isinstance(exc, AskCancelled) else {"failed": True}
            ),
            **metadata,
        )
        raise
    else:
        emit_stage_timing(
            event_log,
            report_id=report_id,
            stage=stage,
            started=started,
            **metadata,
        )


__all__ = ["emit_section_attempt", "emit_stage_timing", "observe_stage"]
