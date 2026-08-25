from __future__ import annotations

import contextvars
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from app.core.config import Settings
from app.core.ask_context import _ASK_MODEL_ERRORS
from app.core.llm import OpenAICompatibleClient
from app.core.llm_logging import (
    current_interaction_support_id,
    interaction_support_scope,
)
from app.core.model_json import ModelJsonRepairError, parse_model_json_object
from app.core.model_safety import (
    MODEL_ERROR_MISSING_CONFIG,
    MODEL_ERROR_UPSTREAM,
    safe_model_display_name,
    safe_model_error_code,
    safe_model_error_stage,
    safe_model_label,
    safe_model_metadata_id,
    safe_model_support_id,
)
from app.services.cancellation import AskCancelled
from app.services.embedding import FakeEmbedder
from app.services.model_registry import (
    ModelServiceDefinition,
    SystemModelServiceRegistry,
    WorkloadSpec,
)
from app.services.model_circuit_breaker import (
    FailureKind,
    classify_provider_failure,
)
from app.services.model_scheduler import ServiceScheduler
from app.services.model_work import (
    MalformedModelResponse,
    ModelPriority,
    ModelProviderError,
    ModelSchedulingError,
    ModelServiceUnavailable,
    ProviderObservation,
    SchedulerSnapshot,
    make_model_work_context,
)
from app.services.rerank_client import RerankClient


_WORKER_ENVIRONMENT_VARIABLES = ("WEB_CONCURRENCY", "UVICORN_WORKERS")
_MODEL_CONFIG_RELOAD_INTERVAL_SECONDS = 1.0
logger = logging.getLogger("silicon_notebook.model_provider")

_JSON_REPAIR_WORKLOADS = frozenset({"reasoning_agent", "ask_answer"})


def validate_process_local_scheduler_deployment(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> None:
    """Reject launch settings that would multiply process-local service caps."""
    values = os.environ if environ is None else environ
    for name in _WORKER_ENVIRONMENT_VARIABLES:
        raw = (values.get(name) or "").strip()
        if not raw:
            continue
        try:
            workers = int(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"{name} must be 1 because the model scheduler is process-local"
            ) from exc
        if workers != 1:
            raise RuntimeError(
                f"{name} must be 1 because the model scheduler is process-local"
            )


class ModelInvocationError(ModelProviderError):
    """Credential-safe failure metadata for one workload invocation."""

    def __init__(
        self,
        *,
        service: ModelServiceDefinition,
        workload: WorkloadSpec,
        code: str,
        support_id: str,
        status_code: int | None = None,
    ) -> None:
        self.service_id = service.id
        self.service_name = service.display_name
        self.workload_id = workload.id
        self.workload_label = workload.display_label
        self.model = service.model
        self.support_id = support_id
        super().__init__(
            (
                f"{self.service_name} / {self.workload_label} failed "
                f"({code}; support {support_id})"
            ),
            code=code,
            status_code=status_code,
        )


class _UnconfiguredChatClient:
    configured = False
    model = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def chat_json(self, *args, **kwargs):
        raise ModelProviderError(
            "system model workload is not configured", code="model_not_configured"
        )


class _UnconfiguredRerankClient:
    configured = False

    def rerank(self, query: str, documents: list[str], on_error=None) -> list[int]:
        del query, on_error
        return list(range(len(documents)))


class _UnconfiguredEmbedder:
    configured = False

    def __init__(self, dim: int) -> None:
        self._delegate = FakeEmbedder(dim=dim)
        self.dim = self._delegate.dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._delegate.embed_texts(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._delegate.embed_query(text)


@dataclass
class _ServiceRuntime:
    service: ModelServiceDefinition
    scheduler: ServiceScheduler
    raw: Any


@dataclass
class _SubmittedCall:
    runtime: _ServiceRuntime
    context: Any
    future: Future
    queued_at: float
    timing: dict[str, Any]
    breaker_before: str
    observe: bool


def _status_code(error: BaseException) -> int | None:
    raw = getattr(error, "status_code", None)
    if raw is None:
        raw = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _stable_error_code(error: BaseException) -> str:
    if isinstance(error, ModelSchedulingError):
        return error.code
    if isinstance(error, MalformedModelResponse):
        return error.code
    status = _status_code(error)
    if status in (401, 403):
        return "provider_auth"
    if status == 429:
        return "provider_rate_limited"
    if status is not None and status >= 500:
        return "provider_unavailable"
    code = str(getattr(error, "code", "") or "").strip().lower()
    if code in {
        "unknown_model",
        "model_not_found",
        "model_rejected",
        "protocol_mismatch",
        "unsupported_protocol",
        "capability_mismatch",
        "unsupported_capability",
    }:
        return code
    if isinstance(error, (ConnectionError, TimeoutError)) or "timeout" in type(error).__name__.lower():
        return "provider_unavailable"
    return "provider_error"


def _safe_status_class(error: BaseException) -> str:
    status = _status_code(error)
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "server_error"
    if isinstance(error, MalformedModelResponse):
        return "malformed_response"
    if isinstance(error, ModelSchedulingError):
        return "scheduler"
    return "provider_error"


def _validate_json_object(content: Any) -> str:
    """Compatibility strict validator retained for direct tests/callers."""
    try:
        return parse_model_json_object(content, "", allow_repair=False).content
    except ModelJsonRepairError as exc:
        raise MalformedModelResponse() from exc


def _validate_rerank_rows(rows: Any, document_count: int) -> list[dict]:
    if not isinstance(rows, list):
        raise MalformedModelResponse()
    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise MalformedModelResponse()
        index = row.get("index")
        score = row.get("relevance_score", row.get("score"))
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < document_count
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
        ):
            raise MalformedModelResponse()
        normalized.append({"index": index, "relevance_score": float(score)})
    return normalized


def _invocation_error(
    runtime: _ServiceRuntime,
    workload: WorkloadSpec,
    call: _SubmittedCall,
    error: BaseException,
) -> ModelInvocationError:
    if isinstance(error, ModelInvocationError):
        return error
    return ModelInvocationError(
        service=runtime.service,
        workload=workload,
        code=_stable_error_code(error),
        support_id=call.context.support_id,
        status_code=_status_code(error),
    )


class _ScheduledAdapter:
    def __init__(
        self,
        provider: "RuntimeModelProvider",
        workload: WorkloadSpec,
        *,
        initial_runtime: _ServiceRuntime | None = None,
        pinned_runtime: _ServiceRuntime | None = None,
    ) -> None:
        self._provider = provider
        self._workload = workload
        self._pinned_runtime = pinned_runtime
        # Retain the most recently resolved generation for close/admission
        # compatibility.  Normal calls refresh it from the live registry; once
        # provider close begins, routing through this scheduler preserves the
        # existing typed rejection path instead of leaking a raw close error.
        self._runtime = pinned_runtime or initial_runtime

    def _current_runtime(self) -> _ServiceRuntime | None:
        if self._pinned_runtime is not None:
            return self._pinned_runtime
        if (
            self._provider._closing or self._provider._closed
        ) and self._runtime is not None:
            return self._runtime
        self._runtime = self._provider._runtime_for_workload(self._workload)
        return self._runtime

    def _submit(
        self,
        runtime: _ServiceRuntime,
        invoke: Callable[[], Any],
        *,
        cancel_event: Any = None,
        actor_id: str | None = None,
        parent_id: str = "",
        support_id: str | None = None,
        observe: bool = True,
    ) -> _SubmittedCall:
        context = make_model_work_context(
            workload_id=self._workload.id,
            priority=ModelPriority(self._workload.default_priority),
            cancel_event=cancel_event,
            actor_id=actor_id,
            parent_id=parent_id,
            support_id=support_id,
        )
        timing: dict[str, Any] = {}
        queued_at = time.perf_counter()
        breaker_before = runtime.scheduler.snapshot().breaker_state
        submission_context = contextvars.copy_context()

        def scheduled() -> Any:
            timing["started"] = time.perf_counter()
            try:
                def run_in_submission_context() -> Any:
                    with interaction_support_scope(context.support_id):
                        return invoke()

                return submission_context.run(run_in_submission_context)
            finally:
                timing["finished"] = time.perf_counter()
                if observe:
                    self._provider._mark_completion(
                        runtime.service.id, timing
                    )

        future = self._provider._submit_scheduled(
            runtime,
            context=context,
            invoke=scheduled,
        )
        call = _SubmittedCall(
            runtime=runtime,
            context=context,
            future=future,
            queued_at=queued_at,
            timing=timing,
            breaker_before=breaker_before,
            observe=observe,
        )
        if observe:
            self._provider._track_completion(
                runtime, self._workload, call
            )
        return call

    def _resolve(self, call: _SubmittedCall) -> Any:
        error: BaseException | None = None
        result: Any = None
        try:
            result = call.future.result()
        except CancelledError:
            self._emit(call, status="cancelled", error=None)
            if (
                call.context.cancel_event is not None
                and call.context.cancel_event.is_set()
            ):
                raise AskCancelled()
            raise
        except AskCancelled:
            self._emit(call, status="cancelled", error=None)
            raise
        except BaseException as exc:
            error = exc
            self._emit(call, status="error", error=exc)
        else:
            self._emit(call, status="ok", error=None)
            return result

        assert error is not None
        typed = _invocation_error(call.runtime, self._workload, call, error)
        raise typed from error

    def _emit(
        self,
        call: _SubmittedCall,
        *,
        status: str,
        error: BaseException | None,
    ) -> None:
        now = time.perf_counter()
        started = call.timing.get("started", now)
        finished = call.timing.get("finished", now)
        runtime = call.runtime
        breaker_after = runtime.scheduler.snapshot().breaker_state
        event = {
            "kind": "model_scheduler",
            "status": status,
            "support_id": call.context.support_id,
            "workload_id": self._workload.id,
            "workload_label": self._workload.display_label,
            "service_id": runtime.service.id,
            "service_name": runtime.service.display_name,
            "model": runtime.service.model,
            "actor_id": call.context.actor_id,
            "parent_id": call.context.parent_id,
            "priority": call.context.priority.value,
            "queue_latency_ms": max(0, round((started - call.queued_at) * 1_000)),
            "execution_latency_ms": max(0, round((finished - started) * 1_000)),
            "retry_outcome": {
                "ok": "succeeded",
                "error": "failed",
                "cancelled": "cancelled",
            }[status],
            "breaker_transition": f"{call.breaker_before}->{breaker_after}",
            "upstream_status_class": _safe_status_class(error) if error else "ok",
        }
        try:
            self._provider.event_log.emit(event)
        except Exception:
            pass


class ScheduledJsonChatClient(_ScheduledAdapter):
    settings: Settings

    def __init__(
        self, provider, workload, *, initial_runtime=None, pinned_runtime=None
    ) -> None:
        super().__init__(
            provider,
            workload,
            initial_runtime=initial_runtime,
            pinned_runtime=pinned_runtime,
        )
        self.settings = provider.settings

    @property
    def configured(self) -> bool:
        return self._provider.configured(self._workload.id)

    @property
    def model(self) -> str:
        service = self._provider.registry.service_for(self._workload.id)
        return service.model if service is not None else ""

    def _emit_json_repair_event(self, *, status: str, reason: str) -> None:
        try:
            self._provider.event_log.emit({
                "kind": "model_json_repair",
                "status": status,
                "workload_id": self._workload.id,
                "support_id": current_interaction_support_id(),
                "reason": reason,
            })
        except Exception:
            # Diagnostics must never change whether a model response succeeds.
            pass

    def chat_json(
        self,
        messages,
        response_schema_hint,
        *,
        timeout=None,
        max_retries=None,
        temperature=1.0,
        top_p=1.0,
        max_tokens=None,
        cancel_event=None,
        bypass_cache=False,
        response_validator: Callable[[str], bool] | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
    ) -> str:
        # Freeze the physical runtime and its workload policy from the same
        # hot-reloaded registry generation.  A queued call may then finish on
        # that route even if the TOML changes while it waits for service capacity.
        with self._provider._lock:
            runtime = self._current_runtime()
            configured_thinking_mode = (
                self._provider.registry.thinking_mode_for(
                    self._workload.id
                )
            )
        if runtime is None:
            return self._provider._offline_chat.chat_json(
                messages, response_schema_hint
            )

        def invoke() -> str:
            content = runtime.raw.chat_json(
                messages,
                response_schema_hint,
                timeout=timeout,
                max_retries=max_retries,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                cancel_event=cancel_event,
                bypass_cache=bypass_cache,
                response_validator=response_validator,
                thinking_mode=(
                    thinking_mode
                    if thinking_mode is not None
                    else (
                        None
                        if configured_thinking_mode == "provider_default"
                        else configured_thinking_mode
                    )
                ),
            )
            repair_mode = (
                self.settings.model_json_repair_mode
                if self._workload.id in _JSON_REPAIR_WORKLOADS
                else "off"
            )
            try:
                parsed = parse_model_json_object(
                    content,
                    response_schema_hint,
                    allow_repair=repair_mode in {"shadow", "on"},
                )
            except ModelJsonRepairError as exc:
                if repair_mode in {"shadow", "on"}:
                    self._emit_json_repair_event(
                        status="rejected", reason=exc.reason
                    )
                raise MalformedModelResponse() from exc
            if parsed.repaired:
                self._emit_json_repair_event(
                    status=(
                        "repairable" if repair_mode == "shadow" else "repaired"
                    ),
                    reason="syntax",
                )
                if repair_mode == "shadow":
                    raise MalformedModelResponse()
            return parsed.content

        return self._resolve(self._submit(
            runtime, invoke, cancel_event=cancel_event
        ))


class ScheduledEmbedder(_ScheduledAdapter):
    @property
    def configured(self) -> bool:
        return self._provider.configured(self._workload.id)

    @property
    def dim(self) -> int:
        runtime = self._current_runtime()
        return (
            int(runtime.raw.dim)
            if runtime is not None
            else int(self._provider.settings.embed_dim)
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        runtime = self._current_runtime()
        if runtime is None:
            return self._provider._offline_embedding(self._workload.id).embed_texts(texts)
        return self._resolve(self._submit(
            runtime, lambda: runtime.raw.embed_texts(texts)
        ))

    def embed_query(self, text: str) -> list[float]:
        runtime = self._current_runtime()
        if runtime is None:
            return self._provider._offline_embedding(self._workload.id).embed_query(text)
        return self._resolve(self._submit(
            runtime, lambda: runtime.raw.embed_query(text)
        ))


class ScheduledRerankClient(_ScheduledAdapter):
    @property
    def configured(self) -> bool:
        return self._provider.configured(self._workload.id)

    def rerank(self, query: str, documents: list[str], on_error=None) -> list[int]:
        if not documents:
            return []
        runtime = self._current_runtime()
        if runtime is None:
            return self._provider._offline_rerank.rerank(query, documents, on_error)
        maximum = max(1, int(getattr(runtime.raw, "max_docs", len(documents))))
        batches = [
            (start, documents[start : start + maximum])
            for start in range(0, len(documents), maximum)
        ]
        capacity = runtime.service.max_concurrency
        batch_iter = iter(batches)
        calls: list[tuple[int, _SubmittedCall]] = []

        def fill_window() -> None:
            while len(calls) < capacity:
                try:
                    base, docs = next(batch_iter)
                except StopIteration:
                    return
                calls.append((
                    base,
                    self._submit(runtime, lambda docs=docs: _validate_rerank_rows(
                        runtime.raw._rerank_batch(query, docs), len(docs)
                    )),
                ))

        fill_window()
        scored: list[dict] = []
        failures: list[Exception] = []
        while calls:
            base, call = calls.pop(0)
            try:
                rows = self._resolve(call)
            except Exception as exc:
                failures.append(exc)
            else:
                scored.extend(
                    {
                        "index": base + row["index"],
                        "relevance_score": row["relevance_score"],
                    }
                    for row in rows
                )
            if not failures:
                fill_window()
        if failures:
            if on_error is not None:
                on_error(failures[0])
            return list(range(len(documents)))
        order: list[int] = []
        seen: set[int] = set()
        for row in sorted(scored, key=lambda value: value["relevance_score"], reverse=True):
            index = row["index"]
            if 0 <= index < len(documents) and index not in seen:
                seen.add(index)
                order.append(index)
        order.extend(index for index in range(len(documents)) if index not in seen)
        return order


class RuntimeModelProvider:
    """Process-owned system model services bound to stable product workloads."""

    def __init__(
        self,
        settings: Settings,
        event_log: Any,
        *,
        registry: SystemModelServiceRegistry | None = None,
        chat_factory: Callable[[ModelServiceDefinition], Any] | None = None,
        embedding_factory: Callable[[ModelServiceDefinition], Any] | None = None,
        rerank_factory: Callable[[ModelServiceDefinition], Any] | None = None,
        observation_sink: Callable[[ProviderObservation], None] | None = None,
    ) -> None:
        self.settings = settings
        self.event_log = event_log
        self.registry = registry or SystemModelServiceRegistry.load(settings)
        self._owns_registry = registry is None
        self._chat_factory = chat_factory or self._raw_chat
        self._embedding_factory = embedding_factory or self._raw_embedding
        self._rerank_factory = rerank_factory or self._raw_rerank
        self._lock = threading.RLock()
        # Physical service ids may be redefined by a hot reload.  Key runtimes
        # by the validated configuration fingerprint so already-submitted work
        # can finish on its original generation while new work routes to the
        # replacement generation.
        self._runtimes: dict[tuple[str, str], _ServiceRuntime] = {}
        self._adapters: dict[tuple[str, str], Any] = {}
        self._closing = False
        self._closed = False
        self._close_complete = threading.Event()
        self._observation_sink = observation_sink
        self._observation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="model-status-observer",
        )
        self._observation_accepting = True
        self._needs_recovery: set[tuple[str, str]] = set()
        self._completion_counter: dict[str, int] = {}
        self._completion_next: dict[str, int] = {}
        self._completion_pending: dict[
            str,
            dict[int, tuple[_ServiceRuntime, WorkloadSpec, _SubmittedCall, BaseException | None]],
        ] = {}
        self._completion_occurred_at: dict[str, datetime] = {}
        self._offline_chat = _UnconfiguredChatClient(settings)
        self._offline_rerank = _UnconfiguredRerankClient()
        self._offline_embeddings: dict[str, _UnconfiguredEmbedder] = {}
        self._reload_stop = threading.Event()
        self._reload_lock = threading.Lock()
        self._config_path = self._resolved_config_path()
        self._config_signature = self._read_config_signature()
        # In-place editors briefly expose truncated but syntactically valid
        # TOML (including an empty registry).  Require the same changed
        # signature on two watcher observations before publishing it.  A
        # forced operator/test reload remains immediate.
        self._pending_config_signature: tuple[int, int, int] | None = None
        self._pending_config_observed = False
        self._reload_thread: threading.Thread | None = None
        if self._owns_registry and self._config_path is not None:
            self._reload_thread = threading.Thread(
                target=self._reload_loop,
                name="model-config-reloader",
                daemon=True,
            )
            self._reload_thread.start()

    def _resolved_config_path(self) -> Path | None:
        raw = (self.settings.model_services_config or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if path.is_absolute():
            return path
        from app.core.config import _ROOT_DIR
        return _ROOT_DIR / path

    def _read_config_signature(self) -> tuple[int, int, int] | None:
        if self._config_path is None:
            return None
        try:
            stat = self._config_path.stat()
        except OSError:
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _reload_loop(self) -> None:
        while not self._reload_stop.wait(_MODEL_CONFIG_RELOAD_INTERVAL_SECONDS):
            self.reload_if_changed()

    def reload_if_changed(self, *, force: bool = False) -> bool:
        """Atomically publish a newly valid model-services file.

        Invalid or temporarily missing files never replace the last valid
        registry.  The attempted file signature is remembered so a half-written
        file produces one diagnostic rather than a log storm; the next write
        changes the signature and is retried automatically.
        """
        if not self._owns_registry or self._config_path is None:
            return False
        signature = self._read_config_signature()
        with self._reload_lock:
            if not force:
                if signature == self._config_signature:
                    self._pending_config_signature = None
                    self._pending_config_observed = False
                    return False
                if (
                    not self._pending_config_observed
                    or signature != self._pending_config_signature
                ):
                    self._pending_config_signature = signature
                    self._pending_config_observed = True
                    return False
            self._pending_config_signature = None
            self._pending_config_observed = False
            try:
                candidate = SystemModelServiceRegistry.load(self.settings)
                # The signature that was stable before the read must still
                # describe the file after parsing.  Otherwise the candidate
                # came from a moving truncate/write snapshot and cannot be
                # published.  Seed the next observation so a now-stable final
                # file can be retried on the following watcher tick.
                loaded_signature = self._read_config_signature()
                if loaded_signature != signature:
                    self._pending_config_signature = loaded_signature
                    self._pending_config_observed = True
                    return False
                # A configured path is not the supported switch to offline
                # mode.  In particular, an empty/comment-only TOML is a valid
                # parser result but is also the common midpoint of an in-place
                # save.  Offline mode is selected by clearing the setting and
                # restarting, as documented.
                if not candidate.services():
                    raise ValueError("MODEL_SERVICES_CONFIG has no model services")
            except Exception as exc:
                self._config_signature = signature
                logger.error(
                    "model service config reload rejected; keeping previous configuration: %s",
                    exc,
                )
                try:
                    self.event_log.emit({
                        "kind": "model_config_reload",
                        "status": "error",
                        "code": "invalid_configuration",
                    })
                except Exception:
                    pass
                return False
            self._config_signature = signature
            with self._lock:
                if self._closing or self._closed:
                    return False
                self.registry = candidate
            try:
                self.event_log.emit({
                    "kind": "model_config_reload",
                    "status": "ok",
                    "service_count": len(candidate.services()),
                })
            except Exception:
                pass
            return True

    def set_observation_sink(
        self, sink: Callable[[ProviderObservation], None] | None
    ) -> None:
        with self._lock:
            self._observation_sink = sink

    def _mark_completion(self, service_id: str, timing: dict[str, Any]) -> None:
        """Stamp actual scheduled-invoke completion, not caller wake order."""
        with self._lock:
            sequence = self._completion_counter.get(service_id, 0) + 1
            self._completion_counter[service_id] = sequence
            occurred_at = datetime.now(timezone.utc)
            previous = self._completion_occurred_at.get(service_id)
            if previous is not None and occurred_at <= previous:
                occurred_at = previous + timedelta(microseconds=1)
            self._completion_occurred_at[service_id] = occurred_at
            timing["completion_seq"] = sequence
            timing["occurred_at"] = occurred_at.isoformat(timespec="microseconds")

    def _track_completion(
        self,
        runtime: _ServiceRuntime,
        workload: WorkloadSpec,
        call: _SubmittedCall,
    ) -> None:
        def completed(future: Future) -> None:
            try:
                error = future.exception()
            except CancelledError as exc:
                error = exc
            self._record_completion(runtime, workload, call, error)

        call.future.add_done_callback(completed)

    def _record_completion(
        self,
        runtime: _ServiceRuntime,
        workload: WorkloadSpec,
        call: _SubmittedCall,
        error: BaseException | None,
    ) -> None:
        sequence = call.timing.get("completion_seq")
        if not isinstance(sequence, int):
            # Work rejected/cancelled before the scheduled invoke started.
            # Those are scheduler outcomes and never provider health signals.
            return
        service_id = runtime.service.id
        with self._lock:
            pending = self._completion_pending.setdefault(service_id, {})
            pending[sequence] = (runtime, workload, call, error)
            expected = self._completion_next.get(service_id, 1)
            while expected in pending:
                item = pending.pop(expected)
                self._apply_completion_locked(*item)
                expected += 1
            self._completion_next[service_id] = expected
            if not pending:
                self._completion_pending.pop(service_id, None)

    def _apply_completion_locked(
        self,
        runtime: _ServiceRuntime,
        workload: WorkloadSpec,
        call: _SubmittedCall,
        error: BaseException | None,
    ) -> None:
        service_id = runtime.service.id
        service_generation = (service_id, runtime.service.fingerprint)
        occurred_at = str(call.timing["occurred_at"])
        if error is None:
            if service_generation not in self._needs_recovery:
                return
            self._needs_recovery.remove(service_generation)
            self._submit_observation(ProviderObservation(
                service_id=service_id,
                config_fingerprint=runtime.service.fingerprint,
                status="ok",
                code="ok",
                trigger="recovery_probe",
                support_id=call.context.support_id,
                latency_ms=self._execution_latency_ms(call),
                occurred_at=occurred_at,
            ))
            return

        if classify_provider_failure(error) is FailureKind.IGNORED:
            return
        self._needs_recovery.add(service_generation)
        typed = _invocation_error(runtime, workload, call, error)
        self._submit_observation(ProviderObservation(
            service_id=service_id,
            config_fingerprint=runtime.service.fingerprint,
            status="error",
            code=typed.code,
            trigger="observed_failure",
            support_id=call.context.support_id,
            latency_ms=self._execution_latency_ms(call),
            occurred_at=occurred_at,
        ))

    @staticmethod
    def _execution_latency_ms(call: _SubmittedCall) -> int:
        started = call.timing.get("started", call.queued_at)
        finished = call.timing.get("finished", started)
        return max(0, round((finished - started) * 1_000))

    def _submit_observation(self, observation: ProviderObservation) -> None:
        def persist() -> None:
            try:
                sink(observation)
            except Exception:
                try:
                    self.event_log.emit({
                        "kind": "model_status_observer",
                        "status": "error",
                        "service_id": observation.service_id,
                        "support_id": observation.support_id,
                        "code": "persistence_failed",
                    })
                except Exception:
                    pass

        fallback_inline = False
        with self._lock:
            sink = self._observation_sink
            if sink is None or not self._observation_accepting:
                return
            # Submission and the close-side accepting->False transition share
            # this lock, so shutdown cannot overtake an accepted observation.
            try:
                self._observation_executor.submit(persist)
            except RuntimeError:
                # Defensive invariant fallback: the model Future is already
                # resolved before its callbacks run, so preserving this health
                # signal inline cannot replace the model result/error.
                fallback_inline = True
        if fallback_inline:
            persist()

    def _raw_chat(self, service: ModelServiceDefinition) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            self.settings,
            base_url=service.base_url,
            api_key=service.api_key,
            model=service.model,
            max_connections=service.max_concurrency,
            top_p_override=service.top_p,
        )

    def _raw_embedding(self, service: ModelServiceDefinition):
        from app.services.embedding_dashscope import DashscopeEmbedder

        embedder = DashscopeEmbedder(
            self.settings,
            base_url=service.base_url,
            api_key=service.api_key,
            model=service.model,
            max_connections=service.max_concurrency,
        )
        # 内容寻址缓存包装——**生产 embedding 的唯一构造点**。ScheduledEmbedder.raw
        # 就是这个返回值，其 embed_texts/dim 都透过 raw，于是生产流量真正走缓存
        # （dim 由 CachedEmbedder.__getattr__ 透传 inner.dim）。探针走 make_embedder
        # （master 架构下 make_embedder 不含缓存）、离线走 _UnconfiguredEmbedder，
        # 两者都不经过这里，天然不缓存——与「健康探针命中缓存会假绿」的要求一致。
        # 缓存默认开；关闭时不包装，省去每批 key 计算的开销。缓存后端构造要碰磁盘
        # （建目录/表/WAL），失败一律降级为「不缓存」而非打死 embedder 工厂——与
        # llm.py 的 _get_cache 外层 try 对称，「缓存故障永不影响主流程」。
        if getattr(self.settings, "llm_cache_enabled", False):
            try:
                from app.core.cache import make_cache_backend
                backend = make_cache_backend(self.settings)
            except Exception:
                backend = None
            if backend is not None:
                from app.services.cached_embedder import CachedEmbedder
                embedder = CachedEmbedder(
                    embedder,
                    backend,
                    model=service.model,
                    truncate_chars=int(getattr(self.settings, "embed_truncate_chars", 2000)),
                    # 服务身份进缓存键：同 model 名、不同 base_url（per-user 模型
                    # 配置）不得共用全局缓存条目。base_url 非秘密；api_key 绝不入键。
                    base_url=service.base_url,
                )
        return embedder

    def _raw_rerank(self, service: ModelServiceDefinition) -> RerankClient:
        return RerankClient(
            self.settings,
            base_url=service.base_url,
            api_key=service.api_key,
            model=service.model,
            api_style=service.protocol,
            max_connections=service.max_concurrency,
        )

    def _workload(self, workload_id: str, kind: str) -> WorkloadSpec:
        try:
            workload = self.registry.workload(workload_id)
        except KeyError as exc:
            raise ValueError(f"unknown model workload: {workload_id}") from exc
        if workload.kind != kind:
            raise ValueError(
                f"model workload {workload_id} has kind {workload.kind}, expected {kind}"
            )
        return workload

    def _runtime(self, service: ModelServiceDefinition) -> _ServiceRuntime:
        with self._lock:
            if self._closing or self._closed:
                raise ModelProviderError(
                    "model provider is closed", code="model_service_unavailable"
                )
            runtime_key = (service.id, service.fingerprint)
            runtime = self._runtimes.get(runtime_key)
            if runtime is not None:
                return runtime
            scheduler = ServiceScheduler(
                service.id, maximum=service.max_concurrency
            )
            factory = {
                "chat": self._chat_factory,
                "embedding": self._embedding_factory,
                "rerank": self._rerank_factory,
            }[service.kind]
            try:
                raw = factory(service)
            except BaseException:
                scheduler.shutdown()
                raise
            runtime = _ServiceRuntime(service=service, scheduler=scheduler, raw=raw)
            self._runtimes[runtime_key] = runtime
            return runtime

    def _runtime_for_workload(
        self, workload: WorkloadSpec
    ) -> _ServiceRuntime | None:
        service = self.registry.service_for(workload.id)
        return self._runtime(service) if service is not None else None

    def _offline_embedding(self, workload_id: str) -> _UnconfiguredEmbedder:
        with self._lock:
            return self._offline_embeddings.setdefault(
                workload_id, _UnconfiguredEmbedder(dim=self.settings.embed_dim)
            )

    def _submit_scheduled(
        self,
        runtime: _ServiceRuntime,
        *,
        context: Any,
        invoke: Callable[[], Any],
    ) -> Future:
        with self._lock:
            if self._closing or self._closed:
                future: Future = Future()
                future.set_exception(
                    ModelServiceUnavailable(support_id=context.support_id)
                )
                return future
            return runtime.scheduler.submit(context=context, invoke=invoke)

    def chat(self, workload_id: str):
        workload = self._workload(workload_id, "chat")
        key = ("chat", workload_id)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = ScheduledJsonChatClient(
                    self,
                    workload,
                    initial_runtime=self._runtime_for_workload(workload),
                )
                self._adapters[key] = adapter
            return adapter

    def embedding(self, workload_id: str):
        workload = self._workload(workload_id, "embedding")
        key = ("embedding", workload_id)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = ScheduledEmbedder(
                    self,
                    workload,
                    initial_runtime=self._runtime_for_workload(workload),
                )
                self._adapters[key] = adapter
            return adapter

    def rerank(self, workload_id: str):
        workload = self._workload(workload_id, "rerank")
        key = ("rerank", workload_id)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = ScheduledRerankClient(
                    self,
                    workload,
                    initial_runtime=self._runtime_for_workload(workload),
                )
                self._adapters[key] = adapter
            return adapter

    def configured(self, workload_id: str) -> bool:
        self.registry.workload(workload_id)
        return self.registry.service_for(workload_id) is not None

    def parallelism(self, workload_id: str) -> int:
        self.registry.workload(workload_id)
        service = self.registry.service_for(workload_id)
        return service.max_concurrency if service is not None else 1

    def probe(
        self,
        service_id: str,
        *,
        actor_id: str,
        allow_half_open: bool,
    ) -> ProviderObservation:
        del allow_half_open  # never bypasses the breaker's fixed cooldown
        service = self.registry.service(service_id)
        workload = WorkloadSpec(
            id="service_probe",
            kind=service.kind,
            default_priority="interactive",
            display_label="模型服务测试",
        )
        runtime = self._runtime(service)
        adapter = _ScheduledAdapter(self, workload, pinned_runtime=runtime)

        def invoke() -> Any:
            if service.kind == "chat":
                return _validate_json_object(runtime.raw.chat_json(
                    [{"role": "user", "content": "health check"}],
                    '{"ok":true}',
                    max_retries=0,
                    bypass_cache=True,
                    thinking_mode="disabled",
                ))
            if service.kind == "embedding":
                return runtime.raw.embed_query("health check")
            return runtime.raw._rerank_batch("health check", ["health check"])

        started = time.perf_counter()
        call = adapter._submit(runtime, invoke, actor_id=actor_id, observe=False)
        try:
            adapter._resolve(call)
        except ModelInvocationError as exc:
            status = "error"
            code = exc.code
            support_id = exc.support_id
        else:
            status = "ok"
            code = "ok"
            support_id = call.context.support_id
        return ProviderObservation(
            service_id=service.id,
            config_fingerprint=service.fingerprint,
            status=status,
            code=code,
            trigger="manual_test",
            support_id=support_id,
            latency_ms=max(0, round((time.perf_counter() - started) * 1_000)),
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )

    def scheduler_snapshot(self, service_id: str) -> SchedulerSnapshot:
        service = self.registry.service(service_id)
        with self._lock:
            runtime = self._runtimes.get((service_id, service.fingerprint))
        if runtime is None:
            return SchedulerSnapshot(
                active=0,
                maximum=service.max_concurrency,
                queued=0,
                oldest_wait_ms=0,
                breaker_state="closed",
                busy=False,
            )
        return runtime.scheduler.snapshot()

    def note_model_error(
        self,
        stage: str,
        error: Exception | str,
        legacy_error: Exception | None = None,
        *legacy_args: Any,
        workload_id: str = "",
        **legacy_kwargs: Any,
    ) -> None:
        """Record raw diagnostics and expose only typed, credential-safe fields.

        ``legacy_error`` keeps embedding/rerank call sites operational until
        their Task-5 migration.  Legacy role/model hints are logs-only and
        never become a physical service identity in the Ask payload.
        """
        del legacy_args, legacy_kwargs
        raw_model = error if isinstance(error, str) else ""
        exc: Exception = legacy_error or (
            error if isinstance(error, Exception) else RuntimeError("model call failed")
        )
        self.event_log.emit({
            "kind": "model_error",
            "stage": stage,
            "workload_id": workload_id,
            "model": raw_model,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "status": "error",
        })
        sink = _ASK_MODEL_ERRORS.get()
        if sink is None:
            return

        typed = exc if isinstance(exc, ModelInvocationError) else None
        resolved_workload_id = (
            safe_model_metadata_id(getattr(typed, "workload_id", ""))
            or safe_model_metadata_id(workload_id)
        )
        workload_label = safe_model_display_name(
            getattr(typed, "workload_label", "")
        )
        if not workload_label and resolved_workload_id:
            try:
                workload_label = safe_model_display_name(
                    self.registry.workload(resolved_workload_id).display_label
                )
            except KeyError:
                workload_label = ""
        raw_code = str(getattr(exc, "code", "") or "")
        if raw_code == "model_not_configured":
            code = MODEL_ERROR_MISSING_CONFIG
        elif typed is not None or isinstance(exc, ModelSchedulingError):
            code = safe_model_error_code(raw_code)
        else:
            code = MODEL_ERROR_UPSTREAM
        sink.append({
            "service_id": safe_model_metadata_id(
                getattr(typed, "service_id", "")
            ),
            "service_name": safe_model_display_name(
                getattr(typed, "service_name", "")
            ),
            "workload_id": resolved_workload_id,
            "workload_label": workload_label,
            "stage": safe_model_error_stage(stage),
            "model": safe_model_label(getattr(typed, "model", "")),
            "message": code,
            "support_id": safe_model_support_id(
                getattr(typed, "support_id", "")
                or getattr(exc, "support_id", "")
            ),
        })

    def primary_unconfigured(self) -> bool:
        # An entirely empty registry is the supported deterministic/offline
        # runtime.  Once the operator defines any system model service, a
        # missing ask_answer binding is instead a real deployment error.
        return bool(self.registry.services()) and not self.configured("ask_answer")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._closing:
                close_complete = self._close_complete
                owner = False
                runtimes = ()
            else:
                self._closing = True
                close_complete = self._close_complete
                owner = True
                runtimes = tuple(self._runtimes.values())
        if not owner:
            close_complete.wait()
            return
        self._reload_stop.set()
        if self._reload_thread is not None:
            self._reload_thread.join()
        # `_closing` is the atomic admission boundary: a submit that completed
        # its provider-lock section before this point is in a scheduler; every
        # later submit is rejected. Do not hold the provider lock while
        # cancelling queued futures—their completion callbacks re-enter it.
        for runtime in runtimes:
            runtime.scheduler.stop_admission()
        for runtime in runtimes:
            runtime.scheduler.shutdown(wait=True)
        # Scheduler shutdown waits for active future completion callbacks,
        # which have now ordered and enqueued every provider observation.
        with self._lock:
            self._observation_accepting = False
        self._observation_executor.shutdown(wait=True, cancel_futures=False)
        for runtime in runtimes:
            close = getattr(runtime.raw, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        with self._lock:
            self._closed = True
            self._close_complete.set()
