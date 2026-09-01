"""Content-private evidence for one model response contract failure."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import Iterator


_LIFECYCLE_LOCK = threading.RLock()
_LIFECYCLE_EPOCHS: dict[str, int] = {}


def current_model_artifact_lifecycle_epoch(notebook_id: str) -> int:
    """Snapshot the notebook generation shared by publication and redaction."""
    if not notebook_id:
        return 0
    with _LIFECYCLE_LOCK:
        return _LIFECYCLE_EPOCHS.get(notebook_id, 0)


@contextmanager
def model_artifact_publication_scope(notebook_id: str) -> Iterator[int]:
    """Serialize one case publication with notebook lifecycle cleanup."""
    with _LIFECYCLE_LOCK:
        yield _LIFECYCLE_EPOCHS.get(notebook_id, 0)


@contextmanager
def model_artifact_read_scope() -> Iterator[None]:
    """Keep a complete artifact read on the publication/redaction timeline."""
    with _LIFECYCLE_LOCK:
        yield


@contextmanager
def model_artifact_redaction_scope(notebook_id: str) -> Iterator[int]:
    """Invalidate in-flight generations before deleting retained content."""
    with _LIFECYCLE_LOCK:
        next_epoch = _LIFECYCLE_EPOCHS.get(notebook_id, 0) + 1
        _LIFECYCLE_EPOCHS[notebook_id] = next_epoch
        yield next_epoch


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
