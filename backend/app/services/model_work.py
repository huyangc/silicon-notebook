from __future__ import annotations

import contextvars
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import Callable, Iterator, Literal, Protocol

from app.core.request_context import request_user_id
from app.domain.model_artifacts import current_model_artifact_lifecycle_epoch


class ModelPriority(StrEnum):
    INTERACTIVE = "interactive"
    REPORT = "report"
    BACKGROUND = "background"


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


_DEADLINE_SECONDS = {
    ModelPriority.INTERACTIVE: 30.0,
    ModelPriority.REPORT: 300.0,
    ModelPriority.BACKGROUND: 1_800.0,
}


@dataclass(frozen=True)
class ModelWorkContext:
    actor_id: str
    workload_id: str
    priority: ModelPriority
    parent_id: str
    support_id: str
    deadline_at: float
    cancel_event: CancellationSignal | None
    notebook_id: str = ""
    question: str = ""
    artifact_lifecycle_epoch: int = 0


@dataclass(frozen=True)
class _ModelWorkScope:
    priority: ModelPriority
    parent_id: str
    actor_id: str
    notebook_id: str
    question: str
    artifact_lifecycle_epoch: int


@dataclass(frozen=True)
class _ModelArtifactScope:
    actor_id: str
    notebook_id: str
    question: str
    parent_id: str
    lifecycle_epoch: int


_CURRENT_SCOPE: contextvars.ContextVar[_ModelWorkScope | None] = (
    contextvars.ContextVar("model_work_scope", default=None)
)
_CURRENT_ARTIFACT_SCOPE: contextvars.ContextVar[_ModelArtifactScope | None] = (
    contextvars.ContextVar("model_artifact_scope", default=None)
)


def _artifact_lifecycle_epoch_for(notebook_id: str) -> int:
    """Reuse the oldest active snapshot for this notebook, if there is one."""
    if not notebook_id:
        return 0
    work_scope = _CURRENT_SCOPE.get()
    if work_scope is not None and work_scope.notebook_id == notebook_id:
        return work_scope.artifact_lifecycle_epoch
    artifact_scope = _CURRENT_ARTIFACT_SCOPE.get()
    if artifact_scope is not None and artifact_scope.notebook_id == notebook_id:
        return artifact_scope.lifecycle_epoch
    return current_model_artifact_lifecycle_epoch(notebook_id)


@contextmanager
def model_artifact_scope(
    *,
    actor_id: str = "",
    notebook_id: str = "",
    question: str = "",
    parent_id: str = "",
) -> Iterator[None]:
    """Attach notebook identity to any chat workload without changing priority."""
    previous = _CURRENT_ARTIFACT_SCOPE.get()
    effective_notebook = str(
        notebook_id or (previous.notebook_id if previous else "")
    )
    token = _CURRENT_ARTIFACT_SCOPE.set(_ModelArtifactScope(
        actor_id=str(actor_id or (previous.actor_id if previous else "")),
        notebook_id=effective_notebook,
        question=str(question or (previous.question if previous else "")),
        parent_id=str(parent_id or (previous.parent_id if previous else "")),
        lifecycle_epoch=_artifact_lifecycle_epoch_for(effective_notebook),
    ))
    try:
        yield
    finally:
        _CURRENT_ARTIFACT_SCOPE.reset(token)


@contextmanager
def model_work_scope(
    *,
    priority: ModelPriority,
    parent_id: str = "",
    actor_id: str = "",
    notebook_id: str = "",
    question: str = "",
) -> Iterator[None]:
    effective_notebook = str(notebook_id)
    token = _CURRENT_SCOPE.set(
        _ModelWorkScope(
            priority=ModelPriority(priority),
            parent_id=str(parent_id),
            actor_id=str(actor_id),
            notebook_id=effective_notebook,
            question=str(question),
            artifact_lifecycle_epoch=_artifact_lifecycle_epoch_for(
                effective_notebook
            ),
        )
    )
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


def notebook_model_artifact_scope(function: Callable) -> Callable:
    """Bind a notebook-aware service method before it materializes model input."""
    @wraps(function)
    def scoped(self, notebook_id: str, *args, **kwargs):
        with model_artifact_scope(notebook_id=str(notebook_id)):
            return function(self, notebook_id, *args, **kwargs)

    return scoped


def make_model_work_context(
    *,
    workload_id: str,
    priority: ModelPriority,
    parent_id: str = "",
    cancel_event: CancellationSignal | None = None,
    actor_id: str | None = None,
    support_id: str | None = None,
    deadline_at: float | None = None,
    clock: Callable[[], float] | None = None,
) -> ModelWorkContext:
    """Build one invocation context, inheriting only scope-owned metadata."""

    scope = _CURRENT_SCOPE.get()
    artifact_scope = _CURRENT_ARTIFACT_SCOPE.get()
    effective_priority = scope.priority if scope is not None else ModelPriority(priority)
    effective_parent = (
        scope.parent_id
        if scope is not None
        else (
            artifact_scope.parent_id
            if artifact_scope is not None and artifact_scope.parent_id
            else str(parent_id)
        )
    )
    effective_actor = (
        scope.actor_id
        if scope is not None and scope.actor_id
        else (
            artifact_scope.actor_id
            if artifact_scope is not None and artifact_scope.actor_id
            else actor_id
        )
    )
    effective_notebook = (
        scope.notebook_id
        if scope is not None and scope.notebook_id
        else (artifact_scope.notebook_id if artifact_scope is not None else "")
    )
    artifact_lifecycle_epoch = (
        scope.artifact_lifecycle_epoch
        if scope is not None and scope.notebook_id
        else (
            artifact_scope.lifecycle_epoch
            if artifact_scope is not None and artifact_scope.notebook_id
            else 0
        )
    )
    now = (clock or time.monotonic)()
    return ModelWorkContext(
        actor_id=str(effective_actor or request_user_id() or "system"),
        workload_id=str(workload_id),
        priority=effective_priority,
        parent_id=effective_parent,
        notebook_id=effective_notebook,
        question=(
            scope.question
            if scope is not None and scope.question
            else (artifact_scope.question if artifact_scope is not None else "")
        ),
        support_id=str(support_id or f"mdl-{secrets.token_urlsafe(12)}"),
        deadline_at=(
            float(deadline_at)
            if deadline_at is not None
            else now + _DEADLINE_SECONDS[effective_priority]
        ),
        cancel_event=cancel_event,
        artifact_lifecycle_epoch=artifact_lifecycle_epoch,
    )


class ModelSchedulingError(Exception):
    code = "model_scheduling_error"

    def __init__(
        self, message: str = "model work could not be scheduled", *, support_id: str = ""
    ) -> None:
        super().__init__(message)
        self.support_id = support_id


class ModelQueueFull(ModelSchedulingError):
    code = "model_queue_full"


class ModelQueueTimeout(ModelSchedulingError):
    code = "model_queue_timeout"


class ModelServiceUnavailable(ModelSchedulingError):
    code = "model_service_unavailable"


class ModelProviderError(Exception):
    def __init__(
        self,
        message: str = "model provider failed",
        *,
        code: str = "provider_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ModelNotConfiguredError(ModelProviderError):
    """A requested system workload has no physical service binding."""

    def __init__(self, message: str = "system model workload is not configured") -> None:
        super().__init__(message, code="model_not_configured")


class MalformedModelResponse(ModelProviderError):
    def __init__(self, message: str = "malformed model response") -> None:
        super().__init__(message, code="malformed_response")


@dataclass(frozen=True)
class ProviderObservation:
    service_id: str
    config_fingerprint: str
    status: Literal["ok", "error"]
    code: str
    trigger: Literal["manual_test", "observed_failure", "recovery_probe"]
    support_id: str
    latency_ms: int
    occurred_at: str


@dataclass(frozen=True)
class SchedulerSnapshot:
    active: int
    maximum: int
    queued: int
    oldest_wait_ms: int
    breaker_state: Literal["closed", "open", "half_open"]
    busy: bool
