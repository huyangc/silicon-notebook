"""Sanitized, explicit connectivity checks for effective model services."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.models.schemas import ModelServiceStatusItem, ModelServicesStatus, UserProfile
from app.repositories.ports import IdentityRepository
from app.services.embedding import make_embedder
from app.services.model_config import (
    STATUS_SERVICE_ROLES,
    ResolvedModelConfig,
    model_config_fingerprint,
)
from app.services.rerank_client import RerankClient


logger = logging.getLogger("silicon_notebook.model_status")

_SAFE_STATUS_VALUES = frozenset({"ok", "error"})
_SAFE_TRIGGER_VALUES = frozenset({"manual_test", "observed_failure"})


@dataclass(frozen=True)
class ServiceDescriptor:
    service: str
    config: ResolvedModelConfig
    required: bool


@dataclass(frozen=True)
class _ProbeOutcome:
    status: str
    latency_ms: int
    code: str
    checked_at: str


class ModelStatusService:
    """Read persisted status without probing; make probes explicit and sanitized."""

    def __init__(
        self,
        identity: IdentityRepository,
        settings: Settings,
        probe: Callable[[ResolvedModelConfig], None] | None = None,
    ) -> None:
        self.identity = identity
        self.settings = settings
        self._probe_call = probe or self._probe

    def descriptors(self, user: UserProfile) -> list[ServiceDescriptor]:
        return [
            ServiceDescriptor(
                service=service,
                config=self.identity.resolve_model_config(user, service),
                required=service == "llm",
            )
            for service in STATUS_SERVICE_ROLES
        ]

    def snapshot(self, user: UserProfile) -> ModelServicesStatus:
        descriptors = self.descriptors(user)
        stored = self.identity.get_model_service_statuses(user.id)
        return ModelServicesStatus(
            services=[self._snapshot_item(descriptor, stored.get(descriptor.service)) for descriptor in descriptors]
        )

    def test_one(self, user: UserProfile, service: str) -> ModelServiceStatusItem:
        descriptor = self._descriptor_for(user, service)
        if not descriptor.config.configured:
            return self._snapshot_item(descriptor, None)
        outcome = self._run_probe(descriptor.config, service)
        self._record(user, descriptor, outcome, "manual_test")
        return self._result_item(descriptor, outcome, "manual_test")

    def test_all(self, user: UserProfile) -> ModelServicesStatus:
        descriptors = self.descriptors(user)
        groups: dict[str, list[ServiceDescriptor]] = {}
        for descriptor in descriptors:
            if descriptor.config.configured:
                groups.setdefault(model_config_fingerprint(descriptor.config), []).append(descriptor)

        outcomes: dict[str, _ProbeOutcome] = {}
        if groups:
            with ThreadPoolExecutor(max_workers=min(4, len(groups))) as executor:
                futures = {
                    executor.submit(self._run_probe, group[0].config, group[0].service): fingerprint
                    for fingerprint, group in groups.items()
                }
                for future in as_completed(futures):
                    outcomes[futures[future]] = future.result()

        for fingerprint, group in groups.items():
            outcome = outcomes[fingerprint]
            for descriptor in group:
                self._record(user, descriptor, outcome, "manual_test")
        return self.snapshot(user)

    def record_observed_failure(self, user: UserProfile, service: str) -> None:
        descriptor = self._descriptor_for(user, service)
        if not descriptor.config.configured:
            return
        self._record(
            user,
            descriptor,
            _ProbeOutcome(
                status="error",
                latency_ms=0,
                code="upstream_error",
                checked_at=_checked_at(),
            ),
            "observed_failure",
        )

    def _descriptor_for(self, user: UserProfile, service: str) -> ServiceDescriptor:
        for descriptor in self.descriptors(user):
            if descriptor.service == service:
                return descriptor
        raise ValueError(f"unknown model service: {service}")

    def _snapshot_item(
        self, descriptor: ServiceDescriptor, stored: dict | None
    ) -> ModelServiceStatusItem:
        config = descriptor.config
        item = ModelServiceStatusItem(
            service=descriptor.service,
            model=config.model,
            source=config.source,
            kind=config.kind,
            configured=config.configured,
            required=descriptor.required,
        )
        if not config.configured:
            return item
        if not stored or stored.get("config_fingerprint") != model_config_fingerprint(config):
            return item.model_copy(update={"status": "untested"})
        status = stored.get("status")
        if status not in _SAFE_STATUS_VALUES:
            return item.model_copy(update={"status": "untested"})
        return item.model_copy(
            update={
                "status": status,
                "latency_ms": _safe_latency(stored.get("latency_ms")),
                "checked_at": _safe_checked_at(stored.get("checked_at")),
                "trigger": _safe_trigger(stored.get("trigger")),
                "code": "upstream_error" if stored.get("code") == "upstream_error" else "",
            }
        )

    def _result_item(
        self,
        descriptor: ServiceDescriptor,
        outcome: _ProbeOutcome,
        trigger: str,
    ) -> ModelServiceStatusItem:
        config = descriptor.config
        return ModelServiceStatusItem(
            service=descriptor.service,
            model=config.model,
            source=config.source,
            kind=config.kind,
            configured=True,
            required=descriptor.required,
            status=outcome.status,
            latency_ms=outcome.latency_ms,
            checked_at=outcome.checked_at,
            trigger=trigger,
            code=outcome.code,
        )

    def _record(
        self,
        user: UserProfile,
        descriptor: ServiceDescriptor,
        outcome: _ProbeOutcome,
        trigger: str,
    ) -> None:
        self.identity.record_model_service_status(
            user.id,
            descriptor.service,
            model_config_fingerprint(descriptor.config),
            outcome.status,
            outcome.latency_ms,
            outcome.code,
            trigger,
            outcome.checked_at,
        )

    def _run_probe(self, config: ResolvedModelConfig, service: str) -> _ProbeOutcome:
        started = time.perf_counter()
        try:
            self._probe_call(config)
            status, code = "ok", ""
        except Exception:  # Provider diagnostics remain in the server log only.
            logger.exception("model service test failed for %s", service)
            status, code = "error", "upstream_error"
        return _ProbeOutcome(
            status=status,
            latency_ms=round((time.perf_counter() - started) * 1000),
            code=code,
            checked_at=_checked_at(),
        )

    def _probe(self, config: ResolvedModelConfig) -> None:
        if config.kind == "llm":
            OpenAICompatibleClient(
                self.settings,
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.model,
            ).chat_json(
                [{"role": "user", "content": "ping"}],
                "{}",
                timeout=10,
                max_retries=0,
                bypass_cache=True,
            )
            return
        if config.kind == "rerank":
            RerankClient(
                self.settings,
                model=config.model,
                base_url=config.base_url,
                api_key=config.api_key,
            )._rerank_batch("ping", ["a", "b"])
            return
        if config.kind == "embedding":
            make_embedder(self.settings).embed_query("ping")
            return
        raise ValueError("unsupported model service kind")


def _checked_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_latency(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _safe_checked_at(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return value if parsed.tzinfo is not None else ""


def _safe_trigger(value: object) -> str:
    return value if value in _SAFE_TRIGGER_VALUES else ""
