from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.core.llm_logging import interaction_support_scope
from app.services.embedding import FakeEmbedder
from app.services.model_registry import (
    ModelServiceDefinition,
    SystemModelServiceRegistry,
    WorkloadSpec,
)
from app.services.model_scheduler import ServiceScheduler
from app.services.model_work import (
    MalformedModelResponse,
    ModelPriority,
    ModelProviderError,
    ModelSchedulingError,
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
    timing: dict[str, float]
    breaker_before: str


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
    ) -> _SubmittedCall:
        context = make_model_work_context(
            workload_id=self._workload.id,
            priority=ModelPriority(self._workload.default_priority),
            cancel_event=cancel_event,
            actor_id=actor_id,
            parent_id=parent_id,
            support_id=support_id,
        )
        timing: dict[str, float] = {}
        queued_at = time.perf_counter()
        breaker_before = self._runtime.scheduler.snapshot().breaker_state

        def scheduled() -> Any:
            timing["started"] = time.perf_counter()
            try:
                with interaction_support_scope(context.support_id):
                    return invoke()
            finally:
                timing["finished"] = time.perf_counter()

        future = self._runtime.scheduler.submit(context=context, invoke=scheduled)
        return _SubmittedCall(
            context=context,
            future=future,
            queued_at=queued_at,
            timing=timing,
            breaker_before=breaker_before,
        )

    def _resolve(self, call: _SubmittedCall) -> Any:
        error: BaseException | None = None
        result: Any = None
        try:
            result = call.future.result()
        except CancelledError:
            self._emit(call, status="cancelled", error=None)
            raise
        except BaseException as exc:
            error = exc
            self._emit(call, status="error", error=exc)
        else:
            self._emit(call, status="ok", error=None)
            return result

        assert error is not None
        if isinstance(error, ModelInvocationError):
            raise error
        typed = ModelInvocationError(
            service=self._runtime.service,
            workload=self._workload,
            code=_stable_error_code(error),
            support_id=call.context.support_id,
            status_code=_status_code(error),
        )
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
        calls = [
            (
                base,
                self._submit(
                    lambda docs=docs: self._runtime.raw._rerank_batch(query, docs)
                ),
            )
            for base, docs in batches
        ]
        scored: list[dict] = []
        failures: list[Exception] = []
        for base, call in calls:
            try:
                rows = self._resolve(call)
            except Exception as exc:
                failures.append(exc)
                continue
            scored.extend(
                {
                    "index": base + int(row["index"]),
                    "relevance_score": float(row.get("relevance_score", row.get("score", 0.0))),
                }
                for row in rows
            )
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
        self._closed = False
        self._offline_chat = _UnconfiguredChatClient(settings)
        self._offline_rerank = _UnconfiguredRerankClient()
        self._offline_embeddings: dict[str, FakeEmbedder] = {}

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
            if self._closed:
                scheduler.shutdown()
            return runtime

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
                    workload_id, FakeEmbedder(dim=self.settings.embed_dim)
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
        workloads = self.registry.workloads_for(service_id)
        if not workloads:
            raise ValueError(f"model service {service_id} has no bound workloads")
        workload = workloads[0]
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
        call = adapter._submit(invoke, actor_id=actor_id)
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
            self._closed = True
            runtimes = tuple(self._runtimes.values())
        for runtime in runtimes:
            runtime.scheduler.shutdown(wait=True)
        for runtime in runtimes:
            close = getattr(runtime.raw, "close", None)
            if callable(close):
                close()
