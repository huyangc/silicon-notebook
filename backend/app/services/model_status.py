"""System model-service health snapshots and explicit administrator probes."""
from __future__ import annotations

import logging

from app.core.model_safety import (
    safe_model_display_name,
    safe_model_error_code,
    safe_model_label,
    safe_model_support_id,
)
from app.models.model_services import (
    ModelServiceStatusItem,
    ModelServicesStatus,
    ModelUnboundWorkloadView,
    ModelWorkloadView,
)
from app.repositories.ports import ModelStatusStorePort
from app.services.model_registry import (
    ModelServiceDefinition,
    SystemModelServiceRegistry,
    binding_report,
    displayable_ids,
)
from app.services.model_work import ProviderObservation, SchedulerSnapshot


logger = logging.getLogger("silicon_notebook.model_status")

_SAFE_STATUS_VALUES = frozenset({"ok", "error"})
_SAFE_TRIGGER_VALUES = frozenset({
    "manual_test", "observed_failure", "recovery_probe"
})


class ModelStatusService:
    """Combine durable observations with process-local scheduler state."""

    def __init__(
        self,
        registry: SystemModelServiceRegistry,
        provider,
        status_store: ModelStatusStorePort,
        *,
        settings: object | None = None,
    ) -> None:
        self._initial_registry = registry
        self.provider = provider
        self.status_store = status_store
        # Only the feature flags are read (``binding_report``); absent → off.
        self.settings = settings

    @property
    def registry(self) -> SystemModelServiceRegistry:
        # RuntimeModelProvider replaces its validated registry atomically when
        # MODEL_SERVICES_CONFIG is hot-reloaded.  Status/probe views must follow
        # that live registry rather than retaining the startup snapshot.
        return getattr(self.provider, "registry", self._initial_registry)

    def snapshot(self, *, include_binding_gaps: bool = False) -> ModelServicesStatus:
        """Per-service rows; binding gaps only when ``include_binding_gaps``.

        The gaps are an operator diagnostic (the remedy is editing the
        deployment's model-services.toml), so the HTTP layer asks for them for
        administrators only; everyone else gets the fields empty.
        """
        stored = self.status_store.get_all()
        return ModelServicesStatus(
            services=[
                self._snapshot_item(
                    service,
                    stored.get(service.id),
                    self.provider.scheduler_snapshot(service.id),
                )
                for service in self.registry.services()
            ],
            **(self._binding_gaps() if include_binding_gaps else {}),
        )

    def _binding_gaps(self) -> dict[str, object]:
        """Render ``binding_report`` — the startup warnings' own computation.

        Read live on every call, so a hot reload that fixes (or breaks) the
        bindings shows up here without a restart. The offline mode reports
        nothing, exactly as the boot log names no workload then. Fail-open
        like the startup diagnostic: a provider that cannot answer yields no
        gap rows rather than a failed status panel.
        """
        try:
            report = binding_report(self.settings, self.provider)
        except Exception as exc:  # diagnostic-only; never fails the panel
            logger.warning(
                "model status: binding report unavailable (%s)", type(exc).__name__
            )
            return {}
        enabled = {workload.id for workload in report.feature_enabled}
        shown, hidden = displayable_ids(report.stale_ids)
        return {
            "unbound_workloads": [
                ModelUnboundWorkloadView(
                    id=workload.id,
                    label=workload.display_label,
                    feature_enabled=workload.id in enabled,
                )
                for workload in report.unbound
            ],
            "stale_binding_ids": list(shown),
            "stale_binding_hidden_count": hidden,
        }

    def test_one(
        self, service_id: str, *, actor_id: str
    ) -> ModelServiceStatusItem:
        service = self.registry.service(service_id)
        observation = self.provider.probe(
            service_id,
            actor_id=actor_id,
            allow_half_open=True,
        )
        self.record_provider_observation(observation)
        return self._snapshot_item(
            service,
            self.status_store.get_all().get(service.id),
            self.provider.scheduler_snapshot(service.id),
        )

    def test_all(self, *, actor_id: str) -> ModelServicesStatus:
        for service in self.registry.services():
            observation = self.provider.probe(
                service.id,
                actor_id=actor_id,
                allow_half_open=True,
            )
            self.record_provider_observation(observation)
        # Only administrators reach this (the route checks), so the gaps ride
        # along: replacing the panel's snapshot must not blank them.
        return self.snapshot(include_binding_gaps=True)

    def record_provider_observation(self, observation: ProviderObservation) -> None:
        try:
            service = self.registry.service(observation.service_id)
        except KeyError:
            return
        if observation.config_fingerprint != service.fingerprint:
            return
        self.status_store.record(
            service_id=service.id,
            config_fingerprint=observation.config_fingerprint,
            status=observation.status,
            latency_ms=observation.latency_ms,
            code=observation.code,
            trigger=observation.trigger,
            support_id=observation.support_id,
            checked_at=observation.occurred_at,
        )

    def _snapshot_item(
        self,
        service: ModelServiceDefinition,
        stored: dict[str, object] | None,
        live: SchedulerSnapshot,
    ) -> ModelServiceStatusItem:
        persisted = self._current_persisted(service, stored)
        persisted_status = str(persisted.get("status", "untested"))
        if live.breaker_state == "half_open":
            status = "half_open"
        elif live.breaker_state == "open":
            status = "circuit_open"
        elif live.busy:
            status = "busy"
        else:
            status = persisted_status
        return ModelServiceStatusItem(
            service_id=service.id,
            display_name=safe_model_display_name(service.display_name),
            kind=service.kind,
            model=safe_model_label(service.model),
            workloads=[
                ModelWorkloadView(id=workload.id, label=workload.display_label)
                for workload in self.registry.workloads_for(service.id)
            ],
            status=status,
            active=max(0, int(live.active)),
            maximum=max(0, int(live.maximum)),
            queued=max(0, int(live.queued)),
            oldest_wait_ms=max(0, int(live.oldest_wait_ms)),
            latency_ms=_safe_latency(persisted.get("latency_ms")),
            checked_at=_safe_checked_at(persisted.get("checked_at")),
            trigger=_safe_trigger(persisted.get("trigger")),
            code=_safe_code(persisted.get("code")),
            support_id=safe_model_support_id(persisted.get("support_id")),
        )

    @staticmethod
    def _current_persisted(
        service: ModelServiceDefinition,
        stored: dict[str, object] | None,
    ) -> dict[str, object]:
        if not stored or stored.get("config_fingerprint") != service.fingerprint:
            return {}
        if stored.get("status") not in _SAFE_STATUS_VALUES:
            return {}
        return stored


def _safe_latency(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_checked_at(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40:
        return ""
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else ""


def _safe_trigger(value: object) -> str:
    return value if value in _SAFE_TRIGGER_VALUES else ""


def _safe_code(value: object) -> str:
    if not value or value == "ok":
        return ""
    return safe_model_error_code(value)
