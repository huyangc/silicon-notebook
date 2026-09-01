"""Content-private evidence for one model response contract failure."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MalformedModelInteraction:
    """Exact request/response pair rejected by the model JSON boundary.

    Instances are handed only to the private analysis-artifact store.  They
    must never be emitted through ordinary logs or content-free telemetry.
    """

    workload_id: str
    workload_label: str
    model_area: str
    failure_kind: str
    support_id: str
    actor_id: str
    parent_id: str
    notebook_id: str
    question: str
    messages: tuple[dict[str, str], ...]
    schema_hint: str
    response: str
    reason: str
    occurred_at: str
