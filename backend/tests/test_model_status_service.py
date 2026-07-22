from __future__ import annotations

from dataclasses import replace

from app.services.model_registry import (
    ModelServiceDefinition,
    SystemModelServiceRegistry,
)
from app.services.model_status import ModelStatusService
from app.services.model_work import ProviderObservation, SchedulerSnapshot


def _definition(
    service_id: str,
    *,
    kind: str = "chat",
    fingerprint: str | None = None,
) -> ModelServiceDefinition:
    return ModelServiceDefinition(
        id=service_id,
        display_name=f"{service_id} display",
        kind=kind,
        protocol="openai",
        base_url=f"https://{service_id}.example/v1",
        model=f"{service_id}-model",
        api_key_env=f"{service_id.upper()}_KEY",
        api_key="secret",
        max_concurrency=3,
        fingerprint=fingerprint or f"fp-{service_id}",
    )


def _registry() -> SystemModelServiceRegistry:
    chat = _definition("chat_primary")
    embed = _definition("embed_primary", kind="embedding")
    return SystemModelServiceRegistry(
        {chat.id: chat, embed.id: embed},
        {"ask_answer": chat.id, "retrieval_query_embedding": embed.id},
    )


class _Store:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.reads = 0
        self.records = []

    def get_all(self):
        self.reads += 1
        return {key: dict(value) for key, value in self.rows.items()}

    def record(self, **values):
        self.records.append(values)
        self.rows[values["service_id"]] = {
            key: value for key, value in values.items() if key != "service_id"
        }


class _Provider:
    def __init__(self):
        self.probes = []
        self.snapshots = {}
        self.observation = None

    def probe(self, service_id, *, actor_id, allow_half_open):
        self.probes.append((service_id, actor_id, allow_half_open))
        if self.observation is not None:
            return self.observation
        return ProviderObservation(
            service_id=service_id,
            config_fingerprint=f"fp-{service_id}",
            status="ok",
            code="ok",
            trigger="manual_test",
            support_id="mdl-probe",
            latency_ms=7,
            occurred_at="2030-01-01T00:00:00+00:00",
        )

    def scheduler_snapshot(self, service_id):
        return self.snapshots.get(service_id, SchedulerSnapshot(
            active=0,
            maximum=3,
            queued=0,
            oldest_wait_ms=0,
            breaker_state="closed",
            busy=False,
        ))


def _persisted(*, fingerprint="fp-chat_primary", status="ok"):
    return {
        "config_fingerprint": fingerprint,
        "status": status,
        "latency_ms": 19,
        "code": "provider_unavailable" if status == "error" else "",
        "trigger": "observed_failure" if status == "error" else "manual_test",
        "support_id": "support-persisted",
        "checked_at": "2030-01-01T00:00:00.000000+00:00",
    }


def _service(rows=None):
    store = _Store(rows)
    provider = _Provider()
    service = ModelStatusService(_registry(), provider, store)
    return service, provider, store


def _item(snapshot, service_id="chat_primary"):
    return next(item for item in snapshot.services if item.service_id == service_id)


def test_snapshot_reads_once_never_probes_and_stale_fingerprint_is_untested():
    service, provider, store = _service({
        "chat_primary": _persisted(fingerprint="stale")
    })

    snapshot = service.snapshot()

    assert store.reads == 1
    assert provider.probes == []
    assert _item(snapshot).status == "untested"
    assert _item(snapshot).latency_ms == 0


def test_snapshot_lists_configured_services_and_workloads():
    service, _, _ = _service()
    snapshot = service.snapshot()
    assert [item.service_id for item in snapshot.services] == [
        "chat_primary", "embed_primary"
    ]
    assert [workload.id for workload in _item(snapshot).workloads] == ["ask_answer"]


def test_snapshot_precedence_half_open_open_busy_then_persisted():
    service, provider, _ = _service({"chat_primary": _persisted(status="error")})
    cases = [
        (SchedulerSnapshot(1, 3, 0, 0, "half_open", False), "half_open"),
        (SchedulerSnapshot(0, 3, 0, 0, "open", False), "circuit_open"),
        (SchedulerSnapshot(3, 3, 1, 12, "closed", True), "busy"),
        (SchedulerSnapshot(0, 3, 0, 0, "closed", False), "error"),
    ]
    for scheduler, expected in cases:
        provider.snapshots["chat_primary"] = scheduler
        assert _item(service.snapshot()).status == expected


def test_busy_and_breaker_states_are_live_overlays_only():
    service, provider, store = _service({"chat_primary": _persisted()})
    provider.snapshots["chat_primary"] = SchedulerSnapshot(
        3, 3, 4, 21, "open", True
    )
    item = _item(service.snapshot())
    assert item.status == "circuit_open"
    assert store.records == []
    assert store.rows["chat_primary"]["status"] == "ok"


def test_provider_failure_updates_shared_service_without_actor_or_workload_key():
    service, _, store = _service()
    service.record_provider_observation(ProviderObservation(
        service_id="chat_primary",
        config_fingerprint="fp-chat_primary",
        status="error",
        code="provider_unavailable",
        trigger="observed_failure",
        support_id="support-failed",
        latency_ms=31,
        occurred_at="2030-01-01T00:00:00+00:00",
    ))
    assert store.records[0]["service_id"] == "chat_primary"
    assert "actor_id" not in store.records[0]
    assert "workload_id" not in store.records[0]


def test_stale_provider_observation_is_not_persisted():
    service, _, store = _service()
    service.record_provider_observation(ProviderObservation(
        service_id="chat_primary",
        config_fingerprint="retired-fingerprint",
        status="error",
        code="provider_unavailable",
        trigger="observed_failure",
        support_id="support-failed",
        latency_ms=31,
        occurred_at="2030-01-01T00:00:00+00:00",
    ))
    assert store.records == []


def test_test_one_uses_named_service_scheduler_and_persists_observation():
    service, provider, store = _service()
    item = service.test_one("chat_primary", actor_id="admin")
    assert provider.probes == [("chat_primary", "admin", True)]
    assert item.status == "ok"
    assert item.code == ""
    assert item.support_id == "mdl-probe"
    assert store.records[0]["trigger"] == "manual_test"


def test_test_all_probes_each_configured_service_once():
    service, provider, store = _service()
    result = service.test_all(actor_id="admin")
    assert provider.probes == [
        ("chat_primary", "admin", True),
        ("embed_primary", "admin", True),
    ]
    assert len(store.records) == 2
    assert {item.status for item in result.services} == {"ok"}
