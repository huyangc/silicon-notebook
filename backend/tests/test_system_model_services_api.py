from __future__ import annotations

import threading
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


def _status_item(*, status: str = "ok") -> dict[str, object]:
    return {
        "service_id": "general",
        "display_name": "通用模型服务",
        "kind": "chat",
        "model": "safe-model",
        "workloads": [{"id": "ask_answer", "label": "问答回答"}],
        "status": status,
        "active": 1,
        "maximum": 2,
        "queued": 0,
        "oldest_wait_ms": 0,
        "latency_ms": 17,
        "checked_at": "2030-01-01T00:00:00+00:00",
        "trigger": "manual_test",
        "code": "ok" if status == "ok" else "model_queue_full",
        "support_id": "mdl-safe-support",
        # A compromised/buggy adapter must not expand the public response model.
        "base_url": "https://private-provider.invalid/v1",
        "api_key": "private-secret",
        "raw_diagnostics": "provider body from 10.0.0.8",
    }


class _StatusService:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.one_calls: list[tuple[str, str]] = []
        self.all_calls: list[str] = []
        self.status = "ok"

    def snapshot(self):
        self.snapshot_calls += 1
        return {"services": [deepcopy(_status_item(status=self.status))]}

    def test_one(self, service_id: str, *, actor_id: str):
        self.one_calls.append((service_id, actor_id))
        if service_id == "missing":
            raise KeyError(service_id)
        return deepcopy(_status_item(status=self.status))

    def test_all(self, *, actor_id: str):
        self.all_calls.append(actor_id)
        return {"services": [deepcopy(_status_item(status=self.status))]}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'models-api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    app = create_app()
    service = _StatusService()
    dependency = getattr(deps, "model_status_service", None)
    if dependency is not None:
        app.dependency_overrides[dependency] = lambda: service
    return TestClient(app), service


def _register(client: TestClient, username: str = "z00123456") -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _admin(client: TestClient) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _assert_sanitized(body: object) -> None:
    encoded = str(body)
    for private_value in (
        "private-provider.invalid",
        "private-secret",
        "raw_diagnostics",
        "10.0.0.8",
        "base_url",
        "api_key",
    ):
        assert private_value not in encoded


class _MemoryStatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, object]] = {}

    def get_all(self):
        with self._lock:
            return deepcopy(self._rows)

    def record(self, **values):
        service_id = str(values.pop("service_id"))
        with self._lock:
            self._rows[service_id] = deepcopy(values)


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, event):
        self.events.append(dict(event))


class _BlockingRawChat:
    configured = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.peak = 0

    def chat_json(self, *_args, **_kwargs):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            assert self.release.wait(5), "blocked fake model call was not released"
            return '{"ok": true}'
        finally:
            with self._lock:
                self.active -= 1


class _FatalProviderError(RuntimeError):
    status_code = 401


class _FatalThenBlockingRawChat(_BlockingRawChat):
    def chat_json(self, *_args, **_kwargs):
        with self._lock:
            self.calls += 1
            call_number = self.calls
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if call_number == 1:
                raise _FatalProviderError(
                    "provider https://10.0.0.8 rejected private-secret"
                )
            self.started.set()
            assert self.release.wait(5), "half-open fake model call was not released"
            return '{"ok": true}'
        finally:
            with self._lock:
                self.active -= 1


class _AlwaysFatalRawChat:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *_args, **_kwargs):
        self.calls += 1
        raise _FatalProviderError("provider body contains private-secret")


class _ImmediateRawChat:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *_args, **_kwargs):
        self.calls += 1
        return '{"ok": true}'


def _real_status_service(raw, *, maximum: int = 1):
    from app.core.config import Settings
    from app.services.model_provider import RuntimeModelProvider
    from app.services.model_registry import (
        ModelServiceDefinition,
        SystemModelServiceRegistry,
    )
    from app.services.model_status import ModelStatusService

    definition = ModelServiceDefinition(
        id="general",
        display_name="通用模型服务",
        kind="chat",
        protocol="openai",
        base_url="https://private-provider.invalid/v1",
        model="safe-model",
        api_key_env="PRIVATE_KEY",
        api_key="private-secret",
        max_concurrency=maximum,
        fingerprint="fp-general",
    )
    registry = SystemModelServiceRegistry(
        {definition.id: definition}, {"ask_answer": definition.id}
    )
    provider = RuntimeModelProvider(
        Settings(_env_file=None, model_services_config=""),
        _EventLog(),
        registry=registry,
        chat_factory=lambda _service: raw,
    )
    service = ModelStatusService(registry, provider, _MemoryStatusStore())
    provider.set_observation_sink(service.record_provider_observation)
    return service, provider


def _override_status_service(client: TestClient, service) -> None:
    from app.api.deps import model_status_service

    client.app.dependency_overrides[model_status_service] = lambda: service


def _wait_for_queue(provider, expected: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if provider.scheduler_snapshot("general").queued == expected:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"scheduler queue did not reach {expected}: "
        f"{provider.scheduler_snapshot('general')}"
    )


def test_status_requires_authentication(api):
    client, service = api
    response = client.get("/api/model-services/status")
    assert response.status_code == 401
    assert service.snapshot_calls == 0


def test_status_is_identical_for_admin_and_user_and_never_probes(api):
    client, service = api
    user_headers, _ = _register(client)
    admin_headers, _ = _admin(client)

    user_response = client.get("/api/model-services/status", headers=user_headers)
    admin_response = client.get("/api/model-services/status", headers=admin_headers)

    assert user_response.status_code == admin_response.status_code == 200
    assert user_response.json() == admin_response.json()
    assert service.snapshot_calls == 2
    assert service.one_calls == []
    assert service.all_calls == []
    _assert_sanitized(user_response.json())


def test_ordinary_user_cannot_probe_services(api):
    client, service = api
    headers, _ = _register(client)

    one = client.post("/api/admin/model-services/general/test", headers=headers)
    all_services = client.post("/api/admin/model-services/test-all", headers=headers)

    assert one.status_code == all_services.status_code == 403
    assert service.one_calls == []
    assert service.all_calls == []


def test_admin_single_and_all_probes_use_authenticated_actor_id(api):
    client, service = api
    headers, admin_id = _admin(client)

    one = client.post("/api/admin/model-services/general/test", headers=headers)
    all_services = client.post("/api/admin/model-services/test-all", headers=headers)

    assert one.status_code == all_services.status_code == 200
    assert service.one_calls == [("general", admin_id)]
    assert service.all_calls == [admin_id]
    _assert_sanitized(one.json())
    _assert_sanitized(all_services.json())


def test_unknown_service_is_404(api):
    client, service = api
    headers, admin_id = _admin(client)
    response = client.post("/api/admin/model-services/missing/test", headers=headers)
    assert response.status_code == 404
    assert service.one_calls == [("missing", admin_id)]


@pytest.mark.parametrize("status", ["busy", "half_open"])
def test_probe_preserves_safe_scheduler_state(api, status):
    client, service = api
    service.status = status
    headers, _ = _admin(client)

    response = client.post("/api/admin/model-services/general/test", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["code"] == "model_queue_full"
    _assert_sanitized(response.json())


def test_admin_probe_shares_scheduler_capacity_with_product_workload(api):
    client, _ = api
    headers, _ = _admin(client)
    raw = _BlockingRawChat()
    service, provider = _real_status_service(raw, maximum=1)
    _override_status_service(client, service)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            product = pool.submit(
                provider.chat("ask_answer").chat_json,
                [{"role": "user", "content": "product request"}],
                '{"ok":true}',
            )
            assert raw.started.wait(2)
            probe = pool.submit(
                client.post,
                "/api/admin/model-services/general/test",
                headers=headers,
            )
            _wait_for_queue(provider, 1)

            snapshot = client.get("/api/model-services/status", headers=headers)
            item = snapshot.json()["services"][0]
            assert (item["active"], item["maximum"], item["queued"]) == (1, 1, 1)
            assert item["status"] == "busy"
            assert raw.peak == 1

            raw.release.set()
            assert product.result(timeout=3) == '{"ok": true}'
            assert probe.result(timeout=3).status_code == 200
            assert raw.peak == 1
    finally:
        raw.release.set()
        provider.close()


def test_queue_full_probe_returns_safe_busy_without_provider_traffic(api):
    client, _ = api
    headers, _ = _admin(client)
    raw = _BlockingRawChat()
    service, provider = _real_status_service(raw, maximum=1)
    _override_status_service(client, service)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            product = pool.submit(
                provider.chat("ask_answer").chat_json,
                [{"role": "user", "content": "product request"}],
                '{"ok":true}',
            )
            assert raw.started.wait(2)
            queued = [
                pool.submit(
                    client.post,
                    "/api/admin/model-services/general/test",
                    headers=headers,
                )
                for _ in range(2)
            ]
            _wait_for_queue(provider, 2)

            rejected = client.post(
                "/api/admin/model-services/general/test", headers=headers
            )
            body = rejected.json()
            assert rejected.status_code == 200
            assert body["status"] == "busy"
            assert body["code"] == "model_queue_full"
            assert body["support_id"].startswith("mdl-")
            assert raw.calls == 1
            _assert_sanitized(body)

            raw.release.set()
            assert product.result(timeout=3) == '{"ok": true}'
            assert all(result.result(timeout=3).status_code == 200 for result in queued)
            assert raw.peak == 1
    finally:
        raw.release.set()
        provider.close()


def test_half_open_allows_exactly_one_admin_probe(api, monkeypatch):
    from app.services.model_circuit_breaker import ServiceCircuitBreaker

    monkeypatch.setattr(ServiceCircuitBreaker, "COOLDOWN_SECONDS", 0.0)
    client, _ = api
    headers, _ = _admin(client)
    raw = _FatalThenBlockingRawChat()
    service, provider = _real_status_service(raw, maximum=1)
    _override_status_service(client, service)

    try:
        opened = client.post(
            "/api/admin/model-services/general/test", headers=headers
        )
        assert opened.status_code == 200
        assert opened.json()["status"] == "circuit_open"
        assert opened.json()["code"] == "provider_auth"

        with ThreadPoolExecutor(max_workers=1) as pool:
            admitted = pool.submit(
                client.post,
                "/api/admin/model-services/general/test",
                headers=headers,
            )
            assert raw.started.wait(2)

            rejected = client.post(
                "/api/admin/model-services/general/test", headers=headers
            )
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "half_open"
            assert rejected.json()["code"] == "model_service_unavailable"
            assert raw.calls == 2
            _assert_sanitized(rejected.json())

            raw.release.set()
            recovered = admitted.result(timeout=3)
            assert recovered.status_code == 200
            assert recovered.json()["status"] == "ok"
            assert raw.peak == 1
    finally:
        raw.release.set()
        provider.close()


def test_admin_test_all_continues_after_one_provider_failure(api):
    from app.core.config import Settings
    from app.services.model_provider import RuntimeModelProvider
    from app.services.model_registry import (
        ModelServiceDefinition,
        SystemModelServiceRegistry,
    )
    from app.services.model_status import ModelStatusService

    client, _ = api
    headers, _ = _admin(client)
    broken = _AlwaysFatalRawChat()
    healthy = _ImmediateRawChat()
    definitions = {
        service_id: ModelServiceDefinition(
            id=service_id,
            display_name=f"{service_id} service",
            kind="chat",
            protocol="openai",
            base_url=f"https://{service_id}.invalid/v1",
            model=f"{service_id}-model",
            api_key_env=f"{service_id.upper()}_KEY",
            api_key="private-secret",
            max_concurrency=1,
            fingerprint=f"fp-{service_id}",
        )
        for service_id in ("broken", "healthy")
    }
    registry = SystemModelServiceRegistry(definitions, {})
    raw_by_id = {"broken": broken, "healthy": healthy}
    provider = RuntimeModelProvider(
        Settings(_env_file=None, model_services_config=""),
        _EventLog(),
        registry=registry,
        chat_factory=lambda definition: raw_by_id[definition.id],
    )
    service = ModelStatusService(registry, provider, _MemoryStatusStore())
    provider.set_observation_sink(service.record_provider_observation)
    _override_status_service(client, service)

    try:
        response = client.post(
            "/api/admin/model-services/test-all", headers=headers
        )
        assert response.status_code == 200
        states = {
            item["service_id"]: (item["status"], item["code"])
            for item in response.json()["services"]
        }
        assert states == {
            "broken": ("circuit_open", "provider_auth"),
            "healthy": ("ok", ""),
        }
        assert broken.calls == healthy.calls == 1
        _assert_sanitized(response.json())
    finally:
        provider.close()


def test_openapi_contains_only_system_model_service_routes(api):
    client, _ = api
    paths = client.get("/openapi.json").json()["paths"]
    assert {
        "/api/model-services/status",
        "/api/admin/model-services/{service_id}/test",
        "/api/admin/model-services/test-all",
    } <= paths.keys()
    assert {
        "/api/me/model-settings",
        "/api/me/model-settings/test",
        "/api/me/model-services/status",
        "/api/me/model-services/{service}/test",
        "/api/me/model-services/test-all",
    }.isdisjoint(paths)
