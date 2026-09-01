"""Content-private evidence for one model response contract failure."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable


_LIFECYCLE_READER_LOCK = threading.RLock()
_LIFECYCLE_EPOCH_READER: Callable[[str], int] | None = None


def set_model_artifact_lifecycle_epoch_reader(
    reader: Callable[[str], int] | None,
) -> None:
    """Install the storage-owned generation reader during runtime composition."""
    global _LIFECYCLE_EPOCH_READER
    with _LIFECYCLE_READER_LOCK:
        _LIFECYCLE_EPOCH_READER = reader


def current_model_artifact_lifecycle_epoch(notebook_id: str) -> int:
    """Snapshot the persisted notebook generation without affecting model work."""
    if not notebook_id:
        return 0
    with _LIFECYCLE_READER_LOCK:
        reader = _LIFECYCLE_EPOCH_READER
    if reader is None:
        return 0
    try:
        return int(reader(notebook_id))
    except Exception:
        # Diagnostics are optional. A failed snapshot uses a value no valid
        # persisted generation can equal, so publication later fails closed.
        return -1


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
    lifecycle_epoch: int | None = None
