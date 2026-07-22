from __future__ import annotations

import contextvars
import json
import math
import os
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.config import Settings
from app.core.ask_context import _ASK_MODEL_ERRORS
from app.core.llm import OpenAICompatibleClient
from app.core.llm_logging import interaction_support_scope
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
    if not isinstance(content, str) or not content.strip():
        raise MalformedModelResponse()
    try:
        value = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise MalformedModelResponse() from exc
    if not isinstance(value, dict):
        raise MalformedModelResponse()
    return content


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
        runtime: _ServiceRuntime,
        workload: WorkloadSpec,
    ) -> None:
        self._provider = provider
        self._runtime = runtime
        self._workload = workload

    def _submit(
        self,
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
        breaker_before = self._runtime.scheduler.snapshot().breaker_state
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
                        self._runtime.service.id, timing
                    )

        future = self._provider._submit_scheduled(
            self._runtime,
            context=context,
            invoke=scheduled,
        )
        call = _SubmittedCall(
            context=context,
            future=future,
            queued_at=queued_at,
            timing=timing,
            breaker_before=breaker_before,
            observe=observe,
        )
        if observe:
            self._provider._track_completion(
                self._runtime, self._workload, call
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
        typed = _invocation_error(self._runtime, self._workload, call, error)
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
        breaker_after = self._runtime.scheduler.snapshot().breaker_state
        event = {
            "kind": "model_scheduler",
            "status": status,
            "support_id": call.context.support_id,
            "workload_id": self._workload.id,
            "workload_label": self._workload.display_label,
            "service_id": self._runtime.service.id,
            "service_name": self._runtime.service.display_name,
            "model": self._runtime.service.model,
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
    def __init__(self, provider, runtime, workload) -> None:
        super().__init__(provider, runtime, workload)
        self.configured = True
        self.model = runtime.service.model
        self.settings = getattr(runtime.raw, "settings", provider.settings)

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
    ) -> str:
        def invoke() -> str:
            content = self._runtime.raw.chat_json(
                messages,
                response_schema_hint,
                timeout=timeout,
                max_retries=max_retries,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                cancel_event=cancel_event,
                bypass_cache=bypass_cache,
            )
            return _validate_json_object(content)

        return self._resolve(self._submit(invoke, cancel_event=cancel_event))


class ScheduledEmbedder(_ScheduledAdapter):
    def __init__(self, provider, runtime, workload) -> None:
        super().__init__(provider, runtime, workload)
        self.configured = True
        self.dim = runtime.raw.dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._resolve(self._submit(lambda: self._runtime.raw.embed_texts(texts)))

    def embed_query(self, text: str) -> list[float]:
        return self._resolve(self._submit(lambda: self._runtime.raw.embed_query(text)))


class ScheduledRerankClient(_ScheduledAdapter):
    configured = True

    def rerank(self, query: str, documents: list[str], on_error=None) -> list[int]:
        if not documents:
            return []
        maximum = max(1, int(getattr(self._runtime.raw, "max_docs", len(documents))))
        batches = [
            (start, documents[start : start + maximum])
            for start in range(0, len(documents), maximum)
        ]
        capacity = self._runtime.service.max_concurrency
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
                    self._submit(lambda docs=docs: _validate_rerank_rows(
                        self._runtime.raw._rerank_batch(query, docs), len(docs)
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
        self._chat_factory = chat_factory or self._raw_chat
        self._embedding_factory = embedding_factory or self._raw_embedding
        self._rerank_factory = rerank_factory or self._raw_rerank
        self._lock = threading.RLock()
        self._runtimes: dict[str, _ServiceRuntime] = {}
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
        self._needs_recovery: set[str] = set()
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
        occurred_at = str(call.timing["occurred_at"])
        if error is None:
            if service_id not in self._needs_recovery:
                return
            self._needs_recovery.remove(service_id)
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
        self._needs_recovery.add(service_id)
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
        )

    def _raw_embedding(self, service: ModelServiceDefinition):
        from app.services.embedding_dashscope import DashscopeEmbedder

        return DashscopeEmbedder(
            self.settings,
            base_url=service.base_url,
            api_key=service.api_key,
            model=service.model,
            max_connections=service.max_concurrency,
        )

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
            runtime = self._runtimes.get(service.id)
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
            self._runtimes[service.id] = runtime
            return runtime

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
        service = self.registry.service_for(workload_id)
        if service is None:
            return self._offline_chat
        key = ("chat", workload_id)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = ScheduledJsonChatClient(self, self._runtime(service), workload)
                self._adapters[key] = adapter
            return adapter

    def embedding(self, workload_id: str):
        workload = self._workload(workload_id, "embedding")
        service = self.registry.service_for(workload_id)
        if service is None:
            with self._lock:
                return self._offline_embeddings.setdefault(
                    workload_id, _UnconfiguredEmbedder(dim=self.settings.embed_dim)
                )
        key = ("embedding", workload_id)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = ScheduledEmbedder(self, self._runtime(service), workload)
                self._adapters[key] = adapter
            return adapter

    def rerank(self, workload_id: str):
        workload = self._workload(workload_id, "rerank")
        service = self.registry.service_for(workload_id)
        if service is None:
            return self._offline_rerank
        key = ("rerank", workload_id)
        with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = ScheduledRerankClient(self, self._runtime(service), workload)
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
        adapter = _ScheduledAdapter(self, runtime, workload)

        def invoke() -> Any:
            if service.kind == "chat":
                return _validate_json_object(runtime.raw.chat_json(
                    [{"role": "user", "content": "health check"}],
                    '{"ok":true}',
                    max_retries=0,
                    bypass_cache=True,
                ))
            if service.kind == "embedding":
                return runtime.raw.embed_query("health check")
            return runtime.raw._rerank_batch("health check", ["health check"])

        started = time.perf_counter()
        call = adapter._submit(invoke, actor_id=actor_id, observe=False)
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
            runtime = self._runtimes.get(service_id)
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

    # Read-only compatibility during the call-site migration. Each property is
    # bound to one explicit workload; there is no role/user resolution or
    # mutable transport replacement behind this surface.
    @property
    def llm_client(self):
        return self.chat("ask_answer")

    @property
    def reasoning_llm_client(self):
        return self.chat("reasoning_agent")

    @property
    def rewrite_llm_client(self):
        return self.chat("query_rewrite")

    @property
    def kg_llm_client(self):
        return self.chat("kg_extract")

    @property
    def rerank_client(self):
        return self.rerank("retrieval_rerank")

    def primary_unconfigured(self) -> bool:
        return not self.configured("ask_answer")

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
