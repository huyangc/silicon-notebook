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
