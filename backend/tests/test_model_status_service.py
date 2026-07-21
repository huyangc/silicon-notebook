import pytest

from app.core.config import Settings
from app.services.model_config import model_config_fingerprint
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/model-status.db",
        storage_dir=str(tmp_path / "storage"),
        openai_compat_base_url="https://primary.example/v1",
        openai_compat_api_key="primary-secret",
        openai_compat_model="primary-live-model",
        rerank_base_url="https://rerank.example/v1",
        rerank_api_key="rerank-secret",
        rerank_model="rerank-live-model",
        embed_provider="dashscope",
        embed_base_url="https://embed.example/v1",
        embed_api_key="embed-secret",
        embed_model="embed-live-model",
    )


@pytest.fixture
def identity(settings):
    return SQLiteRepository(settings)._runtime.identity


@pytest.fixture
def user(identity):
    return identity.current_user()


def test_snapshot_never_probes_and_invalidates_fingerprint(identity, user, settings):
    from app.services.model_status import ModelStatusService

    calls = []
    service = ModelStatusService(identity, settings, probe=lambda cfg: calls.append(cfg))
    config = identity.resolve_model_config(user, "llm")
    identity.record_model_service_status(
        user.id,
        "llm",
        "stale-fingerprint",
        "ok",
        17,
        "",
        "manual_test",
        "2030-01-01T00:00:00+00:00",
    )

    snapshot = service.snapshot(user)

    assert calls == []
    item = next(item for item in snapshot.services if item.service == "llm")
    assert item.status == "untested"
    assert item.code == ""
    assert item.latency_ms == 0
    assert item.model == config.model


def test_test_one_returns_dynamic_model_and_sanitized_failure(identity, user, settings):
    from app.services.model_status import ModelStatusService

    def fail(_config):
        raise RuntimeError("provider 10.0.0.8 rejected secret payload")

    service = ModelStatusService(identity, settings, probe=fail)
    item = service.test_one(user, "reasoning_llm")

    assert item.model == settings.openai_compat_model
    assert item.status == "error"
    assert item.code == "upstream_error"
    assert "10.0.0.8" not in item.model_dump_json()
    assert "secret" not in item.model_dump_json()
    assert "provider" not in item.model_dump_json()


def test_test_all_deduplicates_roles_sharing_one_effective_llm(identity, user, settings):
    from app.services.model_status import ModelStatusService

    calls = []
    service = ModelStatusService(
        identity,
        settings,
        probe=lambda config: calls.append((config.kind, config.model)),
    )

    result = service.test_all(user)
    expected_unique = {
        (descriptor.config.kind, descriptor.config.model)
        for descriptor in service.descriptors(user)
        if descriptor.config.configured
    }

    assert set(calls) == expected_unique
    assert len(calls) == len(expected_unique)
    assert {item.service for item in result.services} >= {
        "llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank", "embedding"
    }


def test_unconfigured_service_never_probes(identity, user, tmp_path):
    from app.services.model_status import ModelStatusService

    empty_settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/empty-model-status.db",
        storage_dir=str(tmp_path / "empty-storage"),
        user_model_config_policy="required",
    )
    empty_identity = SQLiteRepository(empty_settings)._runtime.identity
    calls = []
    service = ModelStatusService(empty_identity, empty_settings, probe=lambda cfg: calls.append(cfg))

    item = service.test_one(empty_identity.current_user(), "llm")

    assert item.status == "unconfigured"
    assert calls == []


def test_record_observed_failure_stores_only_safe_status(identity, user, settings):
    from app.services.model_status import ModelStatusService

    service = ModelStatusService(identity, settings)
    service.record_observed_failure(user, "llm")

    stored = identity.get_model_service_statuses(user.id)["llm"]
    assert stored["config_fingerprint"] == model_config_fingerprint(
        identity.resolve_model_config(user, "llm")
    )
    assert stored["status"] == "error"
    assert stored["code"] == "upstream_error"
    assert stored["trigger"] == "observed_failure"


def test_snapshot_drops_unsafe_persisted_metadata(identity, user, settings):
    from app.services.model_status import ModelStatusService

    service = ModelStatusService(identity, settings)
    config = identity.resolve_model_config(user, "llm")
    identity.record_model_service_status(
        user.id,
        "llm",
        model_config_fingerprint(config),
        "error",
        15,
        "provider 10.0.0.8 secret body",
        "manual_test",
        "provider 10.0.0.8 secret body",
    )

    item = next(item for item in service.snapshot(user).services if item.service == "llm")

    assert item.status == "error"
    assert item.code == ""
    assert item.trigger == "manual_test"
    assert item.checked_at == ""
    assert "10.0.0.8" not in item.model_dump_json()
    assert "secret" not in item.model_dump_json()


@pytest.mark.parametrize(
    "unsafe_model",
    [
        "https://10.0.0.8/v1?api_key=provider-secret",
        "sk-0123456789abcdefghijklmnop",
        "model/10.0.0.8",
        "x/sk-0123456789abcdefghijklmnop",
        "model/http://provider.example/v1",
        "model/[2001:db8::1]",
        "model/fe80::1",
        "localhost:8000",
        "model/127.0.0.1:8000",
        "model-authorization-diagnostic",
        "api.example.com:8000",
        "api.example.com:443",
        "x/api.example.com:443",
        "api:8000",
        "host:80",
        "server:22",
        "gateway:443",
        "intranet:80",
        "custom-host:443",
        "model:65535",
        "model10.0.0.8",
        "modelsk-0123456789abcdefghijklmnop",
    ],
)
def test_status_hides_unsafe_model_but_probe_receives_raw_effective_config(
    identity, user, settings, unsafe_model
):
    from app.services.model_status import ModelStatusService

    identity.set_user_model_settings(user.id, {
        "llm": {
            "base_url": "https://configured.example/v1",
            "api_key": "configured-key",
            "model": unsafe_model,
        }
    })
    calls = []
    service = ModelStatusService(identity, settings, probe=lambda config: calls.append(config.model))

    snapshot = next(item for item in service.snapshot(user).services if item.service == "llm")
    explicit = service.test_one(user, "llm")

    assert snapshot.model == ""
    assert explicit.model == ""
    assert calls == [unsafe_model]
    assert unsafe_model not in snapshot.model_dump_json()
    assert unsafe_model not in explicit.model_dump_json()


@pytest.mark.parametrize(
    "safe_model",
    [
        "llama3.2:latest",
        "llama3:70b",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "meta-llama/Llama-3.1-8B-Instruct",
    ],
)
def test_status_preserves_safe_namespace_and_tag_model_identifiers(
    identity, user, settings, safe_model
):
    from app.services.model_status import ModelStatusService

    identity.set_user_model_settings(user.id, {
        "llm": {
            "base_url": "https://configured.example/v1",
            "api_key": "configured-key",
            "model": safe_model,
        }
    })
    calls = []
    service = ModelStatusService(identity, settings, probe=lambda config: calls.append(config.model))

    snapshot = next(item for item in service.snapshot(user).services if item.service == "llm")
    explicit = service.test_one(user, "llm")

    assert snapshot.model == safe_model
    assert explicit.model == safe_model
    assert calls == [safe_model]
